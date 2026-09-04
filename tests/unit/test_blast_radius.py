"""Blast-radius contracts, per-behaviour autonomy, and the live-run interrupt (ADR 0113).

Decisions under test: (1) a change outside ``may_change`` is denied and the denial names the
contract clause; (3) autonomy is per behaviour, pausable without losing config, and AUTONOMOUS
requires a recorded acknowledgment; (4) one in-flight run can be frozen/killed and a model
quarantined, audited.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from examlops import blast_radius
from examlops.cli.commands import autopilot_cmd
from examlops.platform_db import (
    get_autopilot_config,
    get_db,
    init_db,
    set_autopilot_config,
    set_drift_auto_retrain,
    set_drift_baseline,
    set_promotion_rule,
    write_drift_snapshot,
)
from tests.unit.test_autopilot import seed_data_drift_evidence


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_AUTOPILOT_ENABLED", raising=False)
    monkeypatch.delenv("EXAMLOPS_CONTRACTS_FILE", raising=False)
    init_db()
    monkeypatch.setattr(autopilot_cmd, "_alias_version", lambda model, alias="Production": "4")


def _audit_actions() -> list[str]:
    with get_db() as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit_events").fetchall()]


# ── decision 1: contract checks name the clause ───────────────────────────────


class TestContractCheck:
    def test_allowed_change(self):
        ok, clause = blast_radius.check_change("drift_auto_retrain", "pipeline_run", {"models": 1})
        assert ok and clause == ""

    def test_forbidden_change_names_may_not_change(self):
        ok, clause = blast_radius.check_change("drift_auto_retrain", "model_alias:Production")
        assert not ok
        assert "may_not_change" in clause and "model_alias:Production" in clause

    def test_undeclared_change_names_may_change(self):
        ok, clause = blast_radius.check_change("drift_auto_retrain", "hpc_job")
        assert not ok
        assert "may_change" in clause

    def test_extent_over_cap_names_the_bound(self):
        ok, clause = blast_radius.check_change("drift_auto_retrain", "pipeline_run", {"models": 3})
        assert not ok
        assert "max_extent_per_action.models" in clause

    def test_unknown_behaviour_denied(self):
        ok, clause = blast_radius.check_change("mystery_behaviour", "pipeline_run")
        assert not ok and "no blast-radius contract" in clause

    def test_promote_contract_allows_its_alias(self):
        ok, _ = blast_radius.check_change(
            "autopilot_promote", "model_alias:Production", {"models": 1}
        )
        assert ok

    def test_overlay_narrows_never_widens(self, tmp_path, monkeypatch):
        overlay = tmp_path / "contracts.yaml"
        overlay.write_text(
            "drift_auto_retrain:\n"
            "  autonomy: REVIEW\n"
            "  may_not_change: [pipeline_run]\n"  # operator forbids what defaults allow
            "  max_extent_per_action: {models: 5}\n"  # widening attempt — ignored
        )
        monkeypatch.setenv("EXAMLOPS_CONTRACTS_FILE", str(overlay))
        c = blast_radius.load_contracts()["drift_auto_retrain"]
        assert c.default_autonomy == "REVIEW"  # narrowing applied
        assert c.max_extent_per_action["models"] == 1  # widening refused
        ok, clause = blast_radius.check_change("drift_auto_retrain", "pipeline_run")
        assert not ok and "may_not_change" in clause  # operator addition enforced


# ── decision 3: per-behaviour autonomy with recorded acknowledgment ───────────


class TestAutonomy:
    def test_default_comes_from_contract(self):
        assert blast_radius.get_autonomy("drift_auto_retrain") == blast_radius.AUTONOMOUS

    def test_pause_preserves_other_config(self):
        set_drift_auto_retrain("JPCP", enabled=True, min_z_score=2.5, dataset_name="D")
        blast_radius.set_autonomy("drift_auto_retrain", "DISABLED", actor="tester")
        assert blast_radius.get_autonomy("drift_auto_retrain") == blast_radius.DISABLED
        with get_db() as conn:
            row = conn.execute(
                "SELECT min_z_score FROM drift_auto_retrain WHERE model='JPCP'"
            ).fetchone()
        assert row["min_z_score"] == 2.5, "pausing must not lose the behaviour's configuration"

    def test_autonomous_requires_acknowledgment(self):
        with pytest.raises(ValueError, match="acknowledgment"):
            blast_radius.set_autonomy("drift_auto_retrain", "AUTONOMOUS", actor="tester")

    def test_autonomous_grant_is_recorded(self):
        blast_radius.set_autonomy(
            "drift_auto_retrain",
            "AUTONOMOUS",
            actor="tester",
            acknowledgment="I accept autonomous retrains in staging",
        )
        ack = json.loads(get_autopilot_config("autonomy_ack:drift_auto_retrain"))
        assert ack["by"] == "tester" and "staging" in ack["ack"]
        assert "autonomy_changed" in _audit_actions()

    def test_operator_autonomous_without_ack_row_degrades_to_review(self):
        # A directly-poked config value without its acknowledgment is not an effective grant.
        set_autopilot_config("autonomy:drift_auto_retrain", "AUTONOMOUS")
        assert blast_radius.get_autonomy("drift_auto_retrain") == blast_radius.REVIEW


# ── decision 4: interrupt + quarantine primitives ─────────────────────────────


class TestInterruptPrimitives:
    def test_interrupt_roundtrip_and_audit(self):
        blast_radius.request_interrupt(7, "kill", actor="tester", reason="bad cycle")
        assert blast_radius.pending_interrupt(7) == "kill"
        blast_radius.clear_interrupt(7, actor="tester")
        assert blast_radius.pending_interrupt(7) is None
        actions = _audit_actions()
        assert "run_kill_requested" in actions and "run_resumed" in actions

    def test_invalid_action_rejected(self):
        with pytest.raises(ValueError):
            blast_radius.request_interrupt(7, "pause", actor="tester")

    def test_quarantine_roundtrip(self):
        blast_radius.quarantine_model("JPCP", actor="tester", reason="wild predictions")
        assert blast_radius.quarantine_reason("JPCP") == "wild predictions"
        blast_radius.release_model("JPCP", actor="tester")
        assert blast_radius.quarantine_reason("JPCP") is None
        actions = _audit_actions()
        assert "model_quarantined" in actions and "model_released" in actions


# ── cycle wiring: gates actually stop the autopilot ───────────────────────────


def _arm_drifting_jpcp():
    set_autopilot_config("enabled", "1")
    set_drift_auto_retrain("JPCP", enabled=True, min_z_score=2.0, dataset_name="D", cooldown_s=0)
    set_drift_baseline("JPCP", {"mean": 1.0, "std": 0.1})
    for _ in range(10):
        write_drift_snapshot("JPCP", "Production", 5.0, None)
    seed_data_drift_evidence("JPCP")


class TestCycleWiring:
    def test_quarantined_model_is_skipped(self):
        _arm_drifting_jpcp()
        blast_radius.quarantine_model("JPCP", actor="tester", reason="held")
        with patch.object(autopilot_cmd, "_call_retrain") as mock_retrain:
            result = autopilot_cmd.run_cycle()
        mock_retrain.assert_not_called()
        assert any("quarantined" in s["reason"] for s in result["skipped"])

    def test_disabled_autonomy_skips_without_losing_config(self):
        _arm_drifting_jpcp()
        blast_radius.set_autonomy("drift_auto_retrain", "DISABLED", actor="tester")
        with patch.object(autopilot_cmd, "_call_retrain") as mock_retrain:
            result = autopilot_cmd.run_cycle()
        mock_retrain.assert_not_called()
        assert any("DISABLED" in s["reason"] for s in result["skipped"])

    def test_review_autonomy_routes_to_hitl(self):
        _arm_drifting_jpcp()
        blast_radius.set_autonomy("drift_auto_retrain", "REVIEW", actor="tester")
        with patch.object(autopilot_cmd, "_call_retrain") as mock_retrain:
            result = autopilot_cmd.run_cycle()
        mock_retrain.assert_not_called()
        assert any(h["gate"] == "drift_auto_retrain" for h in result["human_required"])
        assert "human_approval_required" in _audit_actions()

    def test_kill_interrupt_aborts_cycle_and_records_run(self):
        _arm_drifting_jpcp()

        real_create = autopilot_cmd.create_autopilot_run

        def create_and_flag(*a, **k):
            rid = real_create(*a, **k)
            blast_radius.request_interrupt(rid, "kill", actor="tester")
            return rid

        with (
            patch.object(autopilot_cmd, "create_autopilot_run", side_effect=create_and_flag),
            patch.object(autopilot_cmd, "_call_retrain") as mock_retrain,
        ):
            result = autopilot_cmd.run_cycle()
        mock_retrain.assert_not_called()
        assert "interrupted" in result
        assert "run_killed" in _audit_actions()

    def test_promote_contract_denial_names_clause(self, monkeypatch, tmp_path):
        # Operator forbids Production moves via the overlay; the promote path must be denied
        # with the clause, not silently skipped.
        set_autopilot_config("enabled", "1")
        set_promotion_rule("JPCP", "rmse", "lt", 100.0)
        overlay = tmp_path / "contracts.yaml"
        overlay.write_text("autopilot_promote:\n  may_not_change: ['model_alias:Production']\n")
        monkeypatch.setenv("EXAMLOPS_CONTRACTS_FILE", str(overlay))
        with (
            patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"rmse": 1.0}),
            patch.object(autopilot_cmd, "_do_promote") as mock_promote,
        ):
            result = autopilot_cmd.run_cycle()
        mock_promote.assert_not_called()
        blocks = [b for b in result["policy_blocks"] if b.get("gate") == "autopilot_promote"]
        assert blocks and "may_not_change" in blocks[0]["reason"]
        assert "contract_denied" in _audit_actions()
