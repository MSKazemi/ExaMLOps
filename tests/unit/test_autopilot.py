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


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_AUTOPILOT_ENABLED", raising=False)
    init_db()


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


class TestRunCyclePolicy:
    def setup_method(self):
        set_autopilot_config("enabled", "1")
        set_drift_auto_retrain(
            JPCP, enabled=True, min_z_score=2.0, dataset_name="PM100Dataset", cooldown_s=0
        )
        set_drift_baseline(JPCP, {"mean": 1.0, "std": 0.1})
        for _ in range(10):
            write_drift_snapshot(JPCP, "Production", 5.0, None)

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
        set_drift_auto_retrain(JPCP, enabled=True, min_z_score=2.0, dataset_name="D", cooldown_s=0)
        set_drift_baseline(JPCP, {"mean": 1.0, "std": 0.1})
        for _ in range(10):
            write_drift_snapshot(JPCP, "Production", 5.0, None)

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
