# Grafana — Monitoring Dashboards

Grafana visualises the real-time metrics that Ray Serve, the control plane, and the SeanerBUS bridge export to Prometheus. Seven dashboards are provisioned automatically when the monitoring stack starts.

## Start / stop

```bash
# Start Prometheus + Grafana
make monitoring-up

# Stop
make monitoring-down
```

Access Grafana at **http://localhost:13000** (default login: `admin` / `admin`).

## Dashboards

All seven dashboards are pre-provisioned from `platform/infra/docker-compose/grafana/provisioning/dashboards/` and auto-refresh every 10 seconds.

### Overview (`examlops-overview`)

Platform-wide summary: 8-service health row, KPI stats (models loaded, approvals pending, retrain runs), retrain pipeline time series, inference activity, approval event log from Loki, and annotation overlays for retrain/approval events.

### Online Metrics (`examlops-online-metrics`)

SLO compliance and error budget row:

| Panel | Metric | Description |
|---|---|---|
| **SLO Compliance** | `rate(ray_examlops_predict_requests_total{status="error"}[5m])` | 30-day 0.5% error budget gauge |
| **Error Budget Remaining** | derived | Seconds of budget left; turns red when < 10% |
| **Burn Rate 1h / 6h / 24h** | derived | Rate × multiplier; >14.4× triggers SLO alert |
| **Request Rate** | `ray_examlops_predict_requests_total` | Predictions/s by model and outcome |
| **Latency p50 / p95 / p99** | `ray_examlops_predict_latency_seconds` | End-to-end prediction latency |
| **Per-Model Latency Table** | `ray_examlops_predict_latency_seconds` | p50/p95/p99/p05 by model |
| **Models Loaded** | `ray_examlops_models_loaded` | Production aliases in hot set |
| **Reload Count** | `ray_examlops_reload_total` | Hot-reload events |

### Control Plane (`examlops-control-plane`)

| Panel | Metric | Description |
|---|---|---|
| **Retrain Success / Error / Dedup** | `examlops_retrain_requests_total` | Counter by outcome |
| **Retrain Duration p50 / p99** | `examlops_retrain_duration_seconds` | Dispatch latency percentiles |
| **Circuit Breaker Timeline** | `examlops_prefect_circuit_breaker_opens_total` | CB open events |
| **Prefect Retry Rate** | `examlops_prefect_retries_total` | Retries/s |
| **Approval Funnel** | `examlops_approval_events_total` | Created → approved / rejected / expired |

### Drift (`examlops-drift`)

Prediction drift (median/p95/p05), input embedding norm/mean/std vs baseline, auto-retrain history log, and a Loki-based drift event panel.

### Approval Gate (`examlops-approvals`)

Queue depth gauges, approval funnel gauge, SLA risk gauge (oldest pending in days), event rate time series by model and action, auto-expiry trend, and a Loki approval event log.

### SeanerBUS Bridge (`examlops-seanerbus`)

Bridge health status, total inferences, per-model error rate %, combined p50/p95/p99 latency, per-model p99, retrain trigger rate, and a collapsible bridge log panel.

### Logs (`examlops-logs`)

Loki log explorer with service filter — select a `compose_service` to drill into any service's log stream.

## Distributed Tracing — Grafana Tempo

Grafana Tempo is the distributed trace backend for ExaMLOps. It receives traces from instrumented services via the OTLP/gRPC protocol and stores them locally for 48 hours. The Grafana UI queries Tempo and correlates traces with Loki logs.

**http://localhost:13200** (Tempo API; no browser UI — traces are explored through Grafana)

Start with the monitoring stack:

```bash
make monitoring-up   # starts Prometheus + Alertmanager + Tempo + Grafana + Loki + Promtail
```

### Grafana trace exploration

Open **Explore** in Grafana → select the **Tempo** datasource. Paste a `traceID` to see its span tree. The datasource is provisioned with trace→logs correlation: clicking a span shows the matching Loki log lines (±5 minutes around the span window).

### Enabling tracing in services

Tracing is off by default (`OTEL_SDK_DISABLED=true` in `.env` / compose). To enable it for a service:

```bash
# docker-compose .env
OTEL_SDK_DISABLED=false
OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4317   # already set in compose
```

Services that use the `opentelemetry-instrument` launcher (currently: `control-plane` and `dashboard`) auto-instrument all FastAPI routes and httpx calls. Services that import `examlops.observability` can call `setup_tracing("my-service")` for manual spans (e.g. Ray Serve replicas that cannot use the launcher).

### `examlops.observability` helper

`platform/cli/src/examlops/observability.py` provides a lightweight bootstrap wrapper:

```python
from examlops.observability import setup_tracing

# Call once at service startup.
# Returns True if configured, False if OTEL_SDK_DISABLED is set/truthy.
setup_tracing("ray-serve")
```

The helper reads `OTEL_SDK_DISABLED` (truthy → no-op), `OTEL_SERVICE_NAME`, and `OTEL_EXPORTER_OTLP_ENDPOINT` from the environment. Importing it never turns tracing on by surprise.

### Tempo configuration

`platform/infra/docker-compose/tempo-config.yml` — OTLP gRPC on `:4317`, HTTP on `:4318`, 48h block retention, local storage under `/var/tempo`.

## Alertmanager

Alertmanager handles alert routing, deduplication, silences, and inhibitions. It runs at **http://localhost:19093** as part of the `--profile monitoring` stack.

### Starting Alertmanager

```bash
make monitoring-up   # starts Prometheus + Alertmanager + Grafana + Loki + Promtail
```

Alertmanager is configured in `platform/infra/docker-compose/alertmanager.yml`. By default it uses the `default` receiver (no outbound delivery — alerts are visible only in the Alertmanager UI). To deliver to Slack, uncomment the `slack_configs` block and supply a webhook URL.

### Alert Rules

Rules are defined in `platform/infra/docker-compose/alert_rules.yml`, mounted into the Prometheus container. They reference metrics already emitted by the platform:

| Alert | Group | Severity | Condition |
|---|---|---|---|
| `RayServeHighErrorRate` | `examlops-serving` | warning | 5m error ratio > 5% for 10m |
| `RayServeHighErrorRateCritical` | `examlops-serving` | critical | 5m error ratio > 20% for 5m |
| `RayServeHighLatencyP99` | `examlops-serving` | warning | p99 > 1s for 10m |
| `RayServeHighLatencyP99Critical` | `examlops-serving` | critical | p99 > 3s for 5m |
| `RayServeNoModelsLoaded` | `examlops-serving` | critical | `ray_examlops_models_loaded == 0` for 5m |
| `RayServeReloadFailures` | `examlops-serving` | warning | Reload errors in last 15m |
| `SLOErrorBudgetFastBurn` | `examlops-serving` | critical | 1h burn rate > 14.4× (budget expires in < 2h) |
| `SLOErrorBudgetSlowBurn` | `examlops-serving` | warning | 6h burn rate > 3× for 60m |
| `SeanerBUSBridgeDown` | `examlops-seanerbus` | critical | `up{job="seanerbus_bridge"} == 0` for 2m |
| `SeanerBUSHighErrorRate` | `examlops-seanerbus` | warning | Bridge inference error ratio > 5% for 5m |
| `SeanerBUSHighLatencyP99` | `examlops-seanerbus` | warning | Bridge p99 latency > 500ms for 10m |
| `ControlPlaneDown` | `examlops-control-plane` | critical | `up{job="control_plane"} == 0` for 2m |
| `ApprovalsStale` | `examlops-control-plane` | warning | Oldest pending approval > 24h for 30m |
| `ApprovalsStaleUrgent` | `examlops-control-plane` | critical | Oldest pending approval > 72h (auto-expiry imminent) |
| `PendingApprovalQueueLarge` | `examlops-control-plane` | warning | > 10 approvals pending for 5m |
| `ApprovalMetricsUnreadable` | `examlops-control-plane` | warning | Scrape could not read the approval store in the last 10m — the two gauges above are stale |
| `HighRetrainErrorRate` | `examlops-control-plane` | warning | 15m retrain error ratio > 20% for 10m |
| `PrefectCircuitBreakerOpen` | `examlops-control-plane` | critical | CB open events in last 5m |
| `PrefectRetryRateHigh` | `examlops-control-plane` | warning | Prefect retry rate > 0.1/s for 10m |
| `RetrainDurationP99High` | `examlops-control-plane` | warning | Retrain dispatch p99 > 5min for 15m |
| `ManyApprovalsAutoExpired` | `examlops-control-plane` | warning | > 5 auto-expired approvals in 1h |
| `TargetDown` | `examlops-platform` | critical | Any Prometheus scrape target down for 5m |
| `RayServeTargetDown` | `examlops-platform` | critical | `up{job="ray_serve"} == 0` for 3m |
| `LokiDown` | `examlops-platform` | warning | `up{job="loki"} == 0` for 3m |
| `TempoDown` | `examlops-platform` | warning | `up{job="tempo"} == 0` for 3m |
| `AlertmanagerDown` | `examlops-platform` | warning | `up{job="alertmanager"} == 0` for 3m |

### Validating alert rules

```bash
make alerts-check   # runs promtool check rules inside Docker — also runs automatically in make ci-infra
```

## Prometheus data source

Grafana is pre-configured to use Prometheus as its data source:

```yaml
# platform/infra/docker-compose/grafana/provisioning/datasources/prometheus.yml
datasources:
  - name: Prometheus
    type: prometheus
    url: http://prometheus:9090
    isDefault: true
```

Prometheus scrapes these jobs every 15 seconds (configured in `platform/infra/docker-compose/prometheus.yml`).
The list matters beyond documentation: an alert that selects on a job nobody scrapes can never
fire, because `up{job="x"}` is *empty* rather than 0. `tests/unit/test_alert_rules_can_fire.py`
holds this table and the scrape config to each other.

| Job | Target | Purpose |
|---|---|---|
| `ray_serve` | `ray-serving:8080` | Ray Serve inference metrics |
| `control_plane` | `control-plane:8002` | Control plane metrics (retrain, approvals, circuit breaker) |
| `alertmanager` | `alertmanager:9093` | Alertmanager self-monitoring |
| `tempo` | `tempo:3200` | Trace-store self-monitoring |
| `loki` | `loki:3100` | Log-store self-monitoring |
| `seanerbus_bridge` | `seanerbus-bridge:8003` | Bus bridge: inference throughput, errors, latency |
| `dataplane` | `dataplane:8010` | dataplane pulls, freshness |
| `vllm` | `vllm:8000` | vLLM serving: TTFT, queue depth, KV-cache usage (GPU profile) |
| `fleet` | file_sd `/etc/prometheus/targets/*.json` | node_exporter / DCGM / HPC-launched vLLM endpoints, regenerated by `exa hpc prometheus-sd` |

## How metrics are emitted

Ray Serve uses the Ray Metrics API to emit metrics from each replica. These are picked up by Prometheus automatically — no extra instrumentation needed when you deploy a new model.

```python
# Inside MultiModelServer (serving/ray_serving/app.py)
self._req_counter = Counter(
    "ray_examlops_predict_requests_total",
    tag_keys=("model_name", "status"),
)
self._latency_hist = Histogram(
    "ray_examlops_predict_latency_seconds",
    boundaries=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
    tag_keys=("model_name",),
)
```

## Querying metrics in Prometheus

Open **http://localhost:19090** to run PromQL queries directly:

```promql
# Total successful predictions per model (last 5m)
rate(ray_examlops_predict_requests_total{status="success"}[5m])

# 95th percentile latency per model (last 5m)
histogram_quantile(0.95,
  rate(ray_examlops_predict_latency_seconds_bucket[5m])
)

# How many models are loaded right now
ray_examlops_models_loaded

# Approval queue depth
examlops_approvals_pending

# Prefect circuit breaker open events (last 1h)
increase(examlops_prefect_circuit_breaker_opens_total[1h])
```

## Adding custom metrics

**Ray Serve metrics:** Add a new `Counter`, `Gauge`, or `Histogram` in the `MultiModelServer.__init__` in `serving/ray_serving/app.py`, then increment/observe it in the relevant route. Prometheus picks it up on the next scrape — no Grafana changes needed unless you want a new panel.

**Control Plane metrics:** The approval gate uses the standard `prometheus_client` library (`platform/services/control_plane/metrics.py`). Add new metrics there and call the helpers from `app.py` endpoint handlers. The `/metrics` endpoint auto-includes all registered metrics on each scrape.

## Ports

| Service | Host port | Container port | Purpose |
|---|---|---|---|
| Grafana | 13000 | 3000 | Dashboard UI |
| Prometheus | 19090 | 9090 | Metrics query + storage |
| Alertmanager | 19093 | 9093 | Alert routing, silences, UI |
| Tempo | 13200 | 3200 | Distributed trace backend (OTLP gRPC :4317, HTTP :4318) |
| Loki | 13100 | 3100 | Log aggregation |
| Ray Metrics | 18080 | 8080 | Prometheus scrape target (Ray Serve) |
| Control Plane | 18002 | 8002 | Prometheus scrape target (retrain, approvals, circuit breaker metrics) |

## Grafana credentials

Default: `admin` / `admin`. You will be prompted to change the password on first login. This can be overridden with the `GF_SECURITY_ADMIN_PASSWORD` env var in Docker Compose.

## Alerting

Prometheus evaluates `platform/infra/docker-compose/alert_rules.yml` and routes firing alerts to
**Alertmanager** (`prom/alertmanager`, http://localhost:19093). 25 rules span four groups:
`examlops-serving` (SLO burn-rate, error rate, latency, model count),
`examlops-seanerbus` (bridge up, error rate, p99),
`examlops-control-plane` (retrain errors, CB open, approval SLA, auto-expiry),
and `examlops-platform` (Loki, Tempo, Alertmanager, Ray Serve target health).
Validate the rules with `make alerts-check`.

The default Alertmanager receiver has no external delivery — alerts are visible in the
Alertmanager UI and via Prometheus → Alerts (http://localhost:19090/alerts). To page a channel,
uncomment the `slack_configs` template in `platform/infra/docker-compose/alertmanager.yml` and
supply a webhook URL.

## Distributed tracing (Phase 17)

OpenTelemetry traces are exported to **Grafana Tempo** (http://localhost:13200), provisioned as a
Grafana datasource with trace→logs correlation to Loki. Tracing is **off by default**; enable it:

```bash
OTEL_SDK_DISABLED=false make stack-up
make monitoring-up
```

Then generate traffic (e.g. `exa serve check` or a `/retrain`) and open Grafana → Explore →
Tempo → Search. A pipeline request shows the
`inference_pipeline.ingress → inference_pipeline.model_router` span hierarchy; the control plane
and dashboard are auto-instrumented via `opentelemetry-instrument`. Switch off again by unsetting
`OTEL_SDK_DISABLED` (defaults to `true`).
