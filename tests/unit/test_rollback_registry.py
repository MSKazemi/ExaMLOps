"""ADR 0113 decision 2 — an autonomous action must know how to undo itself.

`exa audit autonomy` could already list what the platform did on its own and which of those
declared no inverse. This is the half that makes the list actionable: the action is **refused
before execution**, not reported after.

The rule only works if the registry stays honest, so the last group of tests is a coverage
guard: an autonomous action nobody classified fails the build rather than defaulting to allowed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops import evidence, rollback
from examlops.data import get_db
from examlops.platform_db import (
    init_db,
    set_drift_auto_retrain,
    set_drift_baseline,
    write_drift_snapshot,
)
from examlops.rollback import AutonomousActionRefused, require_rollback

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    init_db()
    yield
    del os.environ["PLATFORM_DB"]


# ── the rule ──────────────────────────────────────────────────────────────────


def test_a_manual_action_is_never_gated():
    """A person may deliberately do things the platform must not do to itself. That asymmetry
    is ADR 0113's autonomy model, not an oversight."""
    with evidence.correlated(mode=evidence.MANUAL):
        assert require_rollback("autopilot_retrain_triggered") is None


def test_an_autonomous_mutating_action_without_an_inverse_is_refused():
    with evidence.correlated(mode=evidence.AUTONOMOUS):
        with pytest.raises(AutonomousActionRefused, match="no rollback_ref"):
            require_rollback("autopilot_retrain_triggered")


def test_an_autonomous_action_with_an_inverse_proceeds():
    with evidence.correlated(
        mode=evidence.AUTONOMOUS, rollback_ref="exa models rollback run JPCP --version 4"
    ):
        assert require_rollback("autopilot_retrain_triggered").endswith("--version 4")


def test_a_record_only_action_needs_no_inverse():
    """Demanding one would be a tax that teaches operators to declare fake inverses — and a fake
    inverse is worse than a missing one, because it reads as an undo path that does not undo."""
    with evidence.correlated(mode=evidence.AUTONOMOUS):
        assert require_rollback("policy_denied") is None
        assert require_rollback("drift_retrain_suppressed") is None


def test_a_no_autonomy_action_is_refused_even_with_a_rollback_ref():
    """Some state changes have no inverse at all. Offering one does not create it."""
    with evidence.correlated(mode=evidence.AUTONOMOUS, rollback_ref="pretend-undo"):
        with pytest.raises(AutonomousActionRefused, match="may not be performed autonomously"):
            require_rollback("secret_written")


def test_an_unregistered_action_is_gated_not_allowed():
    """The failure mode that would quietly reopen the gap: a new autonomous action sailing
    through because nobody thought about it."""
    assert rollback.is_gated("something_nobody_registered") is True
    with evidence.correlated(mode=evidence.AUTONOMOUS):
        with pytest.raises(AutonomousActionRefused, match="not in the rollback registry"):
            require_rollback("something_nobody_registered")


def test_the_refusal_is_a_permission_error_not_a_value_error():
    """A governance refusal, not a bad argument — callers that broadly catch ValueError must not
    swallow it."""
    assert issubclass(AutonomousActionRefused, PermissionError)
    assert not issubclass(AutonomousActionRefused, ValueError)


def test_explicit_arguments_override_the_ambient_context():
    assert require_rollback("autopilot_retrain_triggered", mode=evidence.MANUAL) is None


# ── building the inverse ──────────────────────────────────────────────────────


def test_the_inverse_is_a_real_runnable_command():
    ref = rollback.build_rollback_ref(
        "autopilot_retrain_triggered", model="JPCP", previous_version="4"
    )
    assert ref == "exa models rollback run JPCP --version 4"


def test_a_missing_parameter_yields_none_not_a_half_formatted_string():
    """A rollback reference with `{previous_version}` still in it looks like an undo path and is
    not one."""
    ref = rollback.build_rollback_ref("autopilot_retrain_triggered", model="JPCP")
    assert ref is None


def test_an_action_with_no_template_has_no_ref():
    assert rollback.build_rollback_ref("policy_denied") is None
    assert rollback.build_rollback_ref("nope") is None


# ── the registry stays honest ─────────────────────────────────────────────────


def test_every_registry_entry_is_well_formed():
    for action, entry in rollback.REGISTRY.items():
        assert entry.action == action, "key and entry disagree about the action name"
        assert entry.kind in (rollback.MUTATING, rollback.RECORD_ONLY, rollback.NO_AUTONOMY)
        if entry.kind == rollback.MUTATING:
            assert entry.template, f"{action} is mutating but declares no inverse"
        if entry.kind == rollback.NO_AUTONOMY:
            assert entry.reason, f"{action} refuses autonomy but does not say why"
        if entry.kind == rollback.RECORD_ONLY:
            assert entry.template is None, f"{action} records nothing but claims an inverse"


def test_every_mutating_template_names_a_command_that_exists():
    """A declared inverse that is not a real command is a fake undo path — the exact thing this
    module treats as worse than an admitted absence."""
    import subprocess

    checked = 0
    for entry in rollback.REGISTRY.values():
        if entry.kind != rollback.MUTATING or not entry.template:
            continue
        # "exa models rollback run {model} …" → ["models", "rollback", "run"]
        parts = [p for p in entry.template.split() if not p.startswith(("{", "-"))]
        assert parts[0] == "exa", entry.template
        path = parts[1:4]
        out = subprocess.run(
            [sys.executable, "-m", "examlops.cli.main", *path, "--help"],
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, f"{' '.join(path)} is not a real command: {out.stderr[:200]}"
        checked += 1
    assert checked >= 3, "the template check silently examined almost nothing"


def test_the_autonomous_paths_only_write_registered_actions():
    """The coverage guard.

    Every audit action written by a module an agent can drive must be classified, so adding a
    new one cannot silently skip the rollback gate. It deliberately scans the whole module rather
    than only the autonomous functions: an operator command today is an agent-callable tool
    tomorrow, and the classification is what decides whether it may be.
    """
    import re

    roots = [
        Path(__file__).parents[2] / "platform/cli/src/examlops/cli/commands/autopilot_cmd.py",
        Path(__file__).parents[2] / "platform/cli/src/examlops/cli/commands/drift.py",
    ]
    # `write_audit_event(source, actor, "<action>", ...)` — the third positional argument.
    pattern = re.compile(r"write_audit_event\(\s*\n?\s*[^,]+,\s*\n?\s*[^,]+,\s*\n?\s*\"([a-z_]+)\"")
    found: set[str] = set()
    for root in roots:
        found |= set(pattern.findall(root.read_text()))
    assert found, "the extractor matched nothing — it is no longer reading the call sites"
    # `autonomous_action_refused` is the refusal's own record; it changes nothing by definition.
    unregistered = {a for a in found if a not in rollback.REGISTRY} - {"autonomous_action_refused"}
    assert not unregistered, (
        f"agent-drivable module(s) write audit action(s) absent from the rollback registry: "
        f"{sorted(unregistered)}. Classify each as mutating (with its inverse), record_only, or "
        "no_autonomy in examlops/rollback.py."
    )


# ── the cycle actually refuses ────────────────────────────────────────────────


def _seed_drifting_model(model: str = "JPCP") -> None:
    from examlops.corruption import corruption_stats
    from examlops.data.drift import (
        set_corruption_baseline,
        set_input_baseline,
        write_input_snapshot,
    )

    set_drift_auto_retrain(model, enabled=True, min_z_score=2.0, dataset_name="D", cooldown_s=0)
    set_drift_baseline(model, {"mean": 1.0, "std": 0.1})
    preds = [5.0] * 10
    for p in preds:
        write_drift_snapshot(model, "Production", p, None)
    set_corruption_baseline(model, corruption_stats(preds))
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


def test_the_autopilot_refuses_a_retrain_it_could_not_undo():
    """No MLflow here, so no previous version resolves, so no inverse can be built — and the
    cycle must decline rather than retrain something it cannot roll back."""
    from examlops.cli.commands import autopilot_cmd
    from examlops.platform_db import set_autopilot_config

    set_autopilot_config("enabled", "1")
    _seed_drifting_model()
    with patch.object(autopilot_cmd, "_call_retrain") as mock_retrain:
        result = autopilot_cmd.run_cycle()
    mock_retrain.assert_not_called()
    assert result["refused"], "the cycle retrained without a way to undo it"
    assert "no rollback_ref" in result["refused"][0]["reason"]


def test_the_refusal_is_recorded_in_the_evidence_chain():
    from examlops.cli.commands import autopilot_cmd
    from examlops.platform_db import set_autopilot_config

    set_autopilot_config("enabled", "1")
    _seed_drifting_model()
    with patch.object(autopilot_cmd, "_call_retrain"):
        autopilot_cmd.run_cycle()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action='autonomous_action_refused'"
        ).fetchall()
    assert rows, "a refused action left no trace"
    assert rows[0]["mode"] == evidence.AUTONOMOUS


def test_a_resolvable_previous_version_lets_the_retrain_proceed():
    """The gate must not be a blanket off-switch: given a real inverse, the cycle acts."""
    from examlops.cli.commands import autopilot_cmd
    from examlops.platform_db import set_autopilot_config

    set_autopilot_config("enabled", "1")
    _seed_drifting_model()
    with (
        patch.object(autopilot_cmd, "_alias_version", return_value="4"),
        patch.object(autopilot_cmd, "_call_retrain", return_value={"flow_run_id": "r1"}) as m,
    ):
        result = autopilot_cmd.run_cycle()
    assert m.call_count == 1, result
    assert not result["refused"]
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM audit_events WHERE action='autopilot_retrain_triggered'"
        ).fetchone()
    assert row["rollback_ref"] == "exa models rollback run JPCP --version 4"
