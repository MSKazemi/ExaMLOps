"""C7 — shadow deployment & champion-challenger (ADR 0024).

GWT acceptance criteria from ``design/vision/specs/C7-shadow-champion-challenger.md`` §5.
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


def test_gwt1_shadow_records_without_user_impact():
    """GWT-1: shadow prediction is recorded; champion is what the user gets."""
    from examlops import platform_db
    from examlops.champion_challenger import enable_shadow

    enable_shadow("JPCP", "18", 100)
    platform_db.record_challenger_sample(
        "JPCP", request_hash="h1", champion_pred=1.0, challenger_pred=0.9, label=None
    )
    samples = platform_db.get_challenger_samples("JPCP")
    assert len(samples) == 1
    assert samples[0]["champion_pred"] == 1.0
    assert samples[0]["challenger_pred"] == 0.9


def test_gwt2_isolation_shadow_crash_swallowed():
    """GWT-2: a shadow crash never propagates to the production path."""
    from examlops.champion_challenger import run_shadow

    def boom():
        raise RuntimeError("shadow model exploded")

    result, err = run_shadow(boom)
    assert result is None
    assert isinstance(err, RuntimeError)  # captured, not raised


def test_gwt3_side_effect_free_guard():
    """GWT-3: a shadow write attempt is flagged/prevented."""
    from examlops.champion_challenger import ShadowWriteError, guard_write, run_shadow

    # Outside shadow context, writes are allowed.
    guard_write("normal-write")  # no raise

    captured = {}

    def shadow_that_writes():
        guard_write("db-write")  # should raise under shadow context

    _result, err = run_shadow(shadow_that_writes)
    captured["err"] = err
    assert isinstance(captured["err"], ShadowWriteError)


def test_gwt4_scoreboard_delta_p_n():
    """GWT-4: accumulated labelled samples produce delta, p-value, and N."""
    from examlops import platform_db
    from examlops.champion_challenger import challenger_status, enable_shadow

    enable_shadow("JPCP", "18", 100, min_samples=10, alpha=0.05, min_delta=0.05)
    # Champion is wrong by ~1.0 (with spread); challenger is near-perfect (with spread).
    noise = [0.15, -0.1, 0.2, -0.05, 0.08, -0.18, 0.12, -0.09, 0.03, -0.14]
    for i in range(40):
        d = noise[i % len(noise)]
        platform_db.record_challenger_sample(
            "JPCP",
            request_hash=f"h{i}",
            champion_pred=2.0 + d,  # error ~1.0 ± noise
            challenger_pred=1.0 + d * 0.1,  # error ~0.0 ± small noise
            label=1.0,
        )
    st = challenger_status("JPCP")
    assert st is not None
    assert st.n == 40
    assert st.champion_error == pytest.approx(1.0, abs=0.15)
    assert st.challenger_error < 0.2
    assert st.delta > 0.5  # challenger much better
    assert st.p_value is not None and st.p_value < 0.05
    assert st.significant is True


def test_gwt5_promote_on_win_no_slo_regression():
    """GWT-5: policy met with no SLO regression => a promotion is proposed."""
    from examlops import platform_db
    from examlops.champion_challenger import enable_shadow, maybe_promote

    enable_shadow("JPCP", "18", 100, min_samples=10, alpha=0.05, min_delta=0.05)
    noise = [0.15, -0.1, 0.2, -0.05, 0.08, -0.18, 0.12, -0.09, 0.03, -0.14]
    for i in range(30):
        d = noise[i % len(noise)]
        platform_db.record_challenger_sample(
            "JPCP",
            request_hash=f"h{i}",
            champion_pred=2.0 + d,
            challenger_pred=1.0 + d * 0.1,
            label=1.0,
        )
    proposal = maybe_promote("JPCP")
    assert proposal is not None
    assert proposal.challenger_version == "18"
    assert proposal.delta > 0.5

    # Audit event was written (D4).
    with platform_db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action='challenger_promotion_proposed'"
        ).fetchall()
    assert len(rows) == 1


def test_gwt5_slo_regression_blocks_promotion(monkeypatch):
    """GWT-5: an SLO regression blocks the promotion proposal (R6)."""
    from examlops import platform_db
    from examlops.champion_challenger import enable_shadow, maybe_promote

    enable_shadow("JPCP", "18", 100, min_samples=10, alpha=0.05, min_delta=0.05)
    for i in range(30):
        platform_db.record_challenger_sample(
            "JPCP", request_hash=f"h{i}", champion_pred=2.0, challenger_pred=1.0, label=1.0
        )
    # Exhaust an SLO budget for the model (C6).
    from examlops.slo import apply_spec

    apply_spec({"model": "JPCP", "name": "quality", "target": 0.99})
    platform_db.record_slo_sample("JPCP", "quality", good=50, total=100)  # 50% error

    proposal = maybe_promote("JPCP")
    assert proposal is None  # blocked by SLO regression


def test_policy_not_met_insufficient_samples():
    from examlops import platform_db
    from examlops.champion_challenger import challenger_status, enable_shadow

    enable_shadow("JPCP", "18", 100, min_samples=100)
    for i in range(5):
        platform_db.record_challenger_sample(
            "JPCP", request_hash=f"h{i}", champion_pred=2.0, challenger_pred=1.0, label=1.0
        )
    st = challenger_status("JPCP")
    assert st.policy_met is False  # N below threshold


def test_disable_challenger():
    from examlops import platform_db
    from examlops.champion_challenger import enable_shadow

    enable_shadow("JPCP", "18", 100)
    platform_db.disable_challenger("JPCP")
    cfg = platform_db.get_challenger_config("JPCP")
    assert cfg["enabled"] == 0


def test_mirror_pct_clamped():
    from examlops import platform_db
    from examlops.champion_challenger import enable_shadow

    enable_shadow("JPCP", "18", 250)  # over 100
    cfg = platform_db.get_challenger_config("JPCP")
    assert cfg["mirror_pct"] == 100


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.main import app

    runner = CliRunner()
    r1 = runner.invoke(
        app, ["serve", "challenger", "enable", "JPCP", "--version", "18", "--mirror", "50"]
    )
    assert r1.exit_code == 0, r1.output
    for i in range(20):
        platform_db.record_challenger_sample(
            "JPCP", request_hash=f"h{i}", champion_pred=2.0, challenger_pred=1.0, label=1.0
        )
    r2 = runner.invoke(app, ["serve", "challenger", "status", "JPCP"])
    assert r2.exit_code == 0, r2.output
    assert "challenger" in r2.output.lower()
    r3 = runner.invoke(app, ["serve", "challenger", "list"])
    assert r3.exit_code == 0, r3.output
