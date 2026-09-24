"""D4 — immutable, tamper-evident audit trail (ADR 0028).

GWT acceptance criteria from ``design/vision/specs/D4-immutable-audit-trail.md`` §5.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def test_gwt1_events_are_hash_chained():
    """R1: each event stores prev_hash + hash forming a chain."""
    from examlops import platform_db

    platform_db.write_audit_event("cli", "alice", "a1", "JPCP")
    platform_db.write_audit_event("cli", "bob", "a2", "JPCP")
    with platform_db.get_db() as conn:
        rows = conn.execute("SELECT prev_hash, hash FROM audit_events ORDER BY id").fetchall()
    assert rows[0]["prev_hash"] == "GENESIS"
    assert rows[0]["hash"]
    assert rows[1]["prev_hash"] == rows[0]["hash"]  # chained


def test_verify_clean_chain():
    from examlops import platform_db

    for i in range(5):
        platform_db.write_audit_event("cli", "a", f"act{i}", "JPCP")
    result = platform_db.verify_audit_chain()
    assert result["ok"] is True
    assert result["count"] == 5


def test_gwt2_tamper_breaks_chain():
    """R2: an edit causes verification to fail and identifies the first broken link."""
    from examlops import platform_db

    for i in range(5):
        platform_db.write_audit_event("cli", "a", f"act{i}", "JPCP")

    # Simulate an attacker who has database access: drop the guard trigger, then edit a row.
    # Done through the platform connection rather than a raw sqlite3 file handle, so the property
    # is proven on whichever engine is configured (SQLite by default, Postgres under
    # EXAMLOPS_DB_BACKEND=postgres).
    with platform_db.get_db() as conn:
        conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
        conn.execute("UPDATE audit_events SET action='HACKED' WHERE id=3")

    result = platform_db.verify_audit_chain()
    assert result["ok"] is False
    assert result["broken_at_id"] == 3


def test_gwt3_append_only_delete_blocked():
    """R3: DELETE on audit_events is blocked at the DB level."""
    from examlops import platform_db

    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    with platform_db.get_db() as conn:  # noqa: SIM117
        # Engine-agnostic on purpose: what R3 asserts is that the *database* refuses, not which
        # driver's exception class carries the refusal (sqlite3.IntegrityError / psycopg.errors).
        with pytest.raises(Exception, match="append-only"):
            conn.execute("DELETE FROM audit_events")


def test_gwt3_append_only_update_blocked():
    from examlops import platform_db

    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    with platform_db.get_db() as conn:  # noqa: SIM117
        with pytest.raises(Exception, match="append-only"):
            conn.execute("UPDATE audit_events SET action='x'")


def test_gwt5_checkpoint_signed_over_head():
    """R5: the chain head can be signed, producing a provable detached checkpoint."""
    from examlops import platform_db

    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    head = platform_db.audit_chain_head()
    cp = platform_db.sign_audit_checkpoint("sig-abc", key_id="test")
    assert cp["head_id"] == head["id"]
    assert cp["head_hash"] == head["hash"]
    assert platform_db.list_audit_checkpoints()[0]["signature"] == "sig-abc"


def test_export_is_read_only():
    """R4: export reads events without deleting (append-only)."""
    from examlops import platform_db

    for i in range(3):
        platform_db.write_audit_event("cli", "a", f"act{i}", "JPCP")
    events = platform_db.export_audit_events()
    assert len(events) == 3
    # Events still present after export.
    assert len(platform_db.export_audit_events()) == 3


def test_gwt7_events_carry_actor_tenant_resource():
    """R7: events carry actor, tenant, and resource."""
    from examlops import platform_db

    platform_db.write_audit_event("cli", "alice", "promotion", "JPCP", tenant="acme")
    with platform_db.get_db() as conn:
        row = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["actor"] == "alice"
    assert row["tenant"] == "acme"
    assert row["target"] == "JPCP"


def test_cli_verify_and_checkpoint(monkeypatch):
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    # A real signing key must be configured to produce a non-forgeable checkpoint (item 0.7).
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-signing-key")
    for i in range(3):
        platform_db.write_audit_event("cli", "a", f"act{i}", "JPCP")
    runner = CliRunner()
    r1 = runner.invoke(app, ["audit", "verify"])
    assert r1.exit_code == 0, r1.output
    assert "verified" in r1.output.lower()
    r2 = runner.invoke(app, ["audit", "checkpoint"])
    assert r2.exit_code == 0, r2.output
    r3 = runner.invoke(app, ["audit", "checkpoints"])
    assert r3.exit_code == 0, r3.output


def test_cli_checkpoint_fails_closed_without_signing_key(monkeypatch):
    """0.7: with no signing key, checkpoint refuses (exit 1) instead of signing forgeably."""
    from typer.testing import CliRunner

    import examlops.supplychain as sc
    from examlops import platform_db
    from examlops.cli.main import app

    # Ensure neither the env key nor a D7 secret is available.
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY", raising=False)

    def _no_key() -> bytes:
        raise sc.SigningKeyMissing("no signing key")

    monkeypatch.setattr(sc, "_signing_key", _no_key)
    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    runner = CliRunner()
    r = runner.invoke(app, ["audit", "checkpoint"])
    assert r.exit_code == 1, r.output
    assert "refusing" in r.output.lower() or "cannot sign" in r.output.lower()
    # No forgeable checkpoint was recorded.
    assert platform_db.list_audit_checkpoints() == []


def test_cli_bare_audit_still_works():
    """Converting to a group must not break `exa audit --last`."""
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    platform_db.write_audit_event("cli", "a", "act", "JPCP")
    runner = CliRunner()
    result = runner.invoke(app, ["audit", "--last", "7d"])
    assert result.exit_code == 0, result.output


def test_the_audit_log_is_shown_in_chain_order_not_by_a_one_second_timestamp():
    """ "The most recent N events" must mean the last N in the chain.

    `ts` is `CURRENT_TIMESTAMP`, which has one-second resolution, and a single retrain or autopilot
    cycle writes many events inside one second. Ordering by `ts` alone leaves every tie to the query
    plan, and SQLite resolves them by scanning forward — so `exa audit -n 5` returned the five
    *oldest* events of the tied second and presented them as the newest, stably enough to look
    right. The chain's own order is `id`; that is what makes it a chain, and it is what an auditor
    reconstructing a sequence of events is relying on.
    """
    from datetime import datetime

    from examlops import platform_db

    # Seeded with one explicit `ts` rather than by writing 12 events and hoping: through
    # `write_audit_event` they straddle a second boundary about one run in three, and the test
    # then skipped its own precondition. The behaviour under test is the ORDER BY, and a shared
    # timestamp with ascending ids is exactly the state that exercises it.
    #
    # The shared second is read from the clock, not written as a literal. Pinning it to
    # '2026-09-15 12:00:00' fixed the straddling and created a time bomb in its place: the command
    # selects `ts >= utcnow() - 7d`, so on 2026-09-22 the seeded rows fell out of the window, the
    # command took its "no events" branch, and the assertion below stopped running. What this test
    # needs is only that the 12 events share ONE second — never that it be a particular one.
    # `datetime.utcnow()` (not `now()`) because that is the clock the command compares against.
    shared_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with platform_db.get_db() as conn:
        conn.executemany(
            "INSERT INTO audit_events (ts, source, actor, action, target) "
            "VALUES (?, 'cli', 'alice', ?, 'JPCP')",
            [(shared_ts, f"step_{i:02d}") for i in range(12)],
        )
        assert len(conn.execute("SELECT DISTINCT ts FROM audit_events").fetchall()) == 1, (
            "the events must share one second for this to test anything"
        )

    # Drive the real command, not a query written here: the defect is in the SQL the CLI runs.
    import json as _json

    from typer.testing import CliRunner

    from examlops.cli.main import app

    result = CliRunner().invoke(app, ["--json", "audit", "--last", "7d", "--limit", "5"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.stdout)
    events = payload["events"] if isinstance(payload, dict) and "events" in payload else payload
    # Check that the command selected the seeded rows at all, BEFORE ordering is asserted. Without
    # this the "no events found" ok-document fails on a KeyError several lines further down, which
    # reads as a broken payload shape rather than as "this test is no longer testing anything".
    assert isinstance(events, list) and events, (
        "`exa audit --last 7d` returned no events, so the ORDER BY this test exists to prove was "
        f"never exercised. The 12 rows were stamped {shared_ts!r} and the command selects "
        "`ts >= utcnow() - 7d` — if the seed no longer lands inside that window (a hardcoded "
        f"stamp, a frozen clock, a timezone skew), fix the seed, not this assertion. Got: "
        f"{result.stdout!r}"
    )
    shown = [e["action"] for e in events]
    assert shown == ["step_11", "step_10", "step_09", "step_08", "step_07"], shown


def test_the_audit_cli_and_the_dashboard_order_audit_reads_the_same_way():
    """Both audit surfaces must break a `ts` tie the same way, or they disagree about history.

    They are read by the same person about the same incident — the compliance pack points at the
    CLI, the console shows the console. A tie broken differently in two places is two different
    answers to "what happened first".
    """
    import ast
    import re

    root = Path(__file__).parents[2]
    # Scoped to the FUNCTION that reads audit_events, not to the file: `platform_ops.py` also
    # queries drift_snapshots by `ts`, which is a legitimate sample window, and a file-scoped
    # guard flagged it. The property is "this statement reads the audit chain", and a function is
    # the smallest unit that holds both the `FROM audit_events` and the `ORDER BY` a query-builder
    # appends later.
    offenders: list[str] = []
    for src_file in sorted(root.glob("platform/**/*.py")):
        sp = str(src_file)
        if "/build/" in sp or "/tests/" in sp or src_file.name.startswith("test_"):
            continue
        text = src_file.read_text()
        if "FROM audit_events" not in text:
            continue
        for fn in ast.walk(ast.parse(text)):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            literals = [
                n.value
                for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            if not any("FROM audit_events" in lit for lit in literals):
                continue
            for lit in literals:
                for clause in re.findall(r"ORDER BY ts(?: DESC)?.*", lit):
                    if any(k in clause for k in ("id DESC", "id ASC", "rowid")):
                        continue
                    offenders.append(
                        f"{src_file.relative_to(root)}:{fn.lineno} {fn.name}(): {clause.strip()!r}"
                    )
    assert not offenders, (
        "these order audit events by `ts` alone, which has one-second resolution — the tie goes "
        "to the query plan, and `exa audit -n 5` returned the OLDEST five of a busy second as the "
        "newest. `id` is the hash chain's order:\n  " + "\n  ".join(offenders)
    )
