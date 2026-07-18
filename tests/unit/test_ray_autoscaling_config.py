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
    assert cfg["target_num_ongoing_requests_per_replica"] == 5
    assert cfg["upscale_delay_s"] == 30
    assert cfg["downscale_delay_s"] == 90  # max(cooldown, scale-to-zero idle)


def test_scale_to_zero_sets_min_replicas_zero():
    p = AutoscalePolicy(min_replicas=1, max_replicas=4, scale_to_zero_after_s=300, warm_pool=0)
    cfg = to_ray_autoscaling_config(p)
    assert cfg["min_replicas"] == 0
    assert cfg["initial_replicas"] == 0
    assert cfg["downscale_delay_s"] == 300


def test_warm_pool_sets_initial_replicas():
    p = AutoscalePolicy(min_replicas=0, max_replicas=4, scale_to_zero_after_s=120, warm_pool=1)
    cfg = to_ray_autoscaling_config(p)
    assert cfg["initial_replicas"] == 1  # keep a warm replica


def test_target_is_at_least_one():
    p = AutoscalePolicy(target_value=0)  # avoid div-by-zero / nonsensical 0 target
    assert to_ray_autoscaling_config(p)["target_num_ongoing_requests_per_replica"] == 1.0


def test_deployment_kwargs_include_gpu_fraction():
    p = AutoscalePolicy(gpu_fraction=0.5)
    kw = to_ray_deployment_kwargs(p)
    assert kw["ray_actor_options"]["num_gpus"] == 0.5
    assert "autoscaling_config" in kw
