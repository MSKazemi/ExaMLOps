# Runbooks: bus bridge

Alerts from the bus bridge: the service that receives inference and retrain requests from the
message bus, calls the inference pipeline, replies on the bus, and records drift telemetry.
It runs as its own Compose service; `exa stack status` lists it. Its Prometheus job is the one
these alerts select.

The bridge replies **before** it records telemetry: writing drift snapshots and embedding
statistics goes through a bounded background queue, so a slow datastore never delays or fails an
inference. That is why two of the alerts below are about telemetry, not inference.

## Bridge down {#bus-bridge-down}

**Meaning:** Prometheus has not reached the bridge for 2 minutes.

**Impact:** requests arriving on the bus get no answer. **Bus inference is down.**

**Check:** `exa stack status` (is the bridge container running?), its logs through
`exa stack logs`, and whether the bus itself is reachable from the bridge host.

**Fix:** restart the bridge. If it exits at start, the log names the cause (typically the bus
address, or the model schema registry it loads at startup).

## Bridge error rate {#bus-bridge-error-rate}

**Meaning:** more than 5 % of the bridge's inferences failed over 5 minutes, sustained for 5.

**Impact:** bus requesters get error replies.

!!! note "The figure was not a percentage until 2026-09-14"
    `seanerbus_inferences_total` counted only **successful** calls, so `errors ÷ inferences` was
    errors ÷ successes. It read 11 % at a true 10 % error rate, **900 % at 90 %**, and a total
    outage divided by zero — which is why the Grafana error-rate panels showed `+Inf` or nothing at
    the moment they mattered most. The alert still fired (its `clamp_min` denominator saved it from
    the division), so nothing was missed; only the number was wrong. The counter now records every
    dispatched call, which is what its name, its help text and the bridge's own internal stats
    always said it did.

**Check:** is it the bridge or serving behind it? If
[RayServeHighErrorRate](serving.md#rayservehigherrorrate) fires too, serving is the cause. If not,
the bridge's logs show validation errors (requests whose payload does not match the model's
schema) or failures reaching the inference pipeline.

**Fix:** for serving, follow the serving runbook. For schema mismatches, the requester and the
model's registered schema disagree: fix the sender, or register the schema the model now expects.

## Bridge latency {#bus-bridge-latency}

**Meaning:** the bridge's p99 inference latency is above 500 ms, sustained for 10 minutes.

**Impact:** bus requesters wait longer, and some may time out.

**Check:** compare with serving latency
([RayServeHighLatencyP99](serving.md#rayservehighlatencyp99)). If serving is fast, the time goes
in the bridge itself or in the network between the bridge and serving.

**Fix:** follow the serving runbook when serving is slow. Otherwise check the bridge's CPU and its
network path to serving.

## Telemetry writes failing {#bus-telemetry-writes-failing}

**Meaning:** the bridge failed to write drift or input-embedding telemetry at least once in 10
minutes, sustained for 5.

**Impact:** inference is unaffected, but **drift detection is losing samples**, so drift may be
reported late or not at all.

**Check:** the bridge's logs (the write error), and the platform datastore it writes to (the
`/state` database, or Postgres when `EXAMLOPS_DB_BACKEND=postgres`).

**Fix:** restore the datastore. Writes resume with the next inference; the lost samples are not
back-filled.

## Telemetry dropping {#bus-telemetry-dropping}

**Meaning:** the bridge's telemetry queue was full and records were dropped in the last 10 minutes,
sustained for 10.

**Impact:** as above: drift detection sees fewer samples than traffic.

**Check:** the datastore's write latency. The queue fills when the datastore is slower than bus
traffic.

**Fix:** make the writes faster first (a datastore under load, or SQLite on a slow volume, where
Postgres is the answer). Raise the bridge's telemetry queue bound (see the
[environment variable reference](../reference/env-vars.md)) only after that. A bigger queue in
front of a slow store only delays the drops and costs memory.
