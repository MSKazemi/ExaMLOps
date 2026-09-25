"""An alert that cannot fire looks exactly like an alert that never needed to.

`make alerts-check` runs promtool, which proves the rules *parse*. It cannot prove they can
ever match a series, and that is the failure that matters: a rule referencing a job nobody
scrapes, or a liveness gauge that is only ever set to 1, is a permanently silent alarm that
reads on the dashboard as healthy monitoring.

Two properties, each checked in both directions so neither the alert nor the thing it depends
on can drift away from the other without a red test.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "platform" / "infra" / "docker-compose"
_RULES = _COMPOSE / "alert_rules.yml"
_PROM = _COMPOSE / "prometheus.yml"

# Jobs that arrive from file-based service discovery rather than a static target still have a
# `job_name`, so nothing is exempt here; this stays empty on purpose.
_JOBS_NOT_IN_PROMETHEUS_YML: set[str] = set()


def _alerts() -> list[dict]:
    doc = yaml.safe_load(_RULES.read_text())
    return [rule for group in doc["groups"] for rule in group.get("rules", []) if "alert" in rule]


def _scrape_jobs() -> set[str]:
    doc = yaml.safe_load(_PROM.read_text())
    return {sc["job_name"] for sc in doc.get("scrape_configs", [])}


def _jobs_referenced(expr: str) -> set[str]:
    """Every job name an expression selects on, expanding `job=~"a|b"` alternations."""
    found: set[str] = set()
    for op, value in re.findall(r'job\s*(=~|=)\s*"([^"]+)"', expr):
        found.update(value.split("|") if op == "=~" else [value])
    return found


def test_every_job_an_alert_selects_on_is_actually_scraped():
    """`up{job="x"}` is empty — not zero — when nothing scrapes x, so the alert never fires."""
    scraped = _scrape_jobs() | _JOBS_NOT_IN_PROMETHEUS_YML
    dangling = {
        a["alert"]: sorted(_jobs_referenced(str(a["expr"])) - scraped)
        for a in _alerts()
        if _jobs_referenced(str(a["expr"])) - scraped
    }
    assert not dangling, (
        f"these alerts select on jobs prometheus.yml never scrapes, so they can never fire: "
        f"{dangling}. Scraped jobs: {sorted(scraped)}"
    )


# Gauges that a `prometheus_client` Collector recomputes from durable stored state on every
# scrape — not a flag the service sets once in memory and never revisits — so they cannot go
# stale the way a self-reported liveness gauge does; a genuinely failed reading reads 0 for as
# long as it stays the latest one, scrape after scrape.
# `dataplane_source_up` (platform/cli/src/examlops/dataplane/service/app.py
# `_freshness_collector`) queries the pull catalog fresh each time, including while the
# dataplane service itself is perfectly healthy and only one source's last pull failed.
# `dataplane_catalog_up` is the same collector's whole-catalog gauge: it queries
# `list_source_defs()` fresh on every scrape and reads 0 for exactly as long as that call keeps
# raising, never merely going stale.
_RECOMPUTED_GAUGES = {
    "dataplane_source_up",
    "dataplane_catalog_up",
    # llm_gateway_provider_up: set fresh from `state.probe_all(...)`'s current probe results
    # inside the `/metrics` handler itself (gateway/service/app.py's `metrics_endpoint`) — every
    # scrape recomputes it from live probe state, the same "a Collector recomputes from durable
    # state on every scrape" shape as the two dataplane gauges above, not a value the process sets
    # once and leaves. If the llm-gateway process itself dies, `/metrics` becomes unreachable
    # entirely (LLMGatewayDown, `up{job="llm_gateway"} == 0`, covers that) rather than this gauge
    # freezing at a stale non-zero value.
    "llm_gateway_provider_up",
}


def test_no_alert_detects_downtime_with_a_self_reported_liveness_gauge():
    """A process cannot publish its own death.

    A gauge the service sets to 1 while it runs goes *stale*, not to 0, when the service dies —
    so `<service>_up == 0` matches nothing at exactly the moment it is needed. Down-detection
    belongs to Prometheus's own `up`, which the scrape sets to 0 for us. Exempted: gauges a
    Collector recomputes from durable state on every scrape (see `_RECOMPUTED_GAUGES`).
    """
    offenders = {}
    for a in _alerts():
        expr = str(a["expr"])
        for metric in re.findall(r"\b([a-z_][a-z0-9_]*_up)\s*==\s*0", expr):
            if metric != "up" and metric not in _RECOMPUTED_GAUGES:
                offenders[a["alert"]] = metric
    assert not offenders, (
        f"these alerts test a service-published liveness gauge for 0, which never happens: "
        f'{offenders}. Use up{{job="<job>"}} == 0 instead.'
    )


def test_the_bridge_liveness_gauge_is_in_fact_only_ever_set_to_one():
    """Pins the premise of the test above, so it cannot be dismissed as theoretical."""
    src = (
        Path(__file__).resolve().parents[2] / "platform" / "clients" / "dataplane_bus_bridge.py"
    ).read_text()
    sets = re.findall(r"_BRIDGE_UP\.set\(([^)]*)\)", src)
    assert sets, "the bridge no longer has a liveness gauge — update this test with it"
    assert all(float(v) == 1.0 for v in sets), (
        f"_BRIDGE_UP is now set to values other than 1.0 ({sets}); if the bridge can publish its "
        "own shutdown, the alert may legitimately test it for 0"
    )


def test_loki_is_monitored_at_all():
    """Loki holds the platform's logs; losing it silently is the worst case for an audit trail."""
    assert "loki" in _scrape_jobs(), (
        "nothing scrapes Loki, so neither LokiDown nor the generic TargetDown can fire for it"
    )


def test_the_documented_scrape_table_matches_prometheus_yml():
    """The guide's job table is what a reader consults before writing an alert.

    It said Prometheus scraped *three* services and listed three, when the config had seven — so
    a reader deciding which job label to select on was reading a list with most of the answers
    missing, which is how a dangling `job=` gets written in the first place.
    """
    doc = (Path(__file__).resolve().parents[2] / "docs" / "components" / "grafana.md").read_text()
    section = doc.split("| Job | Target | Purpose |", 1)[1].split("\n\n", 1)[0]
    documented = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", section, re.M))
    assert documented == _scrape_jobs(), (
        f"docs/components/grafana.md lists {sorted(documented)} but prometheus.yml scrapes "
        f"{sorted(_scrape_jobs())}"
    )


# ── and the metric it names must be one the platform emits ───────────────────


#: PromQL's own vocabulary — functions, modifiers and label names, not metrics.
_PROMQL = {
    "rate",
    "irate",
    "increase",
    "sum",
    "avg",
    "max",
    "min",
    "count",
    "by",
    "without",
    "on",
    "group_left",
    "group_right",
    "absent",
    "absent_over_time",
    "histogram_quantile",
    "time",
    "changes",
    "delta",
    "predict_linear",
    "clamp_max",
    "clamp_min",
    "le",
    "job",
    "instance",
    "unless",
    "and",
    "or",
    "offset",
    "bool",
    "ignoring",
    "topk",
    "bottomk",
    "quantile",
    "stddev",
    "last_over_time",
    "avg_over_time",
    "max_over_time",
    "min_over_time",
    "sum_over_time",
    "count_over_time",
    "vector",
    "scalar",
    "round",
    "ceil",
    "floor",
    "abs",
    "deriv",
    "idelta",
    "resets",
    "label_replace",
    "label_join",
    "if",
    "for",
    "namespace",
    "container",
    "pod",
    "node",
    "service",
    "endpoint",
    "alertname",
    "severity",
    "cluster",
    "reason",
    "status",
    "cause",
    "model",
    "tenant",
    "topic",
    "table",
    "kind",
    "mode",
    "backend",
    "engine",
    "source",
    "target",
}


#: Metrics **other software** exports, which no amount of scanning our own code will find. Each is
#: named with what emits it, so an entry cannot quietly become an excuse for a typo in ours.
_THIRD_PARTY = {
    "envoy_cluster_membership_healthy",  # Envoy, the serving gateway
    "envoy_cluster_membership_total",  # Envoy
    "envoy_cluster_upstream_rq_timeout",  # Envoy
    "envoy_http_downstream_rq_xx",  # Envoy
    "prometheus_notifications_dropped_total",  # Prometheus itself
    "alertmanager_notifications_failed_total",  # Alertmanager
    "up",  # Prometheus' own scrape liveness series
    "scrape_duration_seconds",  # Prometheus
    "envoy_http_ext_authz_error",  # Envoy's ext_authz filter
    "envoy_http_local_rate_limit_rate_limited",  # Envoy's local rate limiter
    "time_to_first_token_seconds",  # vLLM's OpenAI server
}


def _metrics_in_alerts() -> dict[str, set[str]]:
    """Metric name → the alerts that depend on it."""
    import yaml

    rules = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for group in rules.get("groups", []):
        for rule in group.get("rules", []):
            if "alert" not in rule:
                continue
            # Label *values* are not metric names: `envoy_cluster_name="ray_serving_grpc"` names a
            # cluster, and reading it as a series to check produced three phantom orphans. Quoted
            # strings are removed before anything is extracted.
            expr = re.sub(r"\"[^\"]*\"|'[^']*'", " ", rule.get("expr", ""))
            for name in re.findall(r"\b([a-z_][a-z0-9_]{4,})\b(?!\s*=)", expr):
                if name in _PROMQL:
                    continue
                found.setdefault(name, set()).add(rule["alert"])
    assert len(found) > 30, f"only {len(found)} metric names parsed — the extraction is broken"
    return found


def test_every_metric_an_alert_watches_is_one_the_platform_emits():
    """A rule on a metric nobody exports is a permanently silent alarm.

    It is the same failure this file is about, one level in: `promtool` proves the rule parses and
    the scrape checks prove the *job* exists, but a misspelt or removed metric name still yields an
    expression that can never match a series — and a rule that never matches reads on the dashboard
    exactly like a system with nothing wrong.

    Checked against the working tree rather than `git grep`, because most of this repository's
    in-flight work is uncommitted and the index would not see the exporter that emits the series.

    **Only Python is scanned, and that is the whole trick.** The first version of this test scanned
    `platform/` wholesale — which contains `alert_rules.yml` itself, so every metric name was
    trivially "found" and the check passed for any string at all. It was vacuous until a mutant with
    one letter changed sailed through it. The corpus must never include the subject.
    """
    import subprocess

    emitted = set(
        subprocess.run(
            ["grep", "-rhoE", "--include=*.py", r"[a-z_][a-z0-9_]{4,}",
             "platform", "serving", "pipelines"],
            cwd=_ROOT, capture_output=True, text=True, timeout=300,
        ).stdout.split()
    )  # fmt: skip
    assert len(emitted) > 1000, "the tree scan found almost nothing — its roots are stale"
    emitted |= _THIRD_PARTY

    # Four transformations sit between what the code declares and what Prometheus stores, and all
    # four are real series rather than sloppiness:
    #   • Ray Serve namespaces a deployment's metrics with `ray_`;
    #   • a histogram named `x_seconds` yields `x_seconds_bucket`, `_count` and `_sum`;
    #   • a `prometheus_client.Counter("x", ...)` exposes as `x_total` (and `x_created`) —
    #     the library's own well-documented auto-suffix, the same class of transformation as the
    #     histogram one above, just for the other metric type this platform actually uses
    #     (`gateway/service/app.py`'s `_Metrics.requests` declares `"llm_gateway_requests"`,
    #     scraped as `llm_gateway_requests_total`; verified against a real `Counter` +
    #     `generate_latest()` round trip, not assumed from documentation);
    #   • a recording rule in this very file defines a series nothing exports directly.
    recorded = {
        rule["record"]
        for group in yaml.safe_load(_RULES.read_text(encoding="utf-8")).get("groups", [])
        for rule in group.get("rules", [])
        if "record" in rule
    }

    def declared(metric: str) -> bool:
        candidates = {metric}
        if metric.startswith("ray_"):
            candidates.add(metric[len("ray_") :])
        for suffix in ("_bucket", "_count", "_sum", "_total"):
            candidates |= {c[: -len(suffix)] for c in set(candidates) if c.endswith(suffix)}
        return bool(candidates & emitted) or metric in recorded

    orphans = {m: a for m, a in _metrics_in_alerts().items() if not declared(m)}
    assert not orphans, "alerts watch metrics nothing in the tree emits:\n  " + "\n  ".join(
        f"{metric} — watched by {', '.join(sorted(alerts))}"
        for metric, alerts in sorted(orphans.items())
    )
