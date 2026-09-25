"""The per-model PromQL behind every autoscale signal (ADR 0031 clause 1).

One place, used by both consumers of a signal — the in-process controller
(:class:`examlops.autoscale.controller.PrometheusSignals`) and the KEDA generator
(:mod:`examlops.autoscale.manifests`) — so the trigger KEDA evaluates is, byte for byte, the query
the controller reads. A metric with no query here has **no source**, and both consumers treat it
as absent (the controller holds; the generator refuses to render a dead trigger).

Sources, and exactly what each measures:

* ``rps`` — ``sum(rate(examlops_predict_requests_total[1m]))``, requests per second.
* ``p95`` — the 95th percentile of ``examlops_predict_latency_seconds`` over 5 minutes.
* ``queue_depth`` — **mean requests in the system** (queued + executing) over 1 minute, by Little's
  law ``L = λ·W``: ``sum(rate(examlops_predict_latency_seconds_sum[1m]))`` is the seconds of
  request time accrued per second, which *is* the time-averaged number of requests in flight. It is
  the quantity Knative's ``concurrency`` metric and Ray Serve's ``target_ongoing_requests`` scale
  on, and it needs no new series. It lags a burst by the rate window — a leading, per-replica queue
  gauge would be better and is not exported by the model server.
* ``gpu_util`` — **no default**: GPU utilisation comes from the site's exporter (DCGM,
  ``nvidia_smi_exporter``, …) whose labels only the site knows. Set
  ``EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY`` to a PromQL template; ``{model}`` is replaced by the
  regex-escaped model name and ``{k8s_name}`` by its DNS-1123 form, e.g.
  ``avg(DCGM_FI_DEV_GPU_UTIL{pod=~"{k8s_name}-predictor-.*"})``.

``EXAMLOPS_AUTOSCALE_QUEUE_DEPTH_QUERY`` overrides the ``queue_depth`` default the same way (a KServe
site can point it at the Knative queue-proxy's ``revision_app_request_concurrency``).
"""

from __future__ import annotations

import os
import re

SIGNAL_NAMES = ("rps", "p95", "queue_depth", "gpu_util")
MODEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_K8S_RE = re.compile(r"[^a-z0-9-]+")
_TEMPLATE_ENV = {
    "queue_depth": "EXAMLOPS_AUTOSCALE_QUEUE_DEPTH_QUERY",
    "gpu_util": "EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY",
}
_MAX_TEMPLATE = 2000


class QueryTemplateError(ValueError):
    """An operator-supplied query template is unusable (it is ignored, never half-applied)."""


def promql_regex(model: str) -> str:
    """``model`` as a regex, escaped for a double-quoted PromQL string literal.

    PromQL string literals follow Go's escape rules, so ``re.escape``'s ``\\.`` / ``\\-`` are
    *unknown escape sequences* the lexer rejects — the query would fail and a model named
    ``demo.v2`` or ``my-model`` would be held forever as "signal source down". Doubling the
    backslash makes the lexer hand the regex engine exactly ``re.escape(model)``.
    """
    return re.escape(model).replace("\\", "\\\\")


def selector(model: str) -> str:
    return f'model_name=~"(?i)^{promql_regex(model)}$"'


def _k8s(model: str) -> str:
    return _K8S_RE.sub("-", model.lower()).strip("-")[:63]


def _template(metric: str, model: str) -> str | None:
    env = _TEMPLATE_ENV.get(metric)
    raw = (os.getenv(env) or "").strip() if env else ""
    if not raw:
        return None
    if len(raw) > _MAX_TEMPLATE or ("{model}" not in raw and "{k8s_name}" not in raw):
        # A template that names no model would scale every model on one fleet-wide number.
        raise QueryTemplateError(
            f"{env} must be at most {_MAX_TEMPLATE} chars and contain {{model}} or {{k8s_name}}"
        )
    return raw.replace("{model}", promql_regex(model)).replace("{k8s_name}", _k8s(model))


def query_for(metric: str, model: str) -> str | None:
    """The PromQL for ``metric`` of ``model``, or ``None`` when the platform has no source.

    ``model`` must match :data:`MODEL_RE` (it is interpolated into a matcher); anything else has no
    query at all. Raises :class:`QueryTemplateError` for an unusable operator template.
    """
    if metric not in SIGNAL_NAMES or not MODEL_RE.match(model or ""):
        return None
    templated = _template(metric, model)
    if templated is not None:
        return templated
    sel = selector(model)
    if metric == "rps":
        return f"sum(rate(examlops_predict_requests_total{{{sel}}}[1m]))"
    if metric == "p95":
        return (
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(examlops_predict_latency_seconds_bucket{{{sel}}}[5m])))"
        )
    if metric == "queue_depth":
        return f"sum(rate(examlops_predict_latency_seconds_sum{{{sel}}}[1m]))"
    return None  # gpu_util: only an operator template can name the exporter


def sourced_metrics(model: str = "m") -> tuple[str, ...]:
    """The metrics that currently have a query (``gpu_util`` only with a template set)."""
    out = []
    for m in SIGNAL_NAMES:
        try:
            if query_for(m, model) is not None:
                out.append(m)
        except QueryTemplateError:
            continue
    return tuple(out)


__all__ = [
    "MODEL_RE",
    "QueryTemplateError",
    "SIGNAL_NAMES",
    "query_for",
    "promql_regex",
    "selector",
    "sourced_metrics",
]
