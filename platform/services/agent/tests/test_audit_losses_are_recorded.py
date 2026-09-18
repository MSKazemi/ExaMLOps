"""The agent's own audit losses must be counted, not passed over.

The library-side conversions are covered in `tests/unit/test_audit_losses_are_recorded.py`. These
two live here because they need the agent package's dependencies — and an earlier version of the
watch case used `importorskip` in the main suite, where it silently skipped and looked like
coverage it was not providing.

`audit_best_effort` fails open (an audit outage must never break a memory operation or lose an
alert) while recording the loss at WARNING and counting it in `dropped_audit_events()`. A caller
that writes the event itself inside `except Exception: pass` — or logs at DEBUG, which is invisible
at any production log level — keeps the failing-open half and discards the recording half.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db
    from examlops.data.audit import reset_dropped_audit_events

    platform_db.init_db()
    reset_dropped_audit_events()  # process-global: it would leak between tests
    yield
    reset_dropped_audit_events()


def _break_the_audit_log(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_the_watch_daemon_counts_an_alert_it_could_not_audit(monkeypatch):
    """`skipper-watch` raises alerts to three sinks, each best-effort.

    Losing the audit row is the worst of the three, because the alert still reaches the outbox:
    an operator sees the alert while the governance record that it was raised does not exist.
    """
    from skipper import watch

    from examlops.data.audit import dropped_audit_events

    _break_the_audit_log(monkeypatch)
    watch._raise_alert(
        {"kind": "drift", "target": "JPCP", "severity": "warn", "detail": "z=3.1", "value": 3.1}
    )
    assert "alert_raised" in dropped_audit_events(), dropped_audit_events()


def test_memory_governance_counts_a_lost_audit(monkeypatch):
    """`AGENT_MEMORY_AUDIT` is a documented governance control.

    This path logged its loss at `log.debug`, which nothing running in production would ever see.
    """
    from skipper import config
    from skipper import memory_types as mt

    from examlops.data.audit import dropped_audit_events

    monkeypatch.setattr(config, "AGENT_MEMORY_AUDIT", True)
    _break_the_audit_log(monkeypatch)
    mt.audit_memory_op(
        "memory_record", "pref", "alice", operator="alice", digest="d", session_id="s"
    )
    assert "memory_record" in dropped_audit_events(), dropped_audit_events()


def test_the_agent_counts_a_lost_retrain_audit(monkeypatch):
    """`retrain_triggered` is an Article 12 required event, and the agent is one of its four doors.

    The coverage check asks only whether *at least one* event of each required type exists, so a
    retrain the agent fired and failed to record is invisible while any sibling survives — the
    reason this door, like the others, must count what it loses.
    """
    from skipper.tools import training

    from examlops.data.audit import dropped_audit_events

    _break_the_audit_log(monkeypatch)
    training._audit_retrain("JPCP", "PM100Dataset", False, "minio", "r1")
    assert "retrain_triggered" in dropped_audit_events(), dropped_audit_events()


def test_the_agent_counts_a_lost_auto_retrain_audit(monkeypatch):
    """`drift_auto_retrain_triggered` records that the platform retrained *on its own initiative*.

    That is the autonomy record ADR 0110 asks about: an action nobody approved, which happened and
    must be attributable afterwards. Losing it silently leaves a retrain in the model's history
    with no entry saying the platform decided it.
    """
    from skipper import confirm as confirm_mod
    from skipper.tools import platform_ops

    from examlops import platform_db as pdb
    from examlops.data.audit import dropped_audit_events

    monkeypatch.setattr(confirm_mod, "interrupt", lambda _p: "yes")  # the HITL gate, approved
    pdb.set_drift_baseline("JPCP", {"mean": 1.0, "std": 1.0})
    pdb.set_drift_auto_retrain("JPCP", True, min_z_score=3.0, dataset_name="PM100Dataset")
    with pdb.get_db() as conn:  # a drifted prediction, far from the baseline
        conn.execute(
            "INSERT INTO drift_snapshots (model, alias, prediction, ts) "
            "VALUES ('JPCP', 'Production', 100.0, '2026-09-15 12:00:00')"
        )
    monkeypatch.setattr(
        platform_ops._http,
        "submit_retrain",
        lambda payload, automated=False: ({"flow_run_id": "r1"}, None),
    )

    _break_the_audit_log(monkeypatch)
    out = platform_ops.trigger_auto_retrain.invoke({"model_name": "JPCP"})
    assert "Triggered" in out, out  # the retrain still fired: auditing must not block it
    assert "drift_auto_retrain_triggered" in dropped_audit_events(), dropped_audit_events()
