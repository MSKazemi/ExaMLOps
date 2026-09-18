"""ADR 0110 — correlation, causation and the W2 gate.

The audit chain could answer *what happened*. It could not answer what the roadmap's W2 gate
asks of an autonomous action: **who did it, on whose behalf, under which mode, and how would it
be undone** — because a chain of independent rows records events, not causation.

The load-bearing constraint here is backward compatibility. The correlation fields go *inside*
the hash (an edge an attacker could rewrite without breaking the chain would be evidence of
nothing), which means the canonical form changes — and every historical row must keep verifying.
That is what the first group of tests exists to prove.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops import evidence
from examlops.cli.main import app
from examlops.data import get_db
from examlops.data.audit import (
    autonomous_actions,
    correlation_chain,
    verify_audit_chain,
    write_audit_event,
)
from examlops.platform_db import _audit_canonical, init_db

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    init_db()
    yield
    del os.environ["PLATFORM_DB"]


# ── backward compatibility: the part that must not break ──────────────────────


def test_an_uncorrelated_event_canonicalises_exactly_as_before():
    """The whole design rests on this: no correlation ⇒ byte-identical canonical form, so every
    row written before ADR 0110 still verifies against the same hash it was stored with."""
    args = ("cli", "alice", "did_thing", "JPCP", None, "default", "2026-09-03 10:00:00")
    assert _audit_canonical(*args) == _audit_canonical(*args, None)
    assert _audit_canonical(*args) == _audit_canonical(*args, {})
    empty = {"correlation_id": None, "mode": None, "on_behalf_of": None}
    assert _audit_canonical(*args) == _audit_canonical(*args, empty)


def test_a_correlated_event_canonicalises_differently():
    """…and the converse: once a causal edge exists it is inside the hash, so rewriting it
    breaks the chain rather than passing unnoticed."""
    args = ("cli", "alice", "did_thing", "JPCP", None, "default", "2026-09-03 10:00:00")
    assert _audit_canonical(*args) != _audit_canonical(*args, {"correlation_id": "abc"})


def test_the_chain_verifies_across_a_mix_of_correlated_and_plain_events():
    write_audit_event("cli", "alice", "plain_one", "JPCP")
    with evidence.correlated(mode=evidence.MANUAL, on_behalf_of="alice"):
        write_audit_event("cli", "alice", "correlated_one", "JPCP")
    write_audit_event("cli", "alice", "plain_two", "JPCP")
    result = verify_audit_chain()
    assert result["ok"] is True, result
    assert result["count"] == 3


def test_tampering_with_a_correlation_field_breaks_the_chain():
    """A causal edge that could be rewritten without breaking the chain would be evidence of
    nothing — this is why the fields are hashed rather than merely stored beside the hash."""
    with evidence.correlated(mode=evidence.AUTONOMOUS, on_behalf_of="autopilot"):
        write_audit_event("autopilot", "svc", "did_thing", "JPCP")
    assert verify_audit_chain()["ok"] is True
    with get_db() as conn:
        # The append-only trigger refuses this edit outright, which is the first line of defence;
        # dropping it here simulates an attacker who got past that, so what is being tested is
        # the hash chain rather than the trigger.
        conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
        conn.execute("UPDATE audit_events SET on_behalf_of='someone-else' WHERE id=1")
    result = verify_audit_chain()
    assert result["ok"] is False
    assert result["broken_at_id"] == 1


# ── the context ───────────────────────────────────────────────────────────────


def test_outside_a_context_everything_is_none():
    ctx = evidence.current()
    assert ctx.is_empty
    assert ctx.correlation_id is None


def test_a_context_gets_an_id_automatically():
    with evidence.correlated() as ctx:
        assert ctx.correlation_id
        assert evidence.current().correlation_id == ctx.correlation_id


def test_nesting_sets_the_parent_which_is_the_whole_point():
    """The orchestrator → tool → downstream chain comes from nesting, not from threading an id
    through every call site."""
    with evidence.correlated() as outer:
        with evidence.correlated() as inner:
            assert inner.parent_correlation_id == outer.correlation_id
            with evidence.correlated() as innermost:
                assert innermost.parent_correlation_id == inner.correlation_id


def test_mode_and_principal_are_inherited_by_nested_work():
    """A tool called by an autonomous cycle is also acting autonomously; recording it as manual
    would understate what happened."""
    with evidence.correlated(mode=evidence.AUTONOMOUS, on_behalf_of="autopilot"):
        with evidence.correlated() as inner:
            assert inner.mode == evidence.AUTONOMOUS
            assert inner.on_behalf_of == "autopilot"


def test_inherit_false_starts_an_unrelated_unit_of_work():
    with evidence.correlated(mode=evidence.AUTONOMOUS, on_behalf_of="autopilot"):
        with evidence.correlated(inherit=False) as inner:
            assert inner.parent_correlation_id is None
            assert inner.mode is None


def test_rollback_ref_is_not_inherited():
    """A parent's inverse does not undo a child. Inheriting it would let an action claim an undo
    path that does not undo it — worse than admitting it has none."""
    with evidence.correlated(rollback_ref="restore:alias/Production/17"):
        with evidence.correlated() as inner:
            assert inner.rollback_ref is None


def test_the_context_is_restored_on_exit_and_on_error():
    with evidence.correlated() as outer:
        with pytest.raises(RuntimeError):
            with evidence.correlated():
                raise RuntimeError("boom")
        assert evidence.current().correlation_id == outer.correlation_id
    assert evidence.current().is_empty


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="mode"):
        with evidence.correlated(mode="whatever"):
            pass


def test_a_rollback_ref_can_be_attached_once_it_is_known():
    """You cannot always build the inverse before acting — you do not know which alias to
    restore until you have read the current one."""
    with evidence.correlated():
        evidence.with_rollback_ref("restore:alias/Production/17")
        assert evidence.current().rollback_ref == "restore:alias/Production/17"


# ── events pick the context up without being changed ──────────────────────────


def test_an_existing_call_site_gains_correlation_without_being_touched():
    """The design win: ~200 call sites gain correlation by being *inside* a unit of work."""
    with evidence.correlated(mode=evidence.DELEGATED, on_behalf_of="alice") as ctx:
        write_audit_event("cli", "svc", "did_thing", "JPCP")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM audit_events WHERE action='did_thing'").fetchone()
    assert row["correlation_id"] == ctx.correlation_id
    assert row["mode"] == evidence.DELEGATED
    assert row["on_behalf_of"] == "alice"


def test_an_event_outside_a_context_stores_nulls():
    write_audit_event("cli", "alice", "plain", "JPCP")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM audit_events WHERE action='plain'").fetchone()
    assert row["correlation_id"] is None
    assert row["mode"] is None


# ── reconstruction: the gate's actual question ────────────────────────────────


def test_a_causal_tree_is_reconstructable_from_the_chain_alone():
    with evidence.correlated(mode=evidence.AUTONOMOUS, on_behalf_of="autopilot") as root:
        write_audit_event("autopilot", "svc", "cycle_started", None)
        with evidence.correlated():
            write_audit_event("autopilot", "svc", "retrain_triggered", "JPCP")
            with evidence.correlated():
                write_audit_event("autopilot", "svc", "promoted", "JPCP")
    chain = correlation_chain(root.correlation_id)
    assert [e["action"] for e in chain] == ["cycle_started", "retrain_triggered", "promoted"]
    assert all(e["mode"] == evidence.AUTONOMOUS for e in chain)
    assert all(e["on_behalf_of"] == "autopilot" for e in chain)


def test_an_unrelated_unit_of_work_is_not_swept_into_the_chain():
    with evidence.correlated() as root:
        write_audit_event("cli", "svc", "mine", None)
    with evidence.correlated():
        write_audit_event("cli", "svc", "theirs", None)
    assert [e["action"] for e in correlation_chain(root.correlation_id)] == ["mine"]


def test_a_cycle_in_the_parent_links_does_not_hang():
    """`parent_correlation_id` is written by the process that acted and nothing at the database
    level stops a loop, so the walk must bound itself rather than spin."""
    with evidence.correlated(correlation_id="a", parent_correlation_id="b"):
        write_audit_event("cli", "svc", "one", None)
    with evidence.correlated(correlation_id="b", parent_correlation_id="a"):
        write_audit_event("cli", "svc", "two", None)
    assert len(correlation_chain("a")) == 2


def test_autonomous_actions_reports_which_declared_no_inverse():
    with evidence.correlated(mode=evidence.AUTONOMOUS, on_behalf_of="autopilot"):
        write_audit_event("autopilot", "svc", "no_undo", "JPCP")
    with evidence.correlated(
        mode=evidence.AUTONOMOUS, on_behalf_of="autopilot", rollback_ref="restore:alias/x/1"
    ):
        write_audit_event("autopilot", "svc", "has_undo", "JPCP")
    rows = autonomous_actions(since_days=30)
    by_action = {r["action"]: r for r in rows}
    assert by_action["no_undo"]["undoable"] is False
    assert by_action["has_undo"]["undoable"] is True


def test_manual_actions_are_not_reported_as_autonomous():
    with evidence.correlated(mode=evidence.MANUAL, on_behalf_of="alice"):
        write_audit_event("cli", "alice", "manual_thing", "JPCP")
    assert autonomous_actions(since_days=30) == []


# ── the CLI surface ───────────────────────────────────────────────────────────


def test_audit_chain_command_renders_the_tree():
    with evidence.correlated(mode=evidence.AUTONOMOUS, on_behalf_of="autopilot") as root:
        write_audit_event("autopilot", "svc", "cycle_started", None)
        with evidence.correlated():
            write_audit_event("autopilot", "svc", "retrain_triggered", "JPCP")
    result = runner.invoke(app, ["--json", "audit", "chain", root.correlation_id])
    assert result.exit_code == 0, result.output
    assert "retrain_triggered" in result.output
    assert '"mode": "autonomous"' in result.output


def test_audit_chain_command_on_an_unknown_id_says_so():
    result = runner.invoke(app, ["audit", "chain", "nope"])
    assert result.exit_code == 0, result.output
    assert "No events correlated" in result.output


def test_audit_autonomy_counts_the_window_not_the_page():
    """`exa audit autonomy` must report the window's real totals, however long the listing is.

    The compliance pack points an auditor straight here ("see: exa audit autonomy"), so the two
    surfaces have to agree. The listing is bounded — rightly, nobody reads 100k rows — but its
    `count` and its "N of M declared no inverse" warning are *counts*, and a count taken from the
    length of a page silently becomes "among the newest few hundred". An operator reading
    "1 of 500" would conclude there is one violation.
    """
    from examlops import platform_db

    with platform_db.get_db() as conn:
        conn.execute(
            "INSERT INTO audit_events (source, actor, action, target, mode, rollback_ref) "
            "VALUES ('autopilot', 'svc', 'no_undo', 'JPCP', 'autonomous', NULL)"
        )
        conn.executemany(
            "INSERT INTO audit_events (source, actor, action, target, mode, rollback_ref) "
            "VALUES ('autopilot', 'svc', 'promotion', ?, 'autonomous', 'undo-ref')",
            [(f"JPCP-{i}",) for i in range(600)],
        )
        # A human action, also without an inverse. It is NOT a policy violation — ADR 0110
        # decision 4 is about what the platform did on its own — so neither total may count it.
        conn.execute(
            "INSERT INTO audit_events (source, actor, action, target, mode, rollback_ref) "
            "VALUES ('cli', 'alice', 'promotion', 'JPCP', 'manual', NULL)"
        )

    result = runner.invoke(app, ["--json", "audit", "autonomy", "--last", "30d"])
    assert result.exit_code == 0, result.output
    assert '"count": 601' in result.output, result.output
    assert '"without_rollback": 1' in result.output, result.output


def test_audit_autonomy_command_counts_undoable():
    with evidence.correlated(mode=evidence.AUTONOMOUS, on_behalf_of="autopilot"):
        write_audit_event("autopilot", "svc", "no_undo", "JPCP")
    result = runner.invoke(app, ["--json", "audit", "autonomy", "--last", "30d"])
    assert result.exit_code == 0, result.output
    assert '"undoable": 0' in result.output
    assert '"count": 1' in result.output
