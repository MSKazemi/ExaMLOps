"""A metric that is never published is not a metric reading zero, and `== 0` cannot see it.

`RayServeNoModelsLoaded` is `sum(ray_examlops_models_loaded) == 0`, annotated "inference is
impossible". In PromQL, `sum()` over a series that does not exist yields an **empty vector**, so
`== 0` matches nothing at all. The gauge was only ever written after a successful registry scan:
`_load_hot_aliases` returns early when MLflow is unreachable, before the `set()` at the end. So a
replica that came up while MLflow was down published no series, and the alert for "no models
loaded" was structurally silent in the one case it names.

Nothing else covered it either. `up{job="ray_serve"}` is 1 — Ray Serve is healthy, it just has no
models. And every request resolves to `status="not_found"`, which the error-rate and SLO burn
alerts deliberately exclude, because a 404 for a model the caller named is normally the caller's
mistake. MLflow down at replica start therefore meant 100% of inference failing with not one alert
in the file firing.

Note which direction the bug ran: with MLflow *reachable* but holding no aliased models, `set(0)`
was called, the series existed, and the alert fired correctly. It worked in the case someone would
notice anyway and was silent in the severe one.
"""

from __future__ import annotations

import re
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from serving.ray_serving import app as rs_app  # noqa: E402

_RULES = REPO_ROOT / "platform" / "infra" / "docker-compose" / "alert_rules.yml"


def _server(hot: dict | None = None) -> rs_app.MultiModelServer:
    cls = rs_app.MultiModelServer.func_or_class
    server = object.__new__(cls)
    server._cache_lock = threading.RLock()
    server._hot = dict(hot or {})
    server._version_cache = OrderedDict()
    server._version_cache_size = 8
    server._preload_aliases = ["Production"]
    server._replica_id = "test"
    for attr in ("_req_counter", "_models_gauge", "_reload_counter", "_version_gauge"):
        setattr(server, attr, MagicMock())
    return server


def _unreachable_registry():
    """MLflow answering the way a downed server does: the scan raises, every retry included."""
    client = MagicMock()
    client.search_registered_models.side_effect = ConnectionError("no route to MLflow")
    return patch.object(rs_app.mlflow, "MlflowClient", return_value=client)


def _gauge_values(server) -> list[float]:
    return [
        c.args[0] if c.args else c.kwargs.get("value") for c in server._models_gauge.set.mock_calls
    ]


def test_an_unreachable_registry_still_publishes_the_gauge():
    """The regression: MLflow down at start must leave a 0 behind, not nothing."""
    server = _server()
    with _unreachable_registry():
        server._load_hot_aliases()
    assert server._models_gauge.set.called, (
        "the hot-set loader returned without publishing models_loaded. The series then does not "
        "exist, and `sum(...) == 0` aggregates an absent series to an empty vector — "
        "RayServeNoModelsLoaded cannot fire in the one case it is named for."
    )
    assert _gauge_values(server) == [0]


def test_a_registry_that_goes_down_later_does_not_claim_the_cache_is_empty():
    """The control, and the reason the fix is not a blind `set(0)`.

    The poller calls this again on a live replica. The early return leaves the hot set untouched
    and still serving from cache, so reporting 0 there would page for an outage that is not
    happening.
    """
    server = _server({("JPCP", "Production"): {"model": object(), "version": "3"}})
    with _unreachable_registry():
        server._load_hot_aliases()
    assert _gauge_values(server) == [1], "a cached, still-serving model was reported as absent"


def test_a_reachable_but_empty_registry_still_reports_zero():
    """The case that always worked — pinned so the fix cannot regress it."""
    server = _server()
    client = MagicMock()
    client.search_registered_models.return_value = []
    with patch.object(rs_app.mlflow, "MlflowClient", return_value=client):
        server._load_hot_aliases()
    assert _gauge_values(server)[-1] == 0


# ── the general rule this instance belongs to ────────────────────────────────────────────
#
# Comparing a metric to a constant is a claim about a *value*, and a value comparison can never
# detect that the series is gone. Every such alert needs either a metric that is published
# unconditionally, or an `absent()` arm. `up` is exempt: Prometheus synthesises it for every
# configured target, so it is never missing while the target is configured.


_PROMQL_WORDS = {
    "sum",
    "rate",
    "increase",
    "irate",
    "delta",
    "avg",
    "min",
    "max",
    "count",
    "absent",
    "avg_over_time",
    "max_over_time",
    "min_over_time",
    "clamp_min",
    "clamp_max",
    "vector",
    "histogram_quantile",
    "by",
    "without",
    "on",
    "ignoring",
    "and",
    "or",
    "unless",
    "le",
    "job",
    "status",
    "model_name",
    "alias",
    "error",
    "timeout",
    "success",
    "offset",
}


# Metrics exempt from the absent()-arm requirement below, each for a documented reason the
# regex-level guard cannot see on its own — not a blanket escape hatch. Extend it only with the
# same care.
#
#   dataplane_catalog_up   published unconditionally, every scrape, with or without any source
#                          registered (examlops.dataplane.service.app._freshness_collector's
#                          `catalog_up` gauge) — the guard's own stated exception ("a metric that
#                          is published unconditionally") applies directly.
#   dataplane_source_up    every *registered* source always gets a series, including a 0 when
#                          its own catalog read fails (same collector, fix round 1: the source
#                          loop used to `continue` — skipping the series entirely — on a read
#                          error, which is exactly the bug this guard exists to catch; it now
#                          emits 0 instead). So the whole series being absent means "not a
#                          registered source", not "outage" — a catalog-wide outage is what
#                          `dataplane_catalog_up` (and `DataplaneCatalogUnavailable`) is for.
#   envoy_cluster_membership_healthy / _total
#                          Envoy publishes both for every configured cluster on every scrape, so
#                          they are missing only when Envoy is not scraped — which is
#                          `ServingGatewayDown` (`up{job=~"gateway|gateway_authz"} == 0`), the same
#                          argument the guard already makes for `up` itself. An `absent()` arm here
#                          would fire on every site that does not deploy the gateway at all (it is
#                          opt-in, behind a Compose profile and `gateway.enabled`), and a rule that
#                          fires forever on a healthy platform is how alerting gets switched off.
#   examlops_feature_view_stale
#                          one series per registered feature view (ADR 0017), so no series is
#                          the healthy state of a site with no views, and an `absent()` arm
#                          would page there forever. The outage it would otherwise hide, a
#                          failed registry read on the scrape, is `FeatureFreshnessUnreadable`
#                          (`examlops_feature_freshness_read_errors_total`, exported from
#                          import).
#   llm_gateway_provider_up / llm_gateway_breaker_state
#                          one series per currently-configured provider / (provider, model)
#                          deployment, published unconditionally inside the `/metrics` handler
#                          itself (gateway/service/app.py's `metrics_endpoint`) for whatever is in
#                          the live routing table right now. A specific series disappearing means
#                          "no longer configured" (an operator's own `exa gateway reload`, e.g.
#                          removing a provider), not an outage of it — an `absent()` arm would page
#                          on every intentional config change. The gateway's own outage is
#                          `LLMGatewayDown` (`up{job="llm_gateway"} == 0`), the same argument the
#                          guard already makes for `up` itself; a total scrape failure takes the
#                          whole metric family down at once, which that alert already covers.
_EXEMPT_METRICS = {
    "examlops_feature_view_stale",
    "dataplane_catalog_up",
    "dataplane_source_up",
    "envoy_cluster_membership_healthy",
    "envoy_cluster_membership_total",
    "llm_gateway_provider_up",
    "llm_gateway_breaker_state",
}


def test_every_equality_alert_can_still_see_an_absent_series():
    rules = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    checked, offenders = 0, []
    for g in rules.get("groups", []):
        for r in g.get("rules", []):
            if "alert" not in r:
                continue
            expr = " ".join(r["expr"].split())
            if not re.search(r"==\s*[0-9.]+", expr):
                continue
            # Strip label selectors first: `up{job="loki"}` names one metric, `up`. The values
            # inside the braces are strings, and reading them as metric names flagged every
            # target-down alert in the file.
            bare = re.sub(r"\{[^}]*\}", "", expr)
            # Likewise a grouping clause names labels, not metrics: `max by (view) (m) == 1`.
            bare = re.sub(r"\b(?:by|without)\s*\([^)]*\)", "", bare)
            names = set(re.findall(r"\b[a-z_][a-z0-9_:]*\b", bare)) - _PROMQL_WORDS
            # `up` is exempt: Prometheus synthesises it for every configured target, so it is
            # never missing while the target is configured. Absence there means "not scraped
            # at all", which is a different alert's job. See `_EXEMPT_METRICS` for the others.
            metrics = names - {"up"} - _EXEMPT_METRICS
            if not metrics:
                continue
            checked += 1
            if "absent(" not in expr:
                offenders.append(f"{r['alert']}: {expr}")
    assert checked, "found no equality alerts to check — the parse is stale, not the file clean"
    assert not offenders, (
        "these alerts compare a metric to a constant with no `absent()` arm, so they go quiet "
        "when the series stops being published rather than firing: " + "; ".join(offenders)
    )
