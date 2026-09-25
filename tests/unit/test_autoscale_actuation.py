"""ADR 0031 — YAML policy defaults, desired-state applier, KEDA/Knative manifests, prefetch plan.

Real platform.db, real controller, real ``decide_scale``; only signals and the clock are fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from examlops.autoscale import AutoscalePolicy, get_policy, set_policy
from examlops.autoscale import controller as ctl
from examlops.autoscale.controller import (
    AutoscaleController,
    DesiredStateApplier,
    Signals,
    make_applier,
)
from examlops.autoscale.manifests import (
    ManifestError,
    render_keda_scaledobject,
    render_knative_overlay,
)
from examlops.autoscale.policy_yaml import (
    effective_config,
    effective_configs,
    validate_autoscale_block,
)
from examlops.autoscale.prefetch import plan_prefetch
from examlops.cli.main import app as exa_app
from examlops.data import autoscale_desired


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_ENABLED", "1")
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setenv("RAY_MODELS_DIR", str(models))
    from examlops import platform_db
    from examlops.coordination import reset_coordinator

    reset_coordinator()
    platform_db.init_db()
    return models


def _write_model(models: Path, name: str, block: dict | None) -> None:
    doc: dict = {"name": name, "config_class": "x.Y", "task_type": "regression"}
    if block is not None:
        doc["autoscale"] = block
    (models / f"{name.lower()}.yaml").write_text(yaml.safe_dump(doc))


class _Sig:
    def __init__(self, rps):
        self.rps = rps

    def read(self, model, policy):
        return Signals(rps=self.rps)


# ─── (c) YAML defaults, DB overrides ─────────────────────────────────────────


def test_a_yaml_block_is_the_default_policy_and_the_db_row_overrides_it(_env):
    _write_model(
        _env,
        "JPCP",
        {
            "min_replicas": 0,
            "max_replicas": 6,
            "target_metric": "rps",
            "target_value": 5,
            "scale_to_zero_after_s": 120,
        },
    )
    _write_model(_env, "MACK", None)
    cfg = effective_config("JPCP")
    assert cfg and cfg["source"] == "yaml" and cfg["max_replicas"] == 6
    assert effective_config("MACK") is None
    assert get_policy("JPCP") == AutoscalePolicy(
        min_replicas=0,
        max_replicas=6,
        target_metric="rps",
        target_value=5.0,
        scale_to_zero_after_s=120,
    )
    set_policy("JPCP", min_replicas=2, max_replicas=3, target_metric="rps", target_value=9)
    over = effective_config("JPCP")
    assert over and over["source"] == "db" and over["max_replicas"] == 3
    assert [c["model"] for c in effective_configs()] == ["JPCP"]


def test_no_yaml_block_means_no_policy_and_no_behaviour_change(_env):
    _write_model(_env, "JPCP", None)
    assert effective_configs() == []
    assert get_policy("JPCP") is None


@pytest.mark.parametrize(
    "block",
    [
        {"nope": 1},
        {"min_replicas": "1"},
        {"target_metric": "cpu"},
        {"min_replicas": 5, "max_replicas": 2},
        {"target_value": 0},
        {"scale_to_zero_after_s": 60, "min_replicas": 1},
        {"gpu_fraction": 2},
        {"min_replicas": True},
    ],
)
def test_invalid_blocks_are_reported(block):
    assert validate_autoscale_block(block)


def test_shipped_packs_declare_only_valid_blocks():
    root = Path(__file__).parents[2] / "usecases"
    for path in root.glob("*/models/*.yaml"):
        block = (yaml.safe_load(path.read_text()) or {}).get("autoscale")
        assert validate_autoscale_block(block) == [], path


def test_the_pipeline_loader_carries_the_block(_env):
    from pipelines.model_loader import load_model_yaml

    _write_model(_env, "JPCP", {"max_replicas": 3})
    assert load_model_yaml(_env / "jpcp.yaml").autoscale == {"max_replicas": 3}
    _write_model(_env, "MACK", None)
    assert load_model_yaml(_env / "mack.yaml").autoscale == {}


# ─── (a) the desired-state applier through the real controller ───────────────


def test_controller_records_desired_replicas_from_a_yaml_only_policy(_env):
    _write_model(
        _env,
        "JPCP",
        {
            "min_replicas": 1,
            "max_replicas": 8,
            "target_metric": "rps",
            "target_value": 10,
            "stabilization_s": 0,
            "cooldown_s": 0,
        },
    )
    applier = make_applier("desired")
    assert isinstance(applier, DesiredStateApplier)
    from examlops.autoscale import ScaleDecision, apply_scale

    apply_scale("JPCP", 1, ScaleDecision(1, 1, "seed", False), actor="t")
    apply_scale("JPCP", 1, ScaleDecision(2, 1, "seed", True), actor="t")  # ledger: 2
    assert applier.current_replicas("JPCP") == 2
    c = AutoscaleController(_Sig(35.0), applier, dry_run=False, clock=lambda: 1e10)
    rep = c.run_cycle()
    assert rep.count("applied") == 1
    row = autoscale_desired.get_desired("JPCP")
    assert row and row["replicas"] == 4  # ceil(35/10)
    assert applier.current_replicas("JPCP") == 4  # desired row now wins over the ledger


def test_dry_run_writes_no_desired_row(_env):
    _write_model(
        _env,
        "JPCP",
        {"target_metric": "rps", "target_value": 10, "stabilization_s": 0, "cooldown_s": 0},
    )
    from examlops.autoscale import ScaleDecision, apply_scale

    apply_scale("JPCP", 1, ScaleDecision(2, 1, "seed", True), actor="t")
    c = AutoscaleController(_Sig(50.0), make_applier("desired"), dry_run=True, clock=lambda: 1e10)
    assert c.run_cycle().count("dry_run") == 1
    assert autoscale_desired.get_desired("JPCP") is None


def test_the_ray_applier_still_refuses_and_says_why():
    with pytest.raises(ctl.ScaleApplierUnavailable, match="not built"):
        make_applier("ray").apply("JPCP", 1, 2)


def test_set_desired_validates():
    with pytest.raises(ValueError):
        autoscale_desired.set_desired("JPCP", -1)
    with pytest.raises(ValueError):
        autoscale_desired.set_desired("", 1)


# ─── (d) manifests ───────────────────────────────────────────────────────────


def test_keda_scaledobject_maps_the_policy():
    p = AutoscalePolicy(
        min_replicas=0, max_replicas=5, target_metric="rps", target_value=7, cooldown_s=90
    )
    doc = yaml.safe_load(yaml.safe_dump(render_keda_scaledobject("JPCP", p, namespace="ml")))
    assert doc["kind"] == "ScaledObject" and doc["apiVersion"] == "keda.sh/v1alpha1"
    spec = doc["spec"]
    assert (spec["minReplicaCount"], spec["maxReplicaCount"], spec["cooldownPeriod"]) == (0, 5, 90)
    assert spec["scaleTargetRef"]["name"] == "jpcp-predictor"
    trig = spec["triggers"][0]
    assert trig["type"] == "prometheus" and trig["metadata"]["threshold"] == "7"
    assert "examlops_predict_requests_total" in trig["metadata"]["query"]
    assert doc["metadata"]["namespace"] == "ml"


def test_metrics_without_a_source_are_refused(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY", raising=False)
    with pytest.raises(ManifestError, match="no per-model series"):
        render_keda_scaledobject("JPCP", AutoscalePolicy(target_metric="gpu_util"))


def test_queue_depth_renders_the_little_law_trigger():
    doc = render_keda_scaledobject("JPCP", AutoscalePolicy(target_metric="queue_depth"))
    q = doc["spec"]["triggers"][0]["metadata"]["query"]
    assert q == 'sum(rate(examlops_predict_latency_seconds_sum{model_name=~"(?i)^JPCP$"}[1m]))'


def test_knative_overlay_and_its_limits():
    p = AutoscalePolicy(
        min_replicas=0,
        max_replicas=3,
        target_metric="rps",
        target_value=4,
        scale_to_zero_after_s=300,
    )
    doc = render_knative_overlay("JPCP", p)
    ann = doc["metadata"]["annotations"]
    assert ann["autoscaling.knative.dev/min-scale"] == "0"
    assert ann["autoscaling.knative.dev/scale-to-zero-pod-retention-period"] == "300s"
    assert doc["spec"]["predictor"] == {"minReplicas": 0, "maxReplicas": 3}
    with pytest.raises(ManifestError):
        render_knative_overlay("JPCP", AutoscalePolicy(target_metric="p95"))


def test_nothing_is_rendered_by_default_and_helm_carries_no_keda():
    chart = Path(__file__).parents[2] / "platform" / "infra" / "helm" / "examlops"
    assert not [f for f in chart.rglob("*.yaml") if "ScaledObject" in f.read_text()]


# ─── (e) prefetch plan ───────────────────────────────────────────────────────


def test_prefetch_plan_rules():
    cfgs = [
        {"model": "A", "warm_pool": 1, "min_replicas": 1},
        {"model": "B", "warm_pool": 0, "min_replicas": 0},
        {"model": "C", "warm_pool": 0, "min_replicas": 0},
        {"model": "D", "warm_pool": 0, "min_replicas": 2},
        {"model": "E", "warm_pool": 0, "min_replicas": 0},
    ]
    plan = plan_prefetch(cfgs, {"A": 0.0, "B": 2.0, "C": 9.0, "D": 50.0, "E": None})
    assert [(p["model"], p["action"]) for p in plan] == [
        ("A", "keep_warm"),
        ("C", "prefetch"),
        ("B", "prefetch"),
    ]  # D cannot go cold, E's traffic is absent (not zero, not assumed)
    assert len(plan_prefetch(cfgs, {"B": 1, "C": 2}, top=1)) == 1


# ─── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_manifest_and_status_show_the_yaml_policy(_env):
    _write_model(
        _env,
        "JPCP",
        {"min_replicas": 0, "max_replicas": 4, "target_metric": "rps", "target_value": 5},
    )
    r = CliRunner().invoke(exa_app, ["serve", "autoscale", "manifest", "JPCP", "--kind", "keda"])
    assert r.exit_code == 0, r.output
    assert yaml.safe_load(r.output)["kind"] == "ScaledObject"
    r = CliRunner().invoke(exa_app, ["--json", "serve", "autoscale", "status", "JPCP"])
    assert r.exit_code == 0, r.output
    assert '"source": "yaml"' in r.output
    r = CliRunner().invoke(exa_app, ["serve", "autoscale", "manifest", "GHOST"])
    assert r.exit_code == 1
    set_policy("JPCP", target_metric="gpu_util")  # no source without an operator template
    r = CliRunner().invoke(exa_app, ["serve", "autoscale", "manifest", "JPCP"])
    assert r.exit_code == 2
