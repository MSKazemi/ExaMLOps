"""Actuate Ray Serve autoscaling from the policy (enterprise-readiness Phase 1, item 1.6).

Proves the platform's autoscale policy maps to Ray Serve's native `autoscaling_config`, so Ray
autoscales the deployment directly (surviving replica restarts) instead of an in-process poller —
scale-to-zero → min_replicas 0, anti-thrash windows → up/downscale delays, GPU fraction → actor opts.
"""

from __future__ import annotations

from examlops.autoscale import (
    AutoscalePolicy,
    to_ray_autoscaling_config,
    to_ray_deployment_kwargs,
)


def test_basic_mapping():
    p = AutoscalePolicy(
        min_replicas=2, max_replicas=8, target_value=5, stabilization_s=30, cooldown_s=90
    )
    cfg = to_ray_autoscaling_config(p)
    assert cfg["min_replicas"] == 2
    assert cfg["max_replicas"] == 8
    assert cfg["target_ongoing_requests"] == 5
    assert cfg["upscale_delay_s"] == 30
    assert cfg["downscale_delay_s"] == 90
    assert "downscale_to_zero_delay_s" not in cfg  # no scale-to-zero, no zero delay


def test_scale_to_zero_sets_min_replicas_zero():
    p = AutoscalePolicy(min_replicas=1, max_replicas=4, scale_to_zero_after_s=300, warm_pool=0)
    cfg = to_ray_autoscaling_config(p)
    assert cfg["min_replicas"] == 0
    assert cfg["initial_replicas"] == 0
    assert cfg["downscale_to_zero_delay_s"] == 300  # the idle time before the last replica goes


def test_warm_pool_sets_initial_replicas():
    p = AutoscalePolicy(min_replicas=0, max_replicas=4, scale_to_zero_after_s=120, warm_pool=1)
    cfg = to_ray_autoscaling_config(p)
    assert cfg["initial_replicas"] == 1  # keep a warm replica


def test_target_is_at_least_one():
    p = AutoscalePolicy(target_value=0)  # avoid div-by-zero / nonsensical 0 target
    assert to_ray_autoscaling_config(p)["target_ongoing_requests"] == 1.0


def test_deployment_kwargs_include_gpu_fraction():
    p = AutoscalePolicy(gpu_fraction=0.5)
    kw = to_ray_deployment_kwargs(p)
    assert kw["ray_actor_options"]["num_gpus"] == 0.5
    assert "autoscaling_config" in kw


def test_ray_itself_accepts_every_key_and_honours_the_target():
    """Ray ignores keys it does not know, so a misspelled key is a silent default, not an error.
    The target was emitted as `target_num_ongoing_requests_per_replica` and every policy ran with
    Ray's default target of 2. Feed the output through Ray's own model and read it back."""
    import pytest

    serve_config = pytest.importorskip("ray.serve.config")
    fields = set(serve_config.AutoscalingConfig.model_fields)
    for policy in (
        AutoscalePolicy(min_replicas=2, max_replicas=8, target_value=7, cooldown_s=90),
        AutoscalePolicy(max_replicas=4, scale_to_zero_after_s=300, warm_pool=1),
    ):
        cfg = to_ray_autoscaling_config(policy)
        assert set(cfg) <= fields, set(cfg) - fields
        parsed = serve_config.AutoscalingConfig(**cfg)
        assert parsed.target_ongoing_requests == max(1.0, policy.target_value)
        assert parsed.max_replicas == policy.max_replicas


# ─── the model server's own replica settings (plan P4.8) ─────────────────────


def _server():
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for p in (str(root), str(root / "modelzoo")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from serving.ray_serving import app

    return app


def test_the_model_server_runs_a_fixed_replica_count_by_default(monkeypatch):
    app = _server()
    monkeypatch.delenv("RAY_AUTOSCALE_MAX_REPLICAS", raising=False)
    monkeypatch.setattr(app, "NUM_REPLICAS", 3)
    assert app._server_scaling() == {"num_replicas": 3}


def test_a_replica_ceiling_turns_on_ray_autoscaling_above_the_floor(monkeypatch):
    import pytest

    serve_config = pytest.importorskip("ray.serve.config")
    app = _server()
    monkeypatch.setattr(app, "NUM_REPLICAS", 2)
    monkeypatch.setenv("RAY_AUTOSCALE_MAX_REPLICAS", "6")
    monkeypatch.setenv("RAY_AUTOSCALE_TARGET_ONGOING", "8")
    scaling = app._server_scaling()
    assert set(scaling) == {"autoscaling_config"}  # never both, which Ray refuses
    parsed = serve_config.AutoscalingConfig(**scaling["autoscaling_config"])
    assert (parsed.min_replicas, parsed.max_replicas) == (2, 6)
    assert parsed.target_ongoing_requests == 8
    assert parsed.downscale_delay_s == 300  # scale in slowly: a replica reloads the hot set


def test_a_ceiling_below_the_floor_is_raised_to_it(monkeypatch):
    app = _server()
    monkeypatch.setattr(app, "NUM_REPLICAS", 4)
    monkeypatch.setenv("RAY_AUTOSCALE_MAX_REPLICAS", "1")
    config = app._server_scaling()["autoscaling_config"]
    assert config["min_replicas"] == config["max_replicas"] == 4


def test_the_inference_pipeline_runs_two_replicas_of_each_stage_by_default():
    """One replica per stage was a single point of failure in front of a replicated server."""
    import os

    _server()  # the same import path setup
    from serving.inference_pipeline import app as pipeline

    if "INFERENCE_PIPELINE_REPLICAS" not in os.environ:
        assert pipeline._PIPELINE_REPLICAS == 2
    for deployment in (
        pipeline._ModelRouterDeployment,
        pipeline._FeatureTransformerDeployment,
        pipeline._IngressDeployment,
    ):
        assert deployment.num_replicas == pipeline._PIPELINE_REPLICAS, deployment.name


# ─── the tracing switch must not switch off Ray's metrics ───────────────────


def test_a_true_tracing_switch_is_removed_before_ray_starts(monkeypatch):
    """Ray 2.55 records its metrics through the OpenTelemetry SDK, which OTEL_SDK_DISABLED=true
    (the platform's tracing-off default) turns into a no-op: ray-serving exported no metric at
    all and every serving alert was blind. tests/integration/test_serving_metrics_live.py shows
    the effect against a real Ray."""
    import os

    from examlops.observability import tracing_enabled

    app = _server()
    for value in ("true", "1", "TRUE", " yes "):
        monkeypatch.setenv("OTEL_SDK_DISABLED", value)
        app._prepare_ray_environment()
        assert "OTEL_SDK_DISABLED" not in os.environ, value
        assert tracing_enabled() is False  # unset still means tracing off


def test_tracing_turned_on_is_left_alone(monkeypatch):
    import os

    app = _server()
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    app._prepare_ray_environment()
    assert os.environ["OTEL_SDK_DISABLED"] == "false"
    monkeypatch.delenv("OTEL_SDK_DISABLED")
    app._prepare_ray_environment()  # unset stays unset
    assert "OTEL_SDK_DISABLED" not in os.environ
