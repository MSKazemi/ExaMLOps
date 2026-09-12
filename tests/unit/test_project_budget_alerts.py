# tests/unit/test_project_budget_alerts.py
"""ADR 0089 — a project budget means its period, and a breach announces itself (BL-073).

Two defects, found reading an **Accepted** ADR against the code.

1. **The period was ignored.** A budget row carries one (`monthly` by default, which is what
   `exa project budget` writes) and `get_project_consumption` summed *every cost ever recorded*
   against it. So a monthly budget breached permanently once lifetime spend passed it, never reset
   at the month boundary, and the number an operator was shown did not mean what its label said.

2. **The breach was only raised while somebody looked.** The clause says a breach "raises a
   governance event". The only writer was `exa project budget`, so a breach existed when a human
   ran that command — and it wrote one duplicate event per run. Nothing evaluated a project's
   budget on its own; the agent's watcher only watches one platform-wide figure from its own config.

Now consumption is windowed by the period, and `evaluate_budget` announces the *transition* — into
breach, and back out of it — at the point the spend is recorded.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.data.projects import get_db  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    assign_model_to_project,
    create_project,
    get_project_budget,
    init_db,
    record_model_cost,
    set_project_budget,
)
from examlops.project_finops import budget_status, evaluate_budget, period_window  # noqa: E402

NOW = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def project():
    init_db()
    create_project("research", created_by="t")
    assign_model_to_project("research", "JPCP")
    return "research"


def _spend(cost: float, *, when: datetime | None = None, gpu: float = 1.0, version: int = 1):
    """Record one cost row for the project's model, optionally back-dated."""
    record_model_cost("JPCP", version, f"run-{version}", "job", gpu, cost)
    if when is not None:
        with get_db() as conn:
            conn.execute(
                "UPDATE model_costs SET recorded_at=? WHERE version=?",
                (when.isoformat(timespec="seconds"), version),
            )


def _events(action: str) -> list[dict]:
    from examlops.data.audit import export_audit_events

    return [e for e in export_audit_events() if e["action"] == action]


# ── the period is a window ───────────────────────────────────────────────────


def test_a_monthly_budget_counts_this_month_only():
    set_project_budget("research", None, 100.0, "monthly", "t")
    _spend(80.0, when=NOW - timedelta(days=40), version=1)  # last month's spend
    _spend(30.0, when=NOW - timedelta(days=1), version=2)  # this month's

    status = budget_status("research", now=NOW)

    assert status["consumption"]["cost_usd"] == 30.0
    assert status["consumption_total"]["cost_usd"] == 110.0, "lifetime is still reported"
    assert status["over_budget"] is False, "110 lifetime, but only 30 this month"


def test_a_monthly_budget_breaches_on_this_month_alone():
    set_project_budget("research", None, 25.0, "monthly", "t")
    _spend(30.0, when=NOW - timedelta(days=1))

    status = budget_status("research", now=NOW)

    assert status["over_budget"] is True
    assert status["breaches"] == ["cost $30.00 exceeds budget $25.00"]


def test_a_total_budget_counts_everything():
    set_project_budget("research", None, 100.0, "total", "t")
    _spend(80.0, when=NOW - timedelta(days=400), version=1)
    _spend(30.0, when=NOW - timedelta(days=1), version=2)

    status = budget_status("research", now=NOW)

    assert (status["period"], status["consumption"]["cost_usd"]) == ("total", 110.0)
    assert status["over_budget"] is True


def test_a_gpu_hours_budget_is_windowed_too():
    set_project_budget("research", 5.0, None, "monthly", "t")
    _spend(1.0, when=NOW - timedelta(days=40), gpu=99.0, version=1)

    assert budget_status("research", now=NOW)["over_budget"] is False


@pytest.mark.parametrize(
    "period,expected",
    [("monthly", "monthly"), ("total", "total"), ("MONTHLY", "monthly"), ("quarterly", "monthly")],
)
def test_the_period_is_resolved_and_reported(period, expected):
    """An unrecognised period falls back to the default, and the status says which was used."""
    since, resolved = period_window(period, now=NOW)

    assert resolved == expected
    assert (since is None) is (expected == "total")
    if since:
        assert since.startswith("2026-09-01T00:00:00")


def test_a_month_boundary_resets_the_window():
    set_project_budget("research", None, 25.0, "monthly", "t")
    _spend(30.0, when=datetime(2026, 9, 30, 12, tzinfo=UTC))

    assert budget_status("research", now=datetime(2026, 9, 30, 23, tzinfo=UTC))["over_budget"]
    assert not budget_status("research", now=datetime(2026, 10, 1, 0, 1, tzinfo=UTC))["over_budget"]


# ── the transition is announced, once ────────────────────────────────────────


def test_entering_breach_raises_one_event():
    set_project_budget("research", None, 10.0, "monthly", "t")
    _spend(25.0, when=NOW)

    result = evaluate_budget("research", actor="t", now=NOW)

    assert result["alert"] == "breached"
    (event,) = _events("project_budget_breach")
    assert event["target"] == "research"


def test_staying_in_breach_raises_nothing_more():
    set_project_budget("research", None, 10.0, "monthly", "t")
    _spend(25.0, when=NOW)

    first = evaluate_budget("research", now=NOW)
    second = evaluate_budget("research", now=NOW)
    third = evaluate_budget("research", now=NOW)

    assert (first["alert"], second["alert"], third["alert"]) == ("breached", None, None)
    assert len(_events("project_budget_breach")) == 1, "one event per breach, not per look"


def test_a_worsening_breach_is_announced_again():
    """A cost breach added to a GPU-hours breach is new information."""
    set_project_budget("research", 1.0, 10.0, "monthly", "t")
    _spend(2.0, when=NOW, gpu=5.0, version=1)
    evaluate_budget("research", now=NOW)

    _spend(50.0, when=NOW, gpu=0.0, version=2)
    result = evaluate_budget("research", now=NOW)

    assert result["alert"] == "breached"
    assert len(_events("project_budget_breach")) == 2


def test_leaving_breach_is_announced():
    set_project_budget("research", None, 10.0, "monthly", "t")
    _spend(25.0, when=NOW)
    evaluate_budget("research", now=NOW)

    set_project_budget("research", None, 100.0, "monthly", "t")  # the operator raises it
    result = evaluate_budget("research", now=NOW)

    assert result["alert"] == "recovered"
    assert len(_events("project_budget_recovered")) == 1


def test_raising_the_budget_keeps_the_alert_state():
    """`INSERT OR REPLACE` would drop it, and the recovery would never be noticed."""
    set_project_budget("research", None, 10.0, "monthly", "t")
    _spend(25.0, when=NOW)
    evaluate_budget("research", now=NOW)

    set_project_budget("research", None, 12.0, "monthly", "t")  # still breached

    with get_db() as conn:
        row = conn.execute(
            "SELECT alert_state, period FROM project_budgets WHERE project='research'"
        ).fetchone()
    assert (row["alert_state"], row["period"]) == ("breached", "monthly")
    assert get_project_budget("research")["cost_budget"] == 12.0


def test_a_project_with_no_budget_is_never_in_breach():
    _spend(1000.0, when=NOW)

    result = evaluate_budget("research", now=NOW)

    assert (result["alert"], result["over_budget"]) == (None, False)
    assert _events("project_budget_breach") == []


# ── the spend point evaluates it, with no daemon ─────────────────────────────


def test_recording_a_cost_announces_the_breach_it_caused(monkeypatch):
    """`exa models cost --record`: the breach exists the moment the spend is recorded."""
    from unittest.mock import patch

    from typer.testing import CliRunner

    from examlops.cli.main import app

    set_project_budget("research", None, 1.0, "monthly", "t")
    monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "mock")
    monkeypatch.delenv("EXAMLOPS_HPC_SCHEDULER", raising=False)
    registered = {"registered_model": {"latest_versions": [{"version": "4", "run_id": "r4"}]}}

    with patch("examlops.cli._client.get", return_value=registered):
        with patch("examlops.cli._client.post", return_value={}):
            result = CliRunner().invoke(app, ["models", "cost", "JPCP", "--record"])

    assert result.exit_code == 0, result.output
    (event,) = _events("project_budget_breach")
    assert event["target"] == "research"


def test_recording_a_cost_under_budget_announces_nothing(monkeypatch):
    from unittest.mock import patch

    from typer.testing import CliRunner

    from examlops.cli.main import app

    set_project_budget("research", None, 1_000_000.0, "monthly", "t")
    monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "mock")
    registered = {"registered_model": {"latest_versions": [{"version": "5", "run_id": "r5"}]}}

    with patch("examlops.cli._client.get", return_value=registered):
        with patch("examlops.cli._client.post", return_value={}):
            CliRunner().invoke(app, ["models", "cost", "JPCP", "--record"])

    assert _events("project_budget_breach") == []


def test_a_governance_write_failure_does_not_fail_the_recording(monkeypatch, capsys):
    from examlops import project_finops

    monkeypatch.setattr(
        project_finops,
        "evaluate_budget",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    from examlops.cli.commands import models as models_cmd

    models_cmd._evaluate_project_budget("JPCP")

    # `_output.warning` goes to stderr, so a `--json` document on stdout stays one document.
    assert "budget check skipped" in capsys.readouterr().err
