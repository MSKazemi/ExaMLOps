# Runbooks: monitoring stack

Alerts about the monitoring pipeline itself: scrape targets, Loki, Tempo, Alertmanager, and the
heartbeat that proves alerts can still be delivered. A monitoring failure hides everything else,
so these deserve attention even when nothing looks broken.

## TargetDown {#targetdown}

**Meaning:** Prometheus has not reached a scrape target (`job`, `instance` in the alert) for 5
minutes.

**Impact:** that service is either down or dark. Its own alerts cannot fire, so check which it is.

**Check:** `exa stack status` for the service; the Prometheus *Targets* page
(http://localhost:19090/targets) shows the scrape error (connection refused, timeout, 401).

**Fix:**

- **The service is down:** follow its own runbook ([control plane](control-plane.md#controlplanedown),
  [serving](serving.md#rayservenomodelsloaded), [bridge](bus.md#bus-bridge-down)).
- **Connection refused but the service is up:** a port or network problem. With the segmented
  overlay, Prometheus reaches only services on the `ops` network.
- **401 or 403:** the metrics endpoint started requiring authentication.
- **An opt-in service you removed on purpose** (the `gateway`, `vllm` or `seanerbus` Compose
  profiles). These are found by DNS rather than listed, so a site that never runs them gets no
  alert. Once one has run, stopping it leaves it as a target reported down: Docker's resolver
  fails the lookup, and Prometheus keeps the last result it got. Restart Prometheus after
  decommissioning one, and the alert clears.

## RayServeTargetDown {#rayservetargetdown}

**Meaning:** Prometheus cannot scrape Ray Serve's metrics endpoint (port 8080 inside the
container), for 3 minutes.

**Impact:** **inference metrics are dark.** Every serving alert, including the error budget, is
blind. Inference itself may be fine.

**Check:** `curl -s localhost:18001/health` answers if serving is up. If it is, the metrics
exporter (`RAY_METRICS_EXPORT_PORT`) or the scrape configuration is the problem.

**Fix:** restart `ray-serving` if the exporter died. Correct `prometheus.yml` if the target moved.

## LokiDown {#lokidown}

**Meaning:** Prometheus has not reached Loki for 3 minutes.

**Impact:** logs are not being stored. Promtail buffers for a while, then drops. `exa stack logs`
still works, because it reads the container runtime directly.

**Check:** `exa stack status`; `exa stack logs --service loki --tail 100` (a full disk is the usual
cause).

**Fix:** free space or grow the volume, then restart Loki.

## TempoDown {#tempodown}

**Meaning:** Prometheus has not reached Tempo for 3 minutes.

**Impact:** traces are not stored. Services keep running; the OpenTelemetry exporters drop spans
they cannot send.

**Check and fix:** as for [LokiDown](#lokidown), with `--service tempo`.

## AlertmanagerDown {#alertmanagerdown}

**Meaning:** Prometheus has not reached Alertmanager for 3 minutes.

**Impact:** **no alert notifications are sent**, whatever fires. You may only be seeing this one
because the external dead-man's switch noticed the heartbeat stop (see [Watchdog](#watchdog)).

**Check:** `exa stack status`; `exa stack logs --service alertmanager --tail 100` (a configuration
error after an edit is the usual cause).

**Fix:** correct `alertmanager.yml` and restart. Then look at Prometheus's *Alerts* page for
anything that fired meanwhile.

## Watchdog {#watchdog}

**Meaning:** always firing, by design. It is the heartbeat of the alerting pipeline.

**Impact:** none while it fires. The alarm is when it **stops** reaching the external
dead-man's-switch receiver configured in Alertmanager: then Prometheus, Alertmanager, or the route
between them is down, and no other alert can reach you either.

**Check:** Prometheus and Alertmanager are both up (`exa stack status`), and the receiver's
configuration in `alertmanager.yml` is still there.

**Fix:** never silence or inhibit it. If the external receiver reports it missing, work through
[AlertmanagerDown](#alertmanagerdown) and [TargetDown](#targetdown) for Prometheus itself.
