"""ADR 0007 decision 3 — a lost online-eval audit event is counted, and the operation stands.

`exa eval online enable|disable` and the scheduler's cycle each record an audit event through
`audit_best_effort`. The audit log failing must not undo the schedule change or the scored window
(fail open), but the loss must reach `dropped_audit_events()` — the hash chain and the Art. 12
check are both blind to an event that never arrived. See
`tests/unit/test_audit_losses_are_recorded.py` for the rule these tests are registered under.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.evaluation import online  # noqa: E402

NOW = 1_780_000_000.0


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for name in (online.ENABLED_ENV, online.TEXTFILE_ENV, "EXAMLOPS_EVAL_TEMPO_URL"):
        monkeypatch.delenv(name, raising=False)
    from examlops import platform_db
    from examlops.data.audit import reset_dropped_audit_events

    platform_db.init_db()
    reset_dropped_audit_events()
    yield
    reset_dropped_audit_events()


def _break_the_audit_log(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def _cli(args):
    from typer.testing import CliRunner

    from examlops.cli.commands.eval_cmd import app

    return CliRunner().invoke(app, args)


def test_a_lost_online_enable_audit_is_counted_and_the_schedule_is_saved(monkeypatch):
    from examlops.data.audit import dropped_audit_events
    from examlops.data.evaluation import get_online_eval

    _break_the_audit_log(monkeypatch)
    res = _cli(["online", "enable", "JPCP", "--suite", "live", "-e", "abs_error"])

    assert res.exit_code == 0, res.output
    cfg = get_online_eval("JPCP")
    assert cfg is not None and cfg["enabled"] is True and cfg["evaluators"] == ["abs_error"]
    assert dropped_audit_events().get("eval_online_enabled") == 1, dropped_audit_events()


def test_a_lost_online_disable_audit_is_counted_and_the_schedule_is_stopped(monkeypatch):
    from examlops.data.audit import dropped_audit_events
    from examlops.data.evaluation import get_online_eval, set_online_eval

    set_online_eval("JPCP", suite="live", evaluators=["abs_error"], window_s=3600, sample_size=5)
    _break_the_audit_log(monkeypatch)
    res = _cli(["online", "disable", "JPCP"])

    assert res.exit_code == 0, res.output
    assert get_online_eval("JPCP")["enabled"] is False
    assert dropped_audit_events().get("eval_online_disabled") == 1, dropped_audit_events()


def test_a_lost_online_cycle_audit_is_counted_and_the_window_is_still_recorded(monkeypatch):
    from examlops import platform_db
    from examlops.data.audit import dropped_audit_events
    from examlops.data.evaluation import get_eval_results, set_online_eval

    at = int(NOW) - 60
    with platform_db.get_db() as conn:
        for i, (p, y) in enumerate([(1.0, 1.0), (2.0, 3.0)]):
            conn.execute(
                "INSERT INTO predictions (model, alias, request_hash, prediction, ts)"
                " VALUES (?,?,?,?,datetime(?, 'unixepoch'))",
                ("JPCP", "Production", f"h{i}", p, at),
            )
            conn.execute(
                "INSERT INTO ground_truth (request_hash, label) VALUES (?,?)", (f"h{i}", y)
            )
    set_online_eval("JPCP", suite="live", evaluators=["abs_error"], window_s=3600, sample_size=50)
    monkeypatch.setenv(online.ENABLED_ENV, "1")
    _break_the_audit_log(monkeypatch)

    report = online.OnlineEvalScheduler(clock=lambda: NOW).run_cycle()

    assert report.runs and report.runs[0].outcome == online.RECORDED, report.runs
    assert {r["metric"] for r in get_eval_results("JPCP", "live")} >= {"mae"}
    assert dropped_audit_events().get("eval_online_cycle") == 1, dropped_audit_events()
