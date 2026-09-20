"""KEDA and Knative/KServe autoscaling manifests, generated from a policy (ADR 0031 clause 1).

Pure generators — nothing here talks to a cluster and nothing is rendered unless asked for
(``exa serve autoscale manifest``). The Prometheus triggers use the same series the controller
reads (``examlops_predict_requests_total`` / ``examlops_predict_latency_seconds``); the policy
metrics ``queue_depth`` and ``gpu_util`` have **no per-model series in the platform**, so a policy
targeting them is refused rather than rendered as a trigger that never fires.

Assumptions the operator must own (not verifiable here, no cluster): the ``scaleTargetRef`` name
(KServe raw-deployment mode names a predictor Deployment ``<isvc>-predictor``) and the Prometheus
address KEDA queries.
"""

from __future__ import annotations

import re
from typing import Any

from examlops.autoscale import AutoscalePolicy

KEDA_API = "keda.sh/v1alpha1"
_NAME_RE = re.compile(r"[^a-z0-9-]+")
SOURCED_METRICS = ("rps", "p95")


class ManifestError(ValueError):
    """The policy cannot be expressed as a working autoscaling manifest."""


def k8s_name(model: str) -> str:
    name = _NAME_RE.sub("-", model.lower()).strip("-")
    if not name:
        raise ManifestError(f"cannot derive a Kubernetes name from {model!r}")
    return name[:63]


def _query(model: str, metric: str) -> str:
    sel = f'model_name=~"(?i)^{re.escape(model)}$"'
    if metric == "rps":
        return f"sum(rate(examlops_predict_requests_total{{{sel}}}[1m]))"
    return (
        "histogram_quantile(0.95, sum by (le) "
        f"(rate(examlops_predict_latency_seconds_bucket{{{sel}}}[5m])))"
    )


def _check(model: str, policy: AutoscalePolicy) -> None:
    if policy.target_metric not in SOURCED_METRICS:
        raise ManifestError(
            f"{model}: target_metric {policy.target_metric!r} has no per-model series in the "
            f"platform (only {', '.join(SOURCED_METRICS)}); refusing to render a dead trigger"
        )


def render_keda_scaledobject(
    model: str,
    policy: AutoscalePolicy,
    *,
    target_name: str | None = None,
    namespace: str | None = None,
    prometheus_url: str = "http://prometheus:9090",
) -> dict[str, Any]:
    """A KEDA ``ScaledObject`` (Prometheus trigger) for the model's predictor Deployment."""
    _check(model, policy)
    base = k8s_name(model)
    meta: dict[str, Any] = {"name": f"{base}-autoscale", "labels": {"examlops/model": base}}
    if namespace:
        meta["namespace"] = namespace
    spec: dict[str, Any] = {
        "scaleTargetRef": {"name": target_name or f"{base}-predictor"},
        "minReplicaCount": policy.min_replicas,
        "maxReplicaCount": policy.max_replicas,
        "cooldownPeriod": policy.cooldown_s,
        "pollingInterval": max(1, policy.stabilization_s // 2 or 1),
        "triggers": [
            {
                "type": "prometheus",
                "metadata": {
                    "serverAddress": prometheus_url,
                    "query": _query(model, policy.target_metric),
                    "threshold": f"{policy.target_value:g}",
                },
            }
        ],
    }
    return {"apiVersion": KEDA_API, "kind": "ScaledObject", "metadata": meta, "spec": spec}


def render_knative_overlay(
    model: str, policy: AutoscalePolicy, *, namespace: str | None = None
) -> dict[str, Any]:
    """A partial KServe ``InferenceService`` carrying Knative autoscaling — an overlay to merge into
    the manifest from ``exa serve manifest`` (``kubectl apply --server-side`` merges fields).

    Knative's built-in metrics are ``rps`` and ``concurrency``; ``p95`` is not one, so it is refused.
    Scale-to-zero maps to ``min-scale: 0`` plus a pod-retention period.
    """
    if policy.target_metric != "rps":
        raise ManifestError(
            f"{model}: the Knative overlay supports target_metric 'rps' only "
            f"(got {policy.target_metric!r}); use --kind keda for p95"
        )
    base = k8s_name(model)
    ann: dict[str, str] = {
        "autoscaling.knative.dev/metric": "rps",
        "autoscaling.knative.dev/target": f"{policy.target_value:g}",
        "autoscaling.knative.dev/min-scale": str(policy.min_replicas),
        "autoscaling.knative.dev/max-scale": str(policy.max_replicas),
    }
    if policy.scale_to_zero_after_s > 0:
        ann["autoscaling.knative.dev/scale-to-zero-pod-retention-period"] = (
            f"{policy.scale_to_zero_after_s}s"
        )
    meta: dict[str, Any] = {"name": base, "annotations": ann}
    if namespace:
        meta["namespace"] = namespace
    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": meta,
        "spec": {
            "predictor": {"minReplicas": policy.min_replicas, "maxReplicas": policy.max_replicas}
        },
    }


def render(kind: str, model: str, policy: AutoscalePolicy, **kw: Any) -> dict[str, Any]:
    if kind == "keda":
        return render_keda_scaledobject(model, policy, **kw)
    if kind == "knative":
        kw.pop("target_name", None)
        kw.pop("prometheus_url", None)
        return render_knative_overlay(model, policy, **kw)
    raise ManifestError(f"unknown kind {kind!r} (choose keda|knative)")
