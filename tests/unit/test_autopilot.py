"""Unit tests for the self-driving MLOps autopilot (ADR 0085).

Tests cover:
- Kill-switch (env var + DB config)
- Drift scan + auto-retrain gate
- Policy check for autopilot_trigger
- Promotion metric gate + policy check for autopilot_promote
- Dry-run mode (no external calls)
- Audit event writing
- platform_db autopilot helpers
- CLI commands (run / status / enable / disable)
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from examlops.cli.commands import autopilot_cmd
from examlops.cli.main import app
from examlops.data.drift import set_corruption_baseline, set_input_baseline, write_input_snapshot
from examlops.platform_db import (
    create_autopilot_run,
    get_autopilot_config,
    init_db,
    list_autopilot_runs,
    set_autopilot_config,
    set_drift_auto_retrain,
    set_drift_baseline,
    set_promotion_rule,
    update_autopilot_run,
    write_drift_snapshot,
)

runner = CliRunner()

JPCP = "JPCP"


def seed_data_drift_evidence(model: str, preds: list[float] | None = None) -> None:
    """Give `model` the *second* axis ADR 0114 requires before an autonomous retrain.

    Prediction drift alone now classifies as `undetermined` — one axis cannot separate data
    drift from a serving regression — so a test that means "this model is genuinely drifting"
    has to say so on both axes. Inputs are placed 4σ from their baseline (real data drift),
    and the corruption baseline matches the predictions (corruption negative).
    """
    from examlops.corruption import corruption_stats

    set_corruption_baseline(model, corruption_stats(preds if preds else [5.0] * 10))
    set_input_baseline(
        model,
        {
            "norm_mean": 1.0,
            "norm_mean_std": 0.1,
            "mean_mean": 0.0,
            "mean_mean_std": 0.1,
            "std_mean": 1.0,
            "std_mean_std": 0.1,
        },
    )
    for _ in range(50):
        write_input_snapshot(model, "Production", 1.4, 0.0, 1.0, None)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_AUTOPILOT_ENABLED", raising=False)
    init_db()
    # ADR 0113: an autonomous retrain is refused unless the cycle can name the version a
    # rollback would restore, which needs MLflow. These tests are about the cycle's other
    # behaviour — policy, cooldown, storm cap — so the precondition is supplied rather than
    # re-asserted here; the gate itself is exercised in test_rollback_registry.py, including
    # the case where it cannot be resolved and the retrain is declined.
    monkeypatch.setattr(autopilot_cmd, "_alias_version", lambda model, alias="Production": "4")


# ── platform_db helpers ───────────────────────────────────────────────────────


class TestAutopilotDB:
    def test_config_get_missing(self):
        assert get_autopilot_config("enabled") is None

    def test_config_set_and_get(self):
        set_autopilot_config("enabled", "1")
        assert get_autopilot_config("enabled") == "1"

    def test_config_upsert(self):
        set_autopilot_config("enabled", "1")
        set_autopilot_config("enabled", "0")
        assert get_autopilot_config("enabled") == "0"

    def test_create_run_returns_id(self):
        run_id = create_autopilot_run()
        assert isinstance(run_id, int)
        assert run_id > 0

    def test_update_run(self):
        run_id = create_autopilot_run()
        update_autopilot_run(
            run_id,
            retrains_triggered=2,
            promotions_made=1,
            policy_blocks=0,
            human_required=1,
            skipped=3,
            summary={"retrains": ["JPCP"]},
        )
        runs = list_autopilot_runs(last_n=1)
        assert runs[0]["retrains_triggered"] == 2
        assert runs[0]["promotions_made"] == 1
        assert runs[0]["human_required"] == 1
        assert runs[0]["skipped"] == 3

    def test_list_runs_ordered_newest_first(self):
        create_autopilot_run()
        create_autopilot_run()
        create_autopilot_run()
        runs = list_autopilot_runs(last_n=3)
        ids = [r["id"] for r in runs]
        assert ids == sorted(ids, reverse=True)

    def test_list_runs_respects_last_n(self):
        for _ in range(5):
            create_autopilot_run()
        assert len(list_autopilot_runs(last_n=3)) == 3


# ── kill-switch ───────────────────────────────────────────────────────────────


class TestKillSwitch:
    def test_disabled_by_default(self):
        assert autopilot_cmd._is_enabled() is False

    def test_enabled_via_env_truthy(self, monkeypatch):
        for val in ("1", "true", "yes", "on", "TRUE"):
            monkeypatch.setenv("EXAMLOPS_AUTOPILOT_ENABLED", val)
            assert autopilot_cmd._is_enabled() is True

    def test_disabled_via_env_falsy(self, monkeypatch):
        for val in ("0", "false", "no", "off", "FALSE"):
            monkeypatch.setenv("EXAMLOPS_AUTOPILOT_ENABLED", val)
            assert autopilot_cmd._is_enabled() is False

    def test_enabled_via_db(self):
        set_autopilot_config("enabled", "1")
        assert autopilot_cmd._is_enabled() is True

    def test_disabled_via_db(self):
        set_autopilot_config("enabled", "0")
        assert autopilot_cmd._is_enabled() is False

    def test_env_takes_precedence_over_db(self, monkeypatch):
        set_autopilot_config("enabled", "1")
        monkeypatch.setenv("EXAMLOPS_AUTOPILOT_ENABLED", "0")
        assert autopilot_cmd._is_enabled() is False


# ── run_cycle: kill-switch ────────────────────────────────────────────────────


class TestRunCycleKillSwitch:
    def test_disabled_returns_early(self):
        result = autopilot_cmd.run_cycle()
        assert result.get("enabled") is False
        assert "disabled" in result.get("reason", "").lower()

    def test_disabled_writes_audit_event(self):
        from examlops.platform_db import get_db

        autopilot_cmd.run_cycle()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE action='autopilot_skipped'"
            ).fetchall()
        assert len(rows) == 1

    # A dry-run changes nothing, so the switch must not gate it: requiring `enable` first would
    # mean arming the loop in order to preview it.

    def test_dry_run_previews_while_disabled(self):
        result = autopilot_cmd.run_cycle(dry_run=True)
        assert result.get("enabled", True) is not False, "a dry-run must not be refused"
        assert result["dry_run"] is True
        assert result["kill_switch_enabled"] is False, "and must say the switch is off"
        assert "run_id" in result

    def test_dry_run_while_disabled_is_recorded_as_disabled(self):
        # History must never imply the loop was armed when it was not.
        from examlops.data.autopilot import list_autopilot_runs

        autopilot_cmd.run_cycle(dry_run=True)
        runs = list_autopilot_runs(last_n=5)
        assert runs, "the preview is still recorded"
        assert runs[0]["enabled_state"] == "disabled"
        assert runs[0]["dry_run"] in (1, True)

    def test_dry_run_while_disabled_takes_no_lease(self):
        from examlops.platform_db import get_db

        autopilot_cmd.run_cycle(dry_run=True)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE action='autopilot_skipped'"
            ).fetchall()
        assert rows == [], "a preview is not a skipped cycle"

    def test_a_live_run_is_still_refused_while_disabled(self):
        # The whole point of the switch. Widening it to dry-run must not widen it to anything else.
        with patch.object(autopilot_cmd, "_call_retrain") as mock_retrain:
            result = autopilot_cmd.run_cycle(dry_run=False)
        assert result.get("enabled") is False
        mock_retrain.assert_not_called()


# ── run_cycle: drift trigger path ─────────────────────────────────────────────


class TestRunCycleDriftTrigger:
    def setup_method(self):
        # Enable autopilot
        set_autopilot_config("enabled", "1")
        # Configure auto-retrain for JPCP
        set_drift_auto_retrain(
            JPCP, enabled=True, min_z_score=2.0, dataset_name="PM100Dataset", cooldown_s=0
        )
        # Set baseline
        set_drift_baseline(JPCP, {"mean": 1.0, "std": 0.1})
        # Write high-drift snapshots (mean far from baseline)
        for _ in range(10):
            write_drift_snapshot(JPCP, "Production", 5.0, None)
        seed_data_drift_evidence(JPCP)

    def test_dry_run_does_not_call_retrain(self):
        with patch.object(autopilot_cmd, "_call_retrain") as mock_retrain:
            result = autopilot_cmd.run_cycle(dry_run=True)
        mock_retrain.assert_not_called()
        assert len(result["retrains"]) == 1
        assert result["retrains"][0]["action"] == "would retrain"

    def test_live_run_calls_retrain(self):
        with patch.object(
            autopilot_cmd, "_call_retrain", return_value={"flow_run_id": "abc123"}
        ) as mock_retrain:
            result = autopilot_cmd.run_cycle()
        mock_retrain.assert_called_once()
        assert result["retrains"][0]["flow_run_id"] == "abc123"

    def test_no_snapshots_skips_model(self):
        from examlops.platform_db import get_db

        # Remove all snapshots
        with get_db() as conn:
            conn.execute("DELETE FROM drift_snapshots")
        result = autopilot_cmd.run_cycle()
        skipped_models = [s["model"] for s in result["skipped"]]
        assert JPCP in skipped_models

    def test_below_threshold_skips(self):
        # Set low z (normal drift) by making baseline mean match predictions
        set_drift_baseline(JPCP, {"mean": 5.0, "std": 0.1})
        result = autopilot_cmd.run_cycle(dry_run=True)
        skipped_models = [s["model"] for s in result["skipped"]]
        assert JPCP in skipped_models

    def test_retrain_error_recorded_in_skipped(self):
        with patch.object(autopilot_cmd, "_call_retrain", side_effect=Exception("timeout")):
            result = autopilot_cmd.run_cycle()
        skipped_models = [s["model"] for s in result["skipped"]]
        assert JPCP in skipped_models

    def test_writes_audit_event_on_trigger(self):
        from examlops.platform_db import get_db

        with patch.object(autopilot_cmd, "_call_retrain", return_value={}):
            autopilot_cmd.run_cycle()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE action='autopilot_retrain_triggered'"
            ).fetchall()
        assert len(rows) == 1


# ── run_cycle: policy integration ─────────────────────────────────────────────


class TestRunCycleCorruptionSuppression:
    """ADR 0114 — the autopilot promotes on its own road, so the suppression has to hold
    here too. Without this the closed loop would be the one path that can still retrain a
    model on a hardware fault."""

    def setup_method(self):
        set_autopilot_config("enabled", "1")
        set_drift_auto_retrain(
            JPCP, enabled=True, min_z_score=2.0, dataset_name="PM100Dataset", cooldown_s=0
        )
        set_drift_baseline(JPCP, {"mean": 1.0, "std": 0.1})

    def _seed_corrupt_predictions(self) -> None:
        from examlops.corruption import corruption_stats, inject_nullification

        clean = [5.0 + (i % 5) * 0.01 for i in range(200)]
        set_corruption_baseline(JPCP, corruption_stats(clean))
        for value in inject_nullification(clean, 0.30, seed=11):
            write_drift_snapshot(JPCP, "Production", value, None)
        seed_data_drift_evidence(JPCP, clean)

    def test_corruption_suppresses_the_cycle_retrain(self):
        self._seed_corrupt_predictions()
        with patch.object(autopilot_cmd, "_call_retrain") as mock_retrain:
            result = autopilot_cmd.run_cycle()
        mock_retrain.assert_not_called()
        assert result["retrains"] == []
        assert result["suppressed"], "the cycle suppressed nothing and recorded nothing"
        assert result["suppressed"][0]["class"] == "suspected_hardware"

    def test_the_suppression_is_recorded(self):
        from examlops.data import get_db
        from examlops.data.drift import list_drift_events

        self._seed_corrupt_predictions()
        with patch.object(autopilot_cmd, "_call_retrain"):
            autopilot_cmd.run_cycle()
        events = list_drift_events(model=JPCP, drift_kind="corruption")
        assert events and events[0]["detail"]["class"] == "suspected_hardware"
        with get_db() as conn:
            audit = conn.execute(
                "SELECT * FROM audit_events WHERE action='autopilot_retrain_suppressed'"
            ).fetchall()
        assert audit, "a retrain that does not happen left no trace at all"

    def test_history_shows_the_suppression(self):
        """A cycle that suppressed a model must not read like a quiet one — that is the
        failure mode of every guard whose only success signal is silence.

        Asserted on the recorded run rather than on the rendered table: rich truncates the
        header at the 80-column test terminal, and a test that fails on terminal width is
        testing the terminal.
        """
        self._seed_corrupt_predictions()
        with patch.object(autopilot_cmd, "_call_retrain"):
            autopilot_cmd.run_cycle()
        result = runner.invoke(app, ["--json", "autopilot", "status"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        recorded = json.loads(payload["runs"][0]["summary"])
        assert recorded["suppressed"], "the run history records no suppression at all"
        assert recorded["retrains"] == []
        assert autopilot_cmd._suppressed_count(list_autopilot_runs(last_n=1)[0]) == 1

    def test_suppressed_count_survives_a_run_with_no_summary(self):
        assert autopilot_cmd._suppressed_count({"summary": None}) == 0
        assert autopilot_cmd._suppressed_count({"summary": "not json"}) == 0

    def test_reverting_the_guard_makes_the_retrain_fire_again(self):
        """The bug must return, or the two tests above prove nothing about the guard."""
        self._seed_corrupt_predictions()
        with (
            patch.object(autopilot_cmd, "_classify_anomaly_for", return_value=None),
            patch.object(autopilot_cmd, "_call_retrain", return_value={"flow_run_id": "x"}) as m,
        ):
            autopilot_cmd.run_cycle()
        assert m.call_count == 1


class TestRunCyclePolicy:
    def setup_method(self):
        set_autopilot_config("enabled", "1")
        set_drift_auto_retrain(
            JPCP, enabled=True, min_z_score=2.0, dataset_name="PM100Dataset", cooldown_s=0
        )
        set_drift_baseline(JPCP, {"mean": 1.0, "std": 0.1})
        for _ in range(10):
            write_drift_snapshot(JPCP, "Production", 5.0, None)
        seed_data_drift_evidence(JPCP)

    def test_policy_deny_blocks_retrain(self):
        with patch.object(
            autopilot_cmd, "_policy_decide", return_value=("deny", "blocked by policy")
        ):
            result = autopilot_cmd.run_cycle(dry_run=True)
        assert len(result["policy_blocks"]) == 1
        assert result["policy_blocks"][0]["model"] == JPCP
        assert result["retrains"] == []

    def test_policy_require_approval_routes_to_hitl(self):
        with patch.object(
            autopilot_cmd, "_policy_decide", return_value=("require_approval", "needs approval")
        ):
            result = autopilot_cmd.run_cycle(dry_run=True)
        assert len(result["human_required"]) == 1
        assert result["human_required"][0]["model"] == JPCP
        assert result["retrains"] == []

    def test_policy_deny_writes_audit_event(self):
        from examlops.platform_db import get_db

        with patch.object(autopilot_cmd, "_policy_decide", return_value=("deny", "blocked")):
            autopilot_cmd.run_cycle(dry_run=True)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE action='policy_denied'"
            ).fetchall()
        assert len(rows) >= 1

    def test_policy_hitl_writes_audit_event(self):
        from examlops.platform_db import get_db

        with patch.object(autopilot_cmd, "_policy_decide", return_value=("require_approval", "")):
            autopilot_cmd.run_cycle(dry_run=True)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE action='human_approval_required'"
            ).fetchall()
        assert len(rows) >= 1


# ── run_cycle: promotion path ─────────────────────────────────────────────────


class TestRunCyclePromotion:
    def setup_method(self):
        set_autopilot_config("enabled", "1")
        set_promotion_rule(JPCP, "rmse", "lt", 5.0, "Staging", "Production")

    def test_dry_run_promotion_does_not_call_promote(self):
        fake_metrics = {"rmse": 3.0}
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value=fake_metrics):
            with patch.object(autopilot_cmd, "_do_promote") as mock_promote:
                result = autopilot_cmd.run_cycle(dry_run=True)
        mock_promote.assert_not_called()
        assert any(p["action"] == "would promote" for p in result["promotions"])

    def test_live_promotion_calls_do_promote(self):
        fake_metrics = {"rmse": 3.0}
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value=fake_metrics):
            with patch.object(autopilot_cmd, "_do_promote") as mock_promote:
                result = autopilot_cmd.run_cycle()
        mock_promote.assert_called_once()
        assert result["promotions"][0]["promoted_to"] == "Production"

    def test_metric_gate_fails_skips_promotion(self):
        # rmse=8.0 fails threshold of < 5.0
        fake_metrics = {"rmse": 8.0}
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value=fake_metrics):
            with patch.object(autopilot_cmd, "_do_promote") as mock_promote:
                result = autopilot_cmd.run_cycle()
        mock_promote.assert_not_called()
        assert any("does not pass" in s["reason"] for s in result["skipped"])

    def test_no_staging_version_skips(self):
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value=None):
            result = autopilot_cmd.run_cycle()
        assert any(JPCP.upper() == s["model"].upper() for s in result["skipped"])

    def test_missing_metric_skips(self):
        # Staging run has no 'rmse' metric
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"accuracy": 0.9}):
            result = autopilot_cmd.run_cycle()
        skipped_reasons = [s["reason"] for s in result["skipped"]]
        assert any("rmse" in r for r in skipped_reasons)

    def test_promote_error_recorded_in_skipped(self):
        fake_metrics = {"rmse": 3.0}
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value=fake_metrics):
            with patch.object(autopilot_cmd, "_do_promote", side_effect=Exception("mlflow down")):
                result = autopilot_cmd.run_cycle()
        assert any("promote error" in s["reason"] for s in result["skipped"])

    def test_promote_policy_deny_blocks(self):
        fake_metrics = {"rmse": 3.0}
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value=fake_metrics):
            with patch.object(autopilot_cmd, "_policy_decide", return_value=("deny", "blocked")):
                result = autopilot_cmd.run_cycle()
        assert len(result["policy_blocks"]) >= 1

    def test_promote_writes_audit_event(self):
        from examlops.platform_db import get_db

        fake_metrics = {"rmse": 3.0}
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value=fake_metrics):
            with patch.object(autopilot_cmd, "_do_promote"):
                autopilot_cmd.run_cycle()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE action='autopilot_promoted'"
            ).fetchall()
        assert len(rows) >= 1


# ── model_filter ──────────────────────────────────────────────────────────────


class TestModelFilter:
    def setup_method(self):
        set_autopilot_config("enabled", "1")

    def test_filter_restricts_to_model(self):
        set_drift_auto_retrain(
            "MACK", enabled=True, min_z_score=2.0, dataset_name="D", cooldown_s=0
        )
        set_drift_baseline("MACK", {"mean": 1.0, "std": 0.1})
        for _ in range(10):
            write_drift_snapshot("MACK", "Production", 5.0, None)
        seed_data_drift_evidence("MACK")
        set_drift_auto_retrain(JPCP, enabled=True, min_z_score=2.0, dataset_name="D", cooldown_s=0)
        set_drift_baseline(JPCP, {"mean": 1.0, "std": 0.1})
        for _ in range(10):
            write_drift_snapshot(JPCP, "Production", 5.0, None)
        seed_data_drift_evidence(JPCP)

        with patch.object(autopilot_cmd, "_call_retrain", return_value={}):
            result = autopilot_cmd.run_cycle(model_filter="MACK")

        triggered_models = [r["model"] for r in result["retrains"]]
        assert "MACK" in triggered_models
        assert JPCP not in triggered_models

    def test_filter_to_unknown_model_produces_skip(self):
        result = autopilot_cmd.run_cycle(model_filter="NOEXIST")
        skipped_models = [s["model"] for s in result["skipped"]]
        assert "NOEXIST" in skipped_models


# ── autopilot_runs record ─────────────────────────────────────────────────────


class TestRunRecord:
    def test_cycle_complete_audit_event_written(self):
        from examlops.platform_db import get_db

        set_autopilot_config("enabled", "1")
        autopilot_cmd.run_cycle(dry_run=True)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE action='autopilot_cycle_complete'"
            ).fetchall()
        assert len(rows) == 1

    def test_run_record_created(self):
        set_autopilot_config("enabled", "1")
        autopilot_cmd.run_cycle(dry_run=True)
        runs = list_autopilot_runs()
        assert len(runs) >= 1

    def test_run_record_counts_accurate(self):
        set_autopilot_config("enabled", "1")
        set_promotion_rule(JPCP, "rmse", "lt", 5.0)
        fake_metrics = {"rmse": 3.0}
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value=fake_metrics):
            with patch.object(autopilot_cmd, "_do_promote"):
                autopilot_cmd.run_cycle()
        runs = list_autopilot_runs(last_n=1)
        assert runs[0]["promotions_made"] == 1


# ── CLI commands ──────────────────────────────────────────────────────────────


class TestAutopilotCLI:
    def test_run_disabled(self):
        result = runner.invoke(app, ["autopilot", "run", "--dry-run"])
        assert result.exit_code == 0
        assert "disabled" in result.output.lower()

    def test_enable(self):
        result = runner.invoke(app, ["autopilot", "enable"])
        assert result.exit_code == 0
        assert get_autopilot_config("enabled") == "1"

    def test_disable(self):
        set_autopilot_config("enabled", "1")
        result = runner.invoke(app, ["autopilot", "disable"])
        assert result.exit_code == 0
        assert get_autopilot_config("enabled") == "0"

    def test_status_shows_enabled_state(self):
        set_autopilot_config("enabled", "1")
        result = runner.invoke(app, ["autopilot", "status"])
        assert result.exit_code == 0
        assert "ENABLED" in result.output

    def test_status_shows_disabled_state(self):
        result = runner.invoke(app, ["autopilot", "status"])
        assert result.exit_code == 0
        assert "DISABLED" in result.output

    def test_status_json(self):
        set_autopilot_config("enabled", "1")
        result = runner.invoke(app, ["--json", "autopilot", "status"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["enabled"] is True

    def test_run_dry_run_no_retrain_calls(self):
        set_autopilot_config("enabled", "1")
        with patch.object(autopilot_cmd, "_call_retrain") as mock_retrain:
            result = runner.invoke(app, ["autopilot", "run", "--dry-run"])
        mock_retrain.assert_not_called()
        assert result.exit_code == 0

    def test_run_json_output(self):
        set_autopilot_config("enabled", "1")
        result = runner.invoke(app, ["--json", "autopilot", "run", "--dry-run"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "run_id" in data
        assert "retrains" in data


# ── run_cycle: per-cycle retrain storm cap ────────────────────────────────────
#
# The cap is one of the things that makes turning the loop on a bounded risk, and until now
# nothing tested it. Measuring it turned up that it applied to a live run only: with three
# drifting models and a cap of one, `--dry-run` promised three retrains where the cycle it was
# previewing did one.


class TestRunCycleStormCap:
    MODELS = ("MODA", "MODB", "MODC")

    def setup_method(self):
        set_autopilot_config("enabled", "1")
        for m in self.MODELS:
            set_drift_auto_retrain(
                m, enabled=True, min_z_score=2.0, dataset_name="PM100Dataset", cooldown_s=0
            )
            set_drift_baseline(m, {"mean": 1.0, "std": 0.1})
            for _ in range(10):
                write_drift_snapshot(m, "Production", 5.0, None)
            seed_data_drift_evidence(m)

    def _cap(self, monkeypatch, n):
        monkeypatch.setenv("EXAMLOPS_AUTOPILOT_MAX_RETRAINS", str(n))

    def test_live_cycle_is_bounded_by_the_cap(self, monkeypatch):
        self._cap(monkeypatch, 1)
        with patch.object(
            autopilot_cmd, "_call_retrain", return_value={"flow_run_id": "x"}
        ) as mock_retrain:
            result = autopilot_cmd.run_cycle()
        assert len(result["retrains"]) == 1
        assert mock_retrain.call_count == 1, "the cap must stop the call, not just the report"
        capped = [s for s in result["skipped"] if "cap" in s["reason"]]
        assert len(capped) == 2, "and the two it stopped must be reported, not silently dropped"

    def test_preview_reports_the_same_count_as_the_cycle_it_previews(self, monkeypatch):
        self._cap(monkeypatch, 1)
        dry = autopilot_cmd.run_cycle(dry_run=True)
        with patch.object(autopilot_cmd, "_call_retrain", return_value={"flow_run_id": "x"}):
            live = autopilot_cmd.run_cycle()
        assert [r["model"] for r in dry["retrains"]] == [r["model"] for r in live["retrains"]]
        assert sorted(s["model"] for s in dry["skipped"] if "cap" in s["reason"]) == sorted(
            s["model"] for s in live["skipped"] if "cap" in s["reason"]
        )

    def test_a_cap_above_the_workload_stops_nothing(self, monkeypatch):
        self._cap(monkeypatch, 10)
        dry = autopilot_cmd.run_cycle(dry_run=True)
        assert len(dry["retrains"]) == 3
        assert [s for s in dry["skipped"] if "cap" in s["reason"]] == []


# ── run_cycle: ADR 0111 — an unmeasured judge may not promote ─────────────────
#
# The autopilot promotes without going through run_eval_gate, so the calibration refusal is
# enforced on this road separately. That enforcement had no test: `judge_eligibility_for_model`
# appeared in no test file at all, which for a rule whose whole point is "absence of calibration
# is not eligibility" is the wrong thing to take on trust.


class TestRunCycleJudgeEligibility:
    def setup_method(self):
        set_autopilot_config("enabled", "1")
        set_promotion_rule(JPCP, "rmse", "lt", 5.0, "Staging", "Production")

    def test_uncalibrated_judge_blocks_the_promote(self):
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"rmse": 3.0}):
            with (
                patch.object(autopilot_cmd, "_do_promote") as mock_promote,
                patch(
                    "examlops.evaluation.gate.judge_eligibility_for_model",
                    return_value=(False, ["no_calibration"], "gpt-judge"),
                ),
            ):
                result = autopilot_cmd.run_cycle()
        mock_promote.assert_not_called(), "an unmeasured judge must not reach production"
        assert any("not gate-eligible" in b["reason"] for b in result["policy_blocks"])

    def test_the_block_is_audited_with_the_judge_and_the_adr(self):
        from examlops.platform_db import get_db

        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"rmse": 3.0}):
            with (
                patch.object(autopilot_cmd, "_do_promote"),
                patch(
                    "examlops.evaluation.gate.judge_eligibility_for_model",
                    return_value=(False, ["position_bias"], "gpt-judge"),
                ),
            ):
                autopilot_cmd.run_cycle()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT details FROM audit_events WHERE action='autopilot_promote_blocked'"
            ).fetchall()
        assert len(rows) == 1
        details = rows[0]["details"]
        assert "gpt-judge" in details and "position_bias" in details and "0111" in details

    def test_a_model_with_no_judge_is_not_blocked(self):
        # No eval gate configured → nothing claims a judge decides → the promote proceeds.
        with patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"rmse": 3.0}):
            with patch.object(autopilot_cmd, "_do_promote") as mock_promote:
                autopilot_cmd.run_cycle()
        mock_promote.assert_called_once()


# ── run_cycle: C3 — the eval regression gate guards this road too ─────────────
#
# `exa eval gate set --mode block` reads as "this gate guards promotion of this model". It
# guarded `exa pipeline promote` and not the autopilot, which promotes the same model to the
# same alias — so the closed loop was the one road to Production that never met it. The
# promotion *rule* is an absolute threshold on one metric; only the C3 gate compares a
# candidate against the baseline alias, which is precisely the check a self-driving loop needs.


class TestRunCycleEvalGate:
    def setup_method(self):
        set_autopilot_config("enabled", "1")
        set_promotion_rule(JPCP, "rmse", "lt", 5.0, "Staging", "Production")
        from examlops.data.evaluation import record_eval_result, set_eval_gate

        # `higher_is_better` is spelled out per metric on purpose. Both roads derive the gate's
        # *default* direction from the promotion rule's operator — here `rmse lt`, i.e. lower is
        # better — which says nothing about the direction of the suite's own metrics. Pass 219
        # made that expressible; the default is still inferred from an unrelated comparison and
        # is worth its own pass.
        set_eval_gate(
            JPCP,
            "smoke",
            [{"name": "accuracy", "max_drop": 0.01, "higher_is_better": True}],
            mode="block",
        )
        # Baseline 0.95 in Production, candidate 0.80 in Staging: the promotion rule's own
        # metric (rmse 3.0 < 5.0) passes happily while accuracy has fallen off a cliff.
        record_eval_result(
            "smoke",
            JPCP,
            run_id="base",
            scores={"accuracy": 0.95},
            model_version="1",
            alias="Production",
        )
        record_eval_result(
            "smoke", JPCP, run_id="cand", scores={"accuracy": 0.80}, model_version="2"
        )

    def test_a_failing_block_mode_gate_stops_the_promote(self):
        with (
            patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"rmse": 3.0}),
            patch.object(autopilot_cmd, "_staging_version", return_value="2"),
            patch.object(autopilot_cmd, "_do_promote") as mock_promote,
        ):
            result = autopilot_cmd.run_cycle()
        mock_promote.assert_not_called(), "a block-mode gate must stop the autopilot too"
        assert any("accuracy" in b["reason"] for b in result["policy_blocks"])

    def test_warn_mode_records_but_does_not_stop_it(self):
        from examlops.data.evaluation import set_eval_gate

        set_eval_gate(
            JPCP,
            "smoke",
            [{"name": "accuracy", "max_drop": 0.01, "higher_is_better": True}],
            mode="warn",
        )
        with (
            patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"rmse": 3.0}),
            patch.object(autopilot_cmd, "_staging_version", return_value="2"),
            patch.object(autopilot_cmd, "_do_promote") as mock_promote,
        ):
            autopilot_cmd.run_cycle()
        mock_promote.assert_called_once(), "warn mode is advisory on this road as on the other"

    def test_a_gate_that_cannot_be_evaluated_does_not_promote_anyway(self):
        """Configured but unevaluable is not the same as passed.

        If the Staging version cannot be resolved there is nothing to compare against the
        baseline, and promoting because the check could not run is the failure this gate exists
        to prevent.
        """
        with (
            patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"rmse": 3.0}),
            patch.object(autopilot_cmd, "_staging_version", return_value=None),
            patch.object(autopilot_cmd, "_do_promote") as mock_promote,
        ):
            result = autopilot_cmd.run_cycle()
        mock_promote.assert_not_called()
        assert any("could not" in b["reason"].lower() for b in result["policy_blocks"])


# ── the gate itself, unmocked ─────────────────────────────────────────────────
#
# Every policy test above patches `_policy_decide`, so its body never ran in the suite. It read
# `decision.action` — a field `Decision` has never had — so the AttributeError landed in its own
# `except` and returned "allow" for everything. Both autopilot gates were dead, fail-open, and
# green. These call the real function.


class TestPolicyDecideItself:
    """`_policy_decide` against real `Decision` objects — no patching of the thing under test."""

    def _with_rules(self, monkeypatch, rules):
        import examlops.policy as policy

        monkeypatch.setattr(policy, "_load_policies", lambda path=None: rules)

    def test_a_deny_rule_is_reported_as_deny(self, monkeypatch):
        self._with_rules(
            monkeypatch, [{"name": "no-auto", "action": "autopilot_trigger", "effect": "deny"}]
        )
        effect, reason = autopilot_cmd._policy_decide("autopilot_trigger", {"model": JPCP})
        assert effect == "deny"
        assert "no-auto" in reason

    def test_require_approval_survives_the_round_trip(self, monkeypatch):
        """The HITL hold on the one component that acts without a human."""
        self._with_rules(
            monkeypatch,
            [{"name": "humans", "action": "autopilot_promote", "effect": "require_approval"}],
        )
        effect, _ = autopilot_cmd._policy_decide("autopilot_promote", {"model": JPCP})
        assert effect == "require_approval"

    def test_no_policy_still_allows(self, monkeypatch):
        self._with_rules(monkeypatch, [])
        effect, _ = autopilot_cmd._policy_decide("autopilot_trigger", {"model": JPCP})
        assert effect == "allow"

    def test_the_effect_it_returns_is_one_the_cycle_acts_on(self, monkeypatch):
        """A typo'd field name returned a string the cycle simply never compares against.

        `run_cycle` branches on the literals "deny" and "require_approval"; anything else means
        allow. So a wrong-but-truthy return value fails open *and* silently — pin the vocabulary.
        """
        for effect in ("deny", "require_approval", "allow"):
            self._with_rules(
                monkeypatch, [{"name": "r", "action": "autopilot_trigger", "effect": effect}]
            )
            got, _ = autopilot_cmd._policy_decide("autopilot_trigger", {"model": JPCP})
            assert got == effect

    def test_a_broken_policy_layer_fails_closed_and_says_why(self, monkeypatch):
        """ADR 0079 decision 2 only ever decided "no file/no match → allow" — a config state,
        handled inside `decide()` itself, which never raises for it. It never decided what an
        autopilot cycle should do if the engine *code* raises (a bug, not a config problem); this
        used to reuse the same "allow" for both, which is what let the autopilot fire a real
        retrain with its policy gates silently disabled (BL-080). Autopilot acts with no human at
        the keyboard — same reasoning as the MCP write-gate, which already failed closed — so a
        raising engine now denies here too, and (unlike before) is durably audited, not just logged.
        """
        import examlops.policy as policy

        def boom(*a, **k):
            raise RuntimeError("policy exploded")

        monkeypatch.setattr(policy, "decide", boom)
        effect, reason = autopilot_cmd._policy_decide("autopilot_trigger", {"model": JPCP})
        assert effect == "deny"
        assert "policy exploded" in reason

    def test_a_broken_policy_layer_is_durably_audited_not_just_logged(self, monkeypatch):
        """The gap `test_a_broken_policy_layer_fails_closed_and_says_why` doesn't cover: BL-080
        found the old fallback wrote nothing to `audit_events` — only a Python log line, which a
        governance review of the audit chain would never see."""
        import json

        import examlops.policy as policy
        from examlops.platform_db import get_db

        def boom(*a, **k):
            raise RuntimeError("policy exploded")

        monkeypatch.setattr(policy, "decide", boom)
        autopilot_cmd._policy_decide("autopilot_trigger", {"model": JPCP})

        with get_db() as conn:
            rows = conn.execute(
                "SELECT details FROM audit_events WHERE action='policy_unavailable:autopilot_trigger'"
            ).fetchall()
        assert rows, "no audit_events row recorded when the policy engine raised"
        details = json.loads(rows[0]["details"])
        assert "policy exploded" in details["error"]
        assert details["default_effect"] == "deny"


class TestPolicyReachesTheRealCycle:
    """End-to-end: a deny rule in the policy layer must stop a drifting model's retrain."""

    def setup_method(self):
        set_autopilot_config("enabled", "1")
        set_drift_auto_retrain(
            JPCP, enabled=True, min_z_score=2.0, dataset_name="PM100Dataset", cooldown_s=0
        )
        set_drift_baseline(JPCP, {"mean": 1.0, "std": 0.1})
        for _ in range(10):
            write_drift_snapshot(JPCP, "Production", 5.0, None)
        seed_data_drift_evidence(JPCP)

    def test_a_real_deny_rule_blocks_the_cycle(self, monkeypatch):
        import examlops.policy as policy

        monkeypatch.setattr(
            policy,
            "_load_policies",
            lambda path=None: [
                {"name": "no-auto", "action": "autopilot_trigger", "effect": "deny"}
            ],
        )
        result = autopilot_cmd.run_cycle(dry_run=True)
        assert result["retrains"] == [] or len(result["retrains"]) == 0
        assert len(result["policy_blocks"]) == 1
        assert result["policy_blocks"][0]["gate"] == "autopilot_trigger"


class TestAnAuditOutageDoesNotStopTheCycle:
    """The cycle must survive a datastore that cannot take its audit events.

    Seven audit writes inside `run_cycle`'s loop could raise, and `run_cycle`'s only outer handler
    catches `_RunKilled`, not `Exception`. So a transient audit outage ended the self-driving cycle
    part-way — with models already acted on, the remaining ones never reached, and
    `update_autopilot_run()` never called, leaving the run row silent about work that had really
    happened. These run the cycle with the audit datastore refusing every write.
    """

    def setup_method(self):
        set_drift_auto_retrain(
            JPCP, enabled=True, min_z_score=2.0, dataset_name="PM100Dataset", cooldown_s=0
        )
        set_drift_baseline(JPCP, {"mean": 1.0, "std": 0.1})
        for _ in range(10):
            write_drift_snapshot(JPCP, "Production", 5.0, None)
        seed_data_drift_evidence(JPCP)

    @staticmethod
    def _break_audit(monkeypatch):
        """Every audit append raises, as an unreachable datastore would make it.

        **Both bindings, deliberately.** `autopilot_cmd` does `from examlops.data.audit import
        write_audit_event` at import time, so patching only the source module leaves the module's
        own bound reference intact — the raw call sites would keep working and the test would
        exercise nothing but the helper. Mutation testing caught exactly that: reverting one call
        site killed only the counter assertion, because the other two never reached a broken write.
        """

        def boom(*_a, **_k):
            raise RuntimeError("audit datastore unreachable")

        monkeypatch.setattr("examlops.data.audit.write_audit_event", boom)
        monkeypatch.setattr(autopilot_cmd, "write_audit_event", boom, raising=False)

    def test_the_cycle_still_completes_and_reports_the_block(self, monkeypatch):
        self._break_audit(monkeypatch)
        with patch.object(
            autopilot_cmd, "_policy_decide", return_value=("deny", "blocked by policy")
        ):
            result = autopilot_cmd.run_cycle(dry_run=True)
        assert result["policy_blocks"], "the cycle stopped before it could record the block"
        assert result["policy_blocks"][0]["model"] == JPCP

    def test_the_run_row_is_still_updated(self, monkeypatch):
        """The bookkeeping at the end of the cycle is what an operator reads afterwards."""
        from examlops.data.autopilot import list_autopilot_runs

        self._break_audit(monkeypatch)
        with patch.object(autopilot_cmd, "_policy_decide", return_value=("deny", "blocked")):
            result = autopilot_cmd.run_cycle(dry_run=True)

        runs = list_autopilot_runs(last_n=5)
        assert runs, "no run was recorded at all"
        row = next((r for r in runs if r["id"] == result["run_id"]), None)
        assert row is not None, "this cycle's run row is missing"
        assert row["policy_blocks"] == 1, (
            "the run row does not reflect the block the cycle actually made — the cycle died "
            "before `update_autopilot_run()`"
        )

    def test_the_loss_is_counted_rather_than_hidden(self, monkeypatch):
        from examlops.data import audit as _audit

        _audit.reset_dropped_audit_events()
        self._break_audit(monkeypatch)
        with patch.object(autopilot_cmd, "_policy_decide", return_value=("deny", "blocked")):
            autopilot_cmd.run_cycle(dry_run=True)
        dropped = _audit.dropped_audit_events()
        assert dropped, "audit events were lost with no counter to show for it"
        assert "policy_denied" in dropped
        _audit.reset_dropped_audit_events()
