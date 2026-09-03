"""Every dashboard audit event joins the hash chain (D4, ADR 0028).

Sixteen routers and five modules each carried an identical `_audit()` doing a raw
``INSERT INTO audit_events``, which writes a row with no ``prev_hash``/``hash``. Because
`verify_audit_chain` selects ``WHERE hash IS NOT NULL``, those rows were not merely unverified —
they were **invisible to verification**. `exa audit verify` answered `ok: True` over a log from
which every dashboard mutation had been silently excluded, and its `count` under-reported by
exactly the number of dashboard events.

The append-only triggers still stopped SQL-level edits, so nothing was rewritable through the app.
What was absent is what a chain is *for*: proof that no row was inserted between others, reordered,
or removed by someone with direct access to the database file.
"""

import pathlib

import audit_write
import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import data as pdb

    pdb.init_db()
    return str(tmp_path / "platform.db")


# ── the writer chains ─────────────────────────────────────────────────────────


def test_an_event_written_through_the_helper_is_in_the_chain(platform_db):
    from examlops.data.audit import verify_audit_chain

    audit_write.audit("mohsen", "thing_happened", "target-1", {"k": "v"})
    result = verify_audit_chain()
    assert result["ok"] is True
    assert result["count"] == 1, "the event is outside the chain"
    assert result["unchained"] == 0


def test_dashboard_and_cli_events_share_one_chain(platform_db):
    """Two half-histories cannot be verified as one. The point of routing through the same writer
    is that a CLI action and a dashboard action land in the same tamper-evident sequence."""
    from examlops.data.audit import verify_audit_chain, write_audit_event

    write_audit_event("cli", "mohsen", "from_cli", "t", None)
    audit_write.audit("mohsen", "from_dashboard", "t", {})
    write_audit_event("cli", "mohsen", "from_cli_again", "t", None)

    result = verify_audit_chain()
    assert result["ok"] is True and result["count"] == 3
    assert result["unchained"] == 0


def test_the_source_distinction_is_preserved(platform_db):
    """`dashboard-copilot` vs `dashboard-flags` is what lets an operator tell an agent proposal
    from a UI toggle — flattening them to one value would lose that."""
    from examlops.data.audit import export_audit_events

    audit_write.audit("m", "copilot_query", "home", {}, source="dashboard-copilot")
    audit_write.audit("m", "flag_set", "beta", {"enabled": True}, source="dashboard-flags")
    sources = {e["source"] for e in export_audit_events()}
    assert {"dashboard-copilot", "dashboard-flags"} <= sources


def test_the_event_is_written_on_the_callers_transaction(platform_db):
    """Not an optimisation. Every caller has just written the thing it is auditing; SQLite admits
    one writer, so opening a second connection deadlocks until `busy_timeout` and fails. It also
    makes the audit atomic with the mutation — they commit together, so an action cannot succeed
    while its record is lost."""
    used: list[str] = []

    from examlops.platform_db import get_db

    class _Spy:
        """Proxies a real connection and records the SQL that reaches it."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **k):
            used.append(sql)
            return self._inner.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with get_db() as conn:
        audit_write.audit("m", "act", "t", {}, conn=_Spy(conn))

    assert any("INSERT INTO audit_events" in sql for sql in used), (
        "the helper opened its own connection instead of joining the caller's transaction"
    )
    from examlops.data.audit import verify_audit_chain

    assert verify_audit_chain()["count"] == 1


def test_a_lost_audit_event_is_never_swallowed(platform_db):
    """The first version logged a warning and returned, so a locked database silently dropped the
    event — strictly worse than the unchained row it was replacing."""

    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("database is locked")

    with pytest.raises(RuntimeError):
        audit_write.audit("m", "act", "t", {}, conn=_Broken())


# ── the guard ─────────────────────────────────────────────────────────────────


def test_no_backend_module_writes_audit_events_directly():
    """The defect was one line copied into twenty-two files. A guard is the only thing that stops
    the twenty-third."""
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        if "tests" in path.parts or path.name == "audit_write.py":
            continue
        if "INSERT INTO audit_events" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(BACKEND)))
    assert not offenders, (
        f"these write audit rows outside the hash chain: {offenders} — use "
        "`audit_write.audit(...)`, which routes through `examlops.data.audit.write_audit_event`. "
        "A raw INSERT leaves prev_hash/hash NULL, and `verify_audit_chain` skips such rows "
        "entirely, so the event becomes invisible to verification rather than merely unverified."
    )
