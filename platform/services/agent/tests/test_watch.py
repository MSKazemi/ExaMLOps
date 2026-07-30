"""Phase 6 (Skipper next-gen) — monitoring/baseline memory (T3) + skipper-watch (ADR 0104).

Verifies the baseline accessor reads recorded normals, that a cost breach raises an alert fanned
out to the events outbox AND the audit log, that ``--dry-run`` detects without side effects, that a
clean platform raises nothing, and that the drift-signal fan-out path works (with the signal stubbed
so the test doesn't depend on drift-event schema details).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CLI_SRC = Path(__file__).resolve().parents[3] / "platform" / "cli" / "src"
sys.path.insert(0, str(_CLI_SRC))

from skipper import baselines, config, watch  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "watch.db"))
    from examlops.data import init_db

    init_db()
    yield


# ── T3 baseline accessor ──────────────────────────────────────────────────────


def test_whats_normal_reads_recorded_baselines(db):
    from examlops.data.drift import set_drift_baseline

    set_drift_baseline("jpcp", {"mean": 1.0, "std": 0.5})
    normal = baselines.whats_normal("jpcp")
    assert normal["model"] == "jpcp"
    assert normal["drift_baseline"] == {"mean": 1.0, "std": 0.5}
    assert "recent_cost" in normal and "slo_specs" in normal


def test_recall_baseline_tool(db):
    from skipper.tools.baselines import recall_baseline

    out = recall_baseline.invoke({"model": "jpcp"})
    assert "jpcp" in out


# ── skipper-watch fan-out ─────────────────────────────────────────────────────


def test_cost_breach_raises_alert_to_outbox_and_audit(db, monkeypatch):
    monkeypatch.setattr(config, "AGENT_WATCH_COST_BUDGET", 10.0)
    from examlops.data.finops import record_model_cost

    record_model_cost("jpcp", 1, None, None, 5.0, 50.0)  # $50 > $10 budget

    result = watch.run_once()
    assert result["alerts"] == 1
    assert result["detail"][0]["kind"] == "cost"

    # fanned out to the durable outbox …
    from examlops.data import get_db

    with get_db() as conn:
        topics = [r[0] for r in conn.execute("SELECT topic FROM event_outbox").fetchall()]
        audits = [
            r[0]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE source='skipper-watch'"
            ).fetchall()
        ]
    assert "alert.cost" in topics
    assert "alert_raised" in audits


def test_dry_run_has_no_side_effects(db, monkeypatch):
    monkeypatch.setattr(config, "AGENT_WATCH_COST_BUDGET", 10.0)
    from examlops.data.finops import record_model_cost

    record_model_cost("jpcp", 1, None, None, 5.0, 50.0)

    result = watch.run_once(dry_run=True)
    assert result["alerts"] == 1  # detected …
    from examlops.data import get_db

    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='skipper-watch'"
        ).fetchone()[0]
    assert n == 0  # … but raised nothing


def test_no_breach_when_under_budget(db, monkeypatch):
    monkeypatch.setattr(config, "AGENT_WATCH_COST_BUDGET", 1000.0)
    from examlops.data.finops import record_model_cost

    record_model_cost("jpcp", 1, None, None, 5.0, 50.0)
    assert watch.run_once()["alerts"] == 0


def test_drift_fanout_and_critical_exit(db, monkeypatch):
    # Stub the drift signal to a critical breach; assert the fan-out + critical severity.
    monkeypatch.setattr(config, "AGENT_WATCH_COST_BUDGET", 0)
    monkeypatch.setattr(
        watch,
        "_drift_breaches",
        lambda: [
            {
                "kind": "drift",
                "target": "jpcp",
                "severity": "critical",
                "detail": "jpcp prediction drift z=6.0",
                "value": 6.0,
                "threshold": 3.0,
            }
        ],
    )
    result = watch.run_once()
    assert result["alerts"] == 1
    from examlops.data import get_db

    with get_db() as conn:
        topics = [r[0] for r in conn.execute("SELECT topic FROM event_outbox").fetchall()]
    assert "alert.drift" in topics
    # module CLI returns non-zero on a critical alert (usable as a CI gate)
    assert watch._main(["--once"]) == 1
