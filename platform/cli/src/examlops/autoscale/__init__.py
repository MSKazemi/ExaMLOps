"""Next-Gen 40 · E5 — autoscaling & scale-to-zero (ADR 0031).

Per-model, metric-driven replica autoscaling with scale-to-zero, managed cold starts, a
warm-pool option, and anti-thrash controls (stabilization + cooldown), GPU-fraction-aware
(E3). Scale events are audited (D4) and scale-to-zero durations feed FinOps savings.

The core `decide_scale` is a **pure function** of the current state + observed metrics +
policy + clock, so it is fully testable and the same logic can drive a KEDA/Knative
generator or an in-process controller. No cluster required.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from examlops import platform_db


@dataclass
class AutoscalePolicy:
    min_replicas: int = 1
    max_replicas: int = 4
    target_metric: str = "queue_depth"
    target_value: float = 10.0
    scale_to_zero_after_s: int = 0  # 0 disables scale-to-zero
    warm_pool: int = 0
    stabilization_s: int = 30
    cooldown_s: int = 60
    gpu_fraction: float = 1.0

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> AutoscalePolicy:
        return cls(
            min_replicas=cfg["min_replicas"],
            max_replicas=cfg["max_replicas"],
            target_metric=cfg["target_metric"],
            target_value=cfg["target_value"],
            scale_to_zero_after_s=cfg["scale_to_zero_after_s"],
            warm_pool=cfg["warm_pool"],
            stabilization_s=cfg["stabilization_s"],
            cooldown_s=cfg["cooldown_s"],
            gpu_fraction=cfg["gpu_fraction"],
        )


@dataclass
class ScaleDecision:
    desired_replicas: int
    current_replicas: int
    reason: str
    changed: bool
    blocked_by: str | None = None  # cooldown | stabilization | None


def _target_replicas(observed: float, policy: AutoscalePolicy) -> int:
    """Replicas needed to bring the metric to target (ceil), clamped to [min|0, max]."""
    if policy.target_value <= 0:
        return policy.min_replicas
    need = math.ceil(observed / policy.target_value)
    floor = 0 if policy.scale_to_zero_after_s > 0 else policy.min_replicas
    return max(floor, min(policy.max_replicas, need))


def decide_scale(
    current_replicas: int,
    observed_metric: float,
    policy: AutoscalePolicy,
    *,
    idle_seconds: float = 0.0,
    seconds_since_last_scale: float = 1e9,
) -> ScaleDecision:
    """Pure scaling decision with scale-to-zero + anti-thrash (R1/R2/R3).

    - Scales toward the replica count that meets ``target_value`` for ``target_metric``.
    - Scales to zero when idle beyond ``scale_to_zero_after_s`` (if enabled), respecting
      the warm pool.
    - Blocks scale-*down* during the cooldown and scale-*up*/down during the stabilization
      window to prevent thrashing.
    """
    # Scale-to-zero on idle (respect warm pool).
    if policy.scale_to_zero_after_s > 0 and idle_seconds >= policy.scale_to_zero_after_s:
        target = max(policy.warm_pool, 0)
        if target < current_replicas:
            if seconds_since_last_scale < policy.cooldown_s:
                return ScaleDecision(
                    current_replicas,
                    current_replicas,
                    "idle scale-to-zero deferred (cooldown)",
                    False,
                    "cooldown",
                )
            return ScaleDecision(
                target,
                current_replicas,
                f"idle {idle_seconds:.0f}s ≥ {policy.scale_to_zero_after_s}s → scale to {target}",
                True,
            )

    target = _target_replicas(observed_metric, policy)
    target = max(target, policy.warm_pool)
    if target == current_replicas:
        return ScaleDecision(current_replicas, current_replicas, "at target", False)

    scaling_up = target > current_replicas
    # Anti-thrash: cooldown gates scale-down; stabilization gates any change.
    if seconds_since_last_scale < policy.stabilization_s:
        return ScaleDecision(
            current_replicas,
            current_replicas,
            "within stabilization window",
            False,
            "stabilization",
        )
    if not scaling_up and seconds_since_last_scale < policy.cooldown_s:
        return ScaleDecision(
            current_replicas, current_replicas, "scale-down within cooldown", False, "cooldown"
        )

    verb = "up" if scaling_up else "down"
    return ScaleDecision(
        target,
        current_replicas,
        f"{policy.target_metric}={observed_metric:g} → scale {verb} to {target}",
        True,
    )


def set_policy(model: str, **kw: Any) -> None:
    """Declare/patch a model's autoscale policy (R1/R7)."""
    platform_db.set_autoscale_config(model, **kw)


def get_policy(model: str) -> AutoscalePolicy | None:
    cfg = platform_db.get_autoscale_config(model)
    return AutoscalePolicy.from_config(cfg) if cfg else None


def apply_scale(
    model: str,
    from_replicas: int,
    decision: ScaleDecision,
    *,
    tenant: str = "default",
    metric_value: float | None = None,
    cold_start_s: float | None = None,
    actor: str | None = None,
) -> None:
    """Record a scale event (audited D4) — the executed side of a decision (R7)."""
    platform_db.record_scale_event(
        model,
        from_replicas,
        decision.desired_replicas,
        tenant=tenant,
        reason=decision.reason,
        metric_value=metric_value,
        cold_start_s=cold_start_s,
    )
    platform_db.write_audit_event(
        "cli",
        actor,
        "autoscale_event",
        model,
        {"from": from_replicas, "to": decision.desired_replicas, "reason": decision.reason},
        tenant=tenant,
    )


def cold_start_seconds(model: str) -> float | None:
    """Measured mean cold-start time (surfaced to C6 SLO) (R4)."""
    events = platform_db.list_scale_events(model, last_n=200)
    starts = [e["cold_start_s"] for e in events if e["cold_start_s"] is not None]
    return (sum(starts) / len(starts)) if starts else None


def scale_to_zero_savings(model: str, *, gpu_cost_per_hour: float = 2.0) -> dict[str, Any]:
    """Estimate FinOps savings from scale-to-zero events (R7).

    Counts replica-hours *not* run when scaled to zero (approximated by the number of
    scale-to-zero transitions × the configured idle window), × GPU fraction × cost.
    """
    cfg = platform_db.get_autoscale_config(model)
    frac = cfg["gpu_fraction"] if cfg else 1.0
    window_h = (cfg["scale_to_zero_after_s"] / 3600.0) if cfg else 0.0
    events = platform_db.list_scale_events(model, last_n=500)
    to_zero = [e for e in events if e["to_replicas"] == 0]
    saved_replica_hours = len(to_zero) * max(window_h, 0.0) * frac
    return {
        "model": model,
        "scale_to_zero_events": len(to_zero),
        "saved_gpu_hours": round(saved_replica_hours, 4),
        "saved_cost": round(saved_replica_hours * gpu_cost_per_hour, 4),
    }


__all__ = [
    "AutoscalePolicy",
    "ScaleDecision",
    "decide_scale",
    "set_policy",
    "get_policy",
    "apply_scale",
    "cold_start_seconds",
    "scale_to_zero_savings",
]
