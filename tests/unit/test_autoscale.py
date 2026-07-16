"""E5 — autoscaling & scale-to-zero (ADR 0031)."""

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


def _policy(**kw):
    from examlops.autoscale import AutoscalePolicy

    return AutoscalePolicy(**kw)


def test_r1_scale_up_on_load():
    from examlops.autoscale import decide_scale

    p = _policy(min_replicas=1, max_replicas=8, target_metric="queue_depth", target_value=10)
    d = decide_scale(1, 45, p, seconds_since_last_scale=1e9)
    assert d.changed
    assert d.desired_replicas == 5  # ceil(45/10)


def test_r1_scale_down_when_idle_metric_low():
    from examlops.autoscale import decide_scale

    p = _policy(min_replicas=1, max_replicas=8, target_value=10)
    d = decide_scale(5, 5, p, seconds_since_last_scale=1e9)
    assert d.changed
    assert d.desired_replicas == 1


def test_r1_clamped_to_max():
    from examlops.autoscale import decide_scale

    p = _policy(max_replicas=4, target_value=10)
    d = decide_scale(1, 1000, p, seconds_since_last_scale=1e9)
    assert d.desired_replicas == 4


def test_r2_stabilization_blocks_change():
    from examlops.autoscale import decide_scale

    p = _policy(target_value=10, stabilization_s=30)
    d = decide_scale(1, 100, p, seconds_since_last_scale=5)  # within stabilization
    assert not d.changed
    assert d.blocked_by == "stabilization"


def test_r2_cooldown_blocks_scale_down():
    from examlops.autoscale import decide_scale

    p = _policy(target_value=10, stabilization_s=0, cooldown_s=60)
    d = decide_scale(5, 5, p, seconds_since_last_scale=30)  # want down, cooldown active
    assert not d.changed
    assert d.blocked_by == "cooldown"


def test_r3_scale_to_zero_on_idle():
    from examlops.autoscale import decide_scale

    p = _policy(min_replicas=1, target_value=10, scale_to_zero_after_s=300, warm_pool=0)
    d = decide_scale(1, 0, p, idle_seconds=400, seconds_since_last_scale=1e9)
    assert d.changed
    assert d.desired_replicas == 0


def test_r5_warm_pool_prevents_zero():
    from examlops.autoscale import decide_scale

    p = _policy(target_value=10, scale_to_zero_after_s=300, warm_pool=1)
    d = decide_scale(2, 0, p, idle_seconds=400, seconds_since_last_scale=1e9)
    assert d.desired_replicas == 1  # warm pool floor


def test_r4_cold_start_measured():
    from examlops import platform_db
    from examlops.autoscale import cold_start_seconds

    platform_db.record_scale_event("JPCP", 0, 1, reason="cold", cold_start_s=3.2)
    platform_db.record_scale_event("JPCP", 0, 1, reason="cold", cold_start_s=4.8)
    assert cold_start_seconds("JPCP") == pytest.approx(4.0)


def test_r7_scale_event_audited():
    from examlops import platform_db
    from examlops.autoscale import ScaleDecision, apply_scale

    apply_scale("JPCP", 1, ScaleDecision(3, 1, "load", True), actor="tester")
    with platform_db.get_db() as conn:
        rows = conn.execute("SELECT * FROM audit_events WHERE action='autoscale_event'").fetchall()
    assert len(rows) == 1


def test_r7_scale_to_zero_savings():
    from examlops import platform_db
    from examlops.autoscale import scale_to_zero_savings, set_policy

    set_policy("JPCP", scale_to_zero_after_s=3600, gpu_fraction=0.5)
    platform_db.record_scale_event("JPCP", 1, 0, reason="idle")
    platform_db.record_scale_event("JPCP", 1, 0, reason="idle")
    s = scale_to_zero_savings("JPCP", gpu_cost_per_hour=2.0)
    assert s["scale_to_zero_events"] == 2
    # 2 events × 1h window × 0.5 fraction = 1.0 GPU-hours × $2 = $2.
    assert s["saved_gpu_hours"] == pytest.approx(1.0)
    assert s["saved_cost"] == pytest.approx(2.0)


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r1 = runner.invoke(
        app,
        [
            "serve",
            "autoscale",
            "set",
            "JPCP",
            "--min",
            "0",
            "--max",
            "8",
            "--target",
            "10",
            "--scale-to-zero-after",
            "300",
        ],
    )
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(
        app, ["serve", "autoscale", "simulate", "JPCP", "--replicas", "2", "--observed", "45"]
    )
    assert r2.exit_code == 0, r2.output
    assert "replicas" in r2.output.lower()
    r3 = runner.invoke(app, ["serve", "autoscale", "status", "JPCP"])
    assert r3.exit_code == 0, r3.output
