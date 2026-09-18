# Runbooks

Every alert ExaMLOps ships (`platform/infra/docker-compose/alert_rules.yml`) carries a
`runbook_url` annotation that links to its section here. Alertmanager shows the link in each
notification, so the page that fired is one click from what it means and what to do.
`tests/unit/test_alert_runbooks.py` fails the build if an alert has no runbook, if the link points
at a section that does not exist, or — since 2026-09-14 — if a runbook names an **`exa` command the
CLI does not have**. The links were held honest from the start; the contents were not, and a page
whose value is that you can follow it at three in the morning is worth exactly as much as its
commands are real. All 90 `exa …` mentions across these runbooks resolve today.

Each section follows the same order:

- **Meaning:** the condition, in words, with its threshold.
- **Impact:** what users and the platform lose while it fires.
- **Check:** the commands and panels that tell you why.
- **Fix:** what to do for each cause, most likely first.

| Area | Page | Alerts |
|---|---|---|
| Model serving (Ray Serve, serving snapshot, error budget) | [Serving](serving.md) | 10 |
| Control plane (commands, approvals, Prefect dispatch) | [Control plane](control-plane.md) | 12 |
| Event backbone (outbox, relay, consumers) | [Event backbone](events.md) | 5 |
| Bus bridge (inference requests arriving over the message bus) | [Bus bridge](bus.md) | 5 |
| Dataplane (scheduled source pulls and snapshots) | [Dataplane](dataplane.md) | 3 |
| Monitoring stack (scrape targets, logs, traces, alert delivery) | [Platform](platform.md) | 6 |
| LLM endpoints (vLLM) | [LLM serving](llm.md) | 4 |

## Severity

| Severity | Meaning | Response |
|---|---|---|
| `critical` | Users are affected now, or will be within the hour | Page; start within minutes |
| `warning` | Something is degrading or a safety net is gone; nobody is failing yet | Next working hour; open a ticket |
| `none` | The heartbeat (`Watchdog`) | Never silence it |

## Service level objectives

These are the objectives the shipped alerts encode. Change the numbers in `alert_rules.yml` and
here together. Per-model quality SLOs (latency, groundedness, drift, with their own burn-rate
rules and a promotion gate) are declared with `exa slo`; see [Model-quality SLOs](../guides/slos.md).

| Service | SLI | Objective | Alerts |
|---|---|---|---|
| Prediction API | Share of `predict` requests that did not fail (`error`, `timeout`, `deadline_exceeded`) | **99.5 % over 30 days**, an error budget of 0.5 % | Fast burn: 14.4× the budget rate over 1 h (the whole budget in about 2 days). Slow burn: 3× over 6 h. Plus the plain error-rate alerts at 5 % and 20 % |
| Prediction API | p99 of prediction latency | **Under 1 s** (warning), never over 3 s (critical) | [RayServeHighLatencyP99](serving.md#rayservehighlatencyp99) |
| Model availability | Models loaded in Ray Serve | **Never zero** for 5 minutes | [RayServeNoModelsLoaded](serving.md#rayservenomodelsloaded) |
| Serving configuration | Replicas on the published snapshot generation | **All replicas current** within 5 minutes of a publish | [ServingSnapshotLagging](serving.md#servingsnapshotlagging) |
| Control plane | Scrape target up | **Down no longer than 2 minutes** | [ControlPlaneDown](control-plane.md#controlplanedown) |
| Retrain dispatch | Share of retrain dispatches that fail | **Under 20 %** over 15 minutes | [HighRetrainErrorRate](control-plane.md#highretrainerrorrate) |
| Event freshness | Age of the oldest unpublished outbox event | **Under 5 minutes** | [EventOutboxStalled](events.md#eventoutboxstalled) |
| Approvals | Age of the oldest pending approval | **Under 24 h** (warning), 72 h (critical, auto-expiry) | [ApprovalsStale](control-plane.md#approvalsstale) |

Requests that are the caller's fault are not failures: `invalid` (422, a malformed feature vector)
and `not_found` (404, a model or alias that does not exist) stay out of the error budget.
`tests/unit/test_error_alerts_see_every_failure.py` holds every `status` the serving code emits
to that classification, and keeps the Grafana SLO panels on the same selector as the alerts.

## Where to look

| What | Where |
|---|---|
| All services, their state and ports | `exa stack status` |
| A service's recent logs | `exa stack logs --service <service> --tail 200` |
| Platform status and pending approvals | `exa status` |
| Grafana: *ExaMLOps Online Metrics* (serving, SLO), *Drift* | http://localhost:13000 |
| Prometheus: alerts, targets, ad-hoc queries | http://localhost:19090 |
| Alertmanager: silences and inhibitions | http://localhost:19093 |
| Traces (Tempo) and logs (Loki) | Grafana → Explore |
| Audit trail of platform actions | `exa audit --last 1d` |
