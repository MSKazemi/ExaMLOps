---
title: Follow the signals
description: How ExaMLOps is observed and how it keeps evidence — metrics, alerts, logs, traces, the hash-chained audit trail, signed checkpoints and the event outbox.
hide:
  - navigation
  - toc
---

# Follow the signals

This is the **signal line**. Two kinds of signal leave the platform. **Telemetry** tells you
how it is running: metrics, alerts, logs and traces. **Evidence** tells you what it did and who
decided: the hash-chained audit trail, its signed checkpoints, and the events other systems
subscribe to.

<div class="xm-player" data-scene="signals" markdown>
<ol class="xm-steps">
<li data-focus="ray,cp,bridge,prom" data-run="ray-prom,cp-prom,bridge-prom" data-actor="Prometheus" data-line="observe"><strong>Prometheus scrapes the serving, control and bridge services.</strong> Every 15 seconds: Ray Serve (requests, latency, loaded versions), the control plane (retrains, Prefect retries, circuit-breaker opens, approvals), the bus bridge, and fleet exporters. Metrics are kept for 7 days.</li>
<li data-focus="prom,alert,pager" data-run="prom-alert;alert-pager" data-actor="Alertmanager" data-line="observe"><strong>Rules turn metrics into alerts.</strong> 31 rules — for example Ray Serve errors above 5% for 10 minutes (warning) or 20% for 5 (critical), p99 latency, no models loaded, a bridge down, SLO burn. Alertmanager groups them and pages on critical ones.</li>
<li data-focus="containers,promtail,loki" data-run="containers-promtail;promtail-loki" data-actor="Promtail" data-line="observe"><strong>Logs flow without code changes.</strong> Promtail tails the output of every container in the ExaMLOps compose project through the Docker API and pushes it to Loki, labelled by compose service and container. Logs are kept for 7 days.</li>
<li data-focus="ray,tempo" data-run="ray-tempo" data-actor="OpenTelemetry" data-line="observe"><strong>Traces go to Tempo — when you turn them on.</strong> The control plane and dashboard are auto-instrumented and the inference pipeline adds its own spans. Tracing is off until <code>OTEL_SDK_DISABLED=false</code>. The inference pipeline then samples 5% of traces; the auto-instrumented services keep every trace unless <code>OTEL_TRACES_SAMPLER</code> says otherwise.</li>
<li data-focus="prom,tempo,loki,grafana" data-run="prom-grafana,tempo-grafana,loki-grafana" data-actor="Grafana" data-line="observe"><strong>Grafana reads all three.</strong> Seven provisioned dashboards — overview, online metrics, control plane, drift, approvals, logs and bus — and it can be embedded in the ExaMLOps dashboard.</li>
<li data-focus="writers,bridge,audit" data-run="writers-audit,bridge-audit" data-actor="Audit trail" data-line="human"><strong>Every governed action is appended to the audit trail.</strong> Retrains, approvals, promotions, cluster admissions, agent writes and each inference served over the bus. Every event's hash covers the one before it, so a changed record breaks the chain.</li>
<li data-focus="audit,checkpoint,worm" data-run="audit-checkpoint;checkpoint-worm" data-actor="Operator" data-line="observe"><strong>Checkpoints are signed and anchored outside the database.</strong> <code>exa audit checkpoint</code> signs the chain head and — when <code>EXAMLOPS_AUDIT_WORM_PATH</code> is set — appends it to an append-only anchor file; <code>exa audit verify-worm</code> checks the chain against that anchor.</li>
<li data-focus="cp2,outbox,relay,publisher" data-run="cp2-outbox;outbox-relay;relay-publisher" data-actor="Relay" data-line="observe"><strong>Events leave through an outbox.</strong> The control plane writes an event in the same transaction as the change it describes. A relay inside the control plane publishes them every second — to the log by default, or to Redis Streams — at least once, with stable ids.</li>
</ol>
</div>

!!! note "Drift is watched by the drift trigger, not by an alert rule"
    None of the 31 alert rules looks at the drift z-score. Drift is measured from the bridge's
    snapshots by `exa drift status` and acted on by `exa drift trigger` or the autopilot — see
    [Follow a retrain](retrain.md).

## Where each signal lives

| Signal | Store | Kept for | Read it with |
|---|---|---|---|
| Metrics | Prometheus | 7 days | Grafana, dashboard consoles |
| Alerts | Alertmanager | until resolved | paging and chat receivers, dashboard Alerts |
| Logs | Loki | 7 days | Grafana, dashboard service log tail |
| Traces | Tempo | 48 hours | Grafana (Tempo data source) |
| Audit trail | `audit_events` in the platform datastore | not touched by `exa data retention-prune` | `exa audit`, `exa audit verify` |
| Checkpoints | `audit_checkpoints`, plus the anchor file when `EXAMLOPS_AUDIT_WORM_PATH` is set | permanent | `exa audit verify-worm` |
| Events | `event_outbox` | kept; published rows are stamped, failed ones stay as evidence | `exa events stats`, `exa events relay` |

## Try it

```bash
make monitoring-up          # Prometheus, Alertmanager, Grafana, Loki, Promtail, Tempo
make alerts-check           # validate the alert rules with promtool
exa audit --last 7d --model JPCP
exa audit verify
exa audit checkpoint && exa audit verify-worm
exa events stats
```

## Read more

- [Grafana and monitoring](../components/grafana.md)
- [Audit trail](../guides/audit-trail.md) and [evidence chain](../guides/evidence-chain.md)
- [GenAI observability](../guides/genai-observability.md) and [SLOs](../guides/slos.md)
- [Dashboard self-observability](../guides/dashboard-self-observability.md)
