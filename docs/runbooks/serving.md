# Runbooks: model serving

Alerts from the `examlops-serving` group (Ray Serve's prediction path, the serving snapshot that
configures it, and the prediction error budget) and the `examlops-gateway` group (the
[serving gateway](../guides/serving-gateway.md) in front of it). Background:
[Ray Serve](../components/ray-serve.md) · [Serving snapshot](../guides/serving-snapshot.md) ·
[Overload, deadlines and load shedding](../components/ray-serve.md#overload-deadlines-and-load-shedding).

The prediction counter `ray_examlops_predict_requests_total` carries a `status` label:
`success`; `error` (the model raised, 500); `timeout` (ran past `RAY_PREDICT_TIMEOUT`, 504);
`deadline_exceeded` (the caller's time budget ran out, 504; possibly while queued); `invalid` (422);
`not_found` (404). The first question for any serving alert is *which status is growing*:

```promql
sum by (status, model_name) (rate(ray_examlops_predict_requests_total[5m]))
```

**What every error rate on this page divides by.**

Every serving error rate and SLO on this page is **bad events ÷ valid events**, and both halves name
their statuses:

| Status | Counts as | Why |
|---|---|---|
| `success` | valid | served |
| `error`, `timeout`, `deadline_exceeded` | **bad** and valid | the service failed to answer |
| `invalid`, `not_found` | **neither** | the caller asked for something the service correctly refused |

!!! warning "Until 2026-09-14 the denominator was the whole counter"
    `invalid` and `not_found` sat in the denominator of four alerts — including both burn-rate
    alerts — and six Grafana panels. Because they inflate the denominator without ever entering the
    numerator, **client traffic moved the SLO**:

    | | requests | server errors | measured error rate |
    |---|---|---|---|
    | real traffic | 100 | 5 | **5.0%** |
    | the same, plus 900 malformed requests | 1000 | 5 | **0.5%** |

    So a burst of junk *improved* compliance and could hold a burn-rate alert below its threshold
    during a real outage — the alerts that exist to page you, silenced by unrelated traffic. The
    denominator is now `success|error|timeout|deadline_exceeded`.

    Client faults are not hidden, they are separated: the *Request Rate by Model (served vs failed
    vs refused)* panel shows them as their own series, so a spike of malformed requests is visible
    as what it is — a caller problem — instead of quietly flattering the SLO.

`tests/unit/test_sli_counts_only_valid_events.py` holds all of it: no error rate divides by the
unfiltered counter, no client fault is counted as a bad event, and a serving status that belongs to
none of the three buckets fails the build until somebody classifies it.

## RayServeHighErrorRate {#rayservehigherrorrate}

**Meaning:** more than 5 % of predictions failed (`error`, `timeout` or `deadline_exceeded`) over
5 minutes, sustained for 10.

**Impact:** callers get 5xx answers; the error budget is burning (see
[SLOErrorBudgetFastBurn](#sloerrorbudgetfastburn)).

**Check:**

1. Which status and which model (the query above, or Grafana *Error Rate by Model (%)*).
2. Did it start with a promotion? `exa audit --last 1d` shows alias moves; `exa serve models`
   shows the version each alias serves now.
3. `exa serve check` smoke-tests every loaded model; `exa stack logs --service ray-serving
   --tail 300` shows the exceptions (`Prediction error for …`).

**Fix:**

- **`error` on one model after a promotion.** The new version is broken. Move the alias back,
  or shift its traffic away while you investigate: `exa serve traffic <model> --production 100`.
- **`timeout`.** The model is slower than `RAY_PREDICT_TIMEOUT` (30 s), or hung predict threads
  have used up the pool. `Prediction TIMEOUT` log lines name the model; a hung pool recycles
  itself after `RAY_PREDICT_WORKERS` hung calls.
- **`deadline_exceeded`.** Callers' time budgets run out, usually while requests queue: the
  replicas are overloaded. See [RayServeHighLatencyP99](#rayservehighlatencyp99).

## RayServeHighErrorRateCritical {#rayservehigherrorratecritical}

**Meaning:** more than 20 % of predictions failed over 5 minutes, sustained for 5.

**Impact:** a fifth of inference traffic fails. At this rate the monthly error budget is gone
in about a day.

**Check:** as for [RayServeHighErrorRate](#rayservehigherrorrate), but first rule out a
platform-wide cause: `exa stack status` (is MLflow or the object store down?) and
[RayServeNoModelsLoaded](#rayservenomodelsloaded).

**Fix:** roll back the last change first (alias move, traffic split, image), then diagnose. If
one model is responsible, `exa serve traffic <model> --production 100` isolates a canary.

## RayServeHighLatencyP99 {#rayservehighlatencyp99}

**Meaning:** the p99 prediction latency is above 1 s over 5 minutes, sustained for 10.

**Impact:** slow answers. Callers with a short `X-ExaMLOps-Budget-Ms` start receiving 504
`deadline_exceeded`.

**Check:**

1. Queueing or compute? The Ray dashboard (http://localhost:18265, loopback only) shows replica
   CPU and queue length. Grafana *p95 Latency by Model* shows whether one model or all are slow.
2. Cold loads: a request for an alias outside `RAY_PRELOAD_ALIASES`, or for a raw version
   (`"version": "…"`) not in the version cache, downloads the model on the request path. Look for
   slow requests that name such an alias or version.
3. A new model version that is simply slower (compare with the previous version's latency).

**Fix:**

- **Saturated replicas.** Add replicas (`RAY_NUM_REPLICAS`) or cores. Bound the queue
  (`RAY_MAX_QUEUED_REQUESTS`), so overload is answered with an immediate 503 instead of growing
  every caller's wait.
- **Cold loads.** Add the alias to `RAY_PRELOAD_ALIASES`, raise `RAY_VERSION_CACHE_SIZE`, or turn
  on the artifact cache (`RAY_ARTIFACT_CACHE`).
- **A slower model.** Roll back, or accept it and adjust the SLO.

## RayServeHighLatencyP99Critical {#rayservehighlatencyp99critical}

**Meaning:** the p99 prediction latency is above 3 s, sustained for 5 minutes.

**Impact:** most callers with a normal budget are close to timing out; the bus bridge's
requesters are waiting.

**Check and fix:** as for [RayServeHighLatencyP99](#rayservehighlatencyp99). At this level, shed
load deliberately (a bounded queue) rather than let every request wait.

## RayServeNoModelsLoaded {#rayservenomodelsloaded}

**Meaning:** no Ray Serve replica reports a loaded model for 5 minutes, or the metric is absent.

**Impact:** every prediction fails with 404 or 503. **Inference is down.**

**Check:**

1. `curl -s localhost:18001/health`: `models_loaded`, and `snapshot` (the generation and where
   it came from: `kv`, `db`, `cache`, or none). If it reports models loaded, the models are
   fine and the metric is missing: see [RayServeMetricsMissing](#rayservemetricsmissing),
   which fires alongside.
2. `exa stack logs --service ray-serving --tail 300`: `refusing to load … signature
   verification failed` (verify-before-load in `enforce` mode), `MLflow unreachable`, or a
   framework import error.
3. `exa serve snapshot show`: does the snapshot list any model with an alias this replica
   preloads (`RAY_PRELOAD_ALIASES`)?

**Fix:**

- **MLflow or the object store is unreachable, and there is no snapshot or last-known-good
  copy.** Restore them, then `exa serve reload`. For the next time: keep the snapshot's
  last-known-good file and the artifact cache on a volume. Both are set in Compose.
- **Every version refused by verification.** Sign them (`exa models sign <model> <version>`),
  or return to `EXAMLOPS_SERVING_VERIFY=warn` while you do. See
  [Rolling out enforcement](../guides/supply-chain-security.md#rolling-out-enforcement-on-ray-serve).
- **No Production alias exists.** Promote a version (`exa pipeline promote`), or preload the
  aliases you do have.

## InferenceRetryBudgetSpent {#inferenceretrybudgetspent}

**Meaning:** the inference router wanted to retry a failed model-server call and its retry budget
refused, at least once in 10 minutes. The budget retries a burst of about 50 failures (a replica
dying with its requests), then only about one call in ten (`INFERENCE_RETRY_MAX_TOKENS`, 100;
`INFERENCE_RETRY_TOKEN_RATIO`, 0.1), so that an outage is not multiplied by retries of itself. The
refused requests' `cause` (`transport`, `replica_lost`) says what failed.

**Impact:** requests that could have succeeded on a second attempt fail with their first error
(`overloaded` or `inference_failed`). The underlying failure is the real problem.

**Check:** why the calls fail, by reason and by outcome:

```promql
sum by (reason) (increase(ray_examlops_router_retries_total[10m]))
sum by (outcome) (rate(ray_examlops_router_requests_total[5m]))
```

- Mostly `overloaded`: the model server is shedding load. See
  [Overload, deadlines and load shedding](../components/ray-serve.md#overload-deadlines-and-load-shedding):
  add replicas or raise `RAY_MAX_QUEUED_REQUESTS`.
- Mostly `transport`: the model server is unreachable or restarting;
  [RayServeTargetDown](platform.md#rayservetargetdown) and `exa stack status`.
- `replica_lost`: see [InferenceReplicasLost](#inferencereplicaslost).

**Fix:** fix the cause. Raising the budget only hides it and adds load to a failing server.

## InferenceReplicasLost {#inferencereplicaslost}

**Meaning:** a model-server replica died while requests were running on it, in the last 15
minutes. Ray's proxy answered those requests with a plain-text `500`, and the router retried each
of them once.

**Impact:** usually none: the retries went to a healthy replica. Callers of `/v2` directly, not
through the pipeline, saw the `500`.

**Check:** why the replica died. `exa stack logs --service ray-serving --tail 500`, and
`docker inspect examlops-ray-serving --format '{{.State.OOMKilled}}'` for the container.

- **Out of memory.** Each replica holds the whole hot set. Lower `RAY_PRELOAD_ALIASES`, or give
  the container more memory.
- **The same request each time.** A request that crashes the replica is retried once and then
  answered with an error, so it cannot take every replica down, but it will keep killing one. Find
  it in the router's logs and fix the model or the input validation.
- **Once, during a deploy or restart.** Expected. It clears within 15 minutes.

## RayServeMetricsMissing {#rayservemetricsmissing}

**Meaning:** Prometheus scrapes Ray Serve's metrics port successfully, but the platform's serving
series (`ray_examlops_models_loaded` and the rest) have been absent for 10 minutes.

**Impact:** **every serving alert and SLO panel is blind.** Inference itself may be fine.
[RayServeNoModelsLoaded](#rayservenomodelsloaded) fires too, because its metric is absent.

**Check:**

1. What the port serves: `docker exec examlops-ray-serving python -c "import urllib.request;
   print(urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode())" | grep -c
   '^ray_'`. Zero `ray_` lines, with only `process_*` and `python_*`, is this failure.
2. `docker exec examlops-ray-serving env | grep OTEL_SDK_DISABLED`, and whether the running image
   includes `_prepare_ray_environment` (in `serving/ray_serving/app.py`).

**Fix:**

- **`OTEL_SDK_DISABLED=true` reached Ray.** Ray 2.55 records its metrics through the
  OpenTelemetry SDK, and that variable (the platform's switch for turning tracing off) disables
  the SDK. The model server removes it before starting Ray, so rebuild and restart `ray-serving`
  from a current image. Tracing stays off; an unset variable means off to the platform.
- **The metric appears for a few seconds after a restart and then disappears.** The image
  predates gauge republishing (`RAY_GAUGE_REFRESH_SECONDS`): Ray exports a gauge only once per
  set. Rebuild from a current image; check `RAY_GAUGE_REFRESH_SECONDS` is not `0`.
- **The metrics agent died.** Restart `ray-serving`.

## ServingSnapshotLagging {#servingsnapshotlagging}

**Meaning:** at least one replica is serving an older snapshot generation than the control plane
has published, for 5 minutes.

**Impact:** replicas disagree. Some serve the new alias target or traffic split, others the old
one.

**`0` means the replica has applied *nothing*,** and is serving from its fallback registry — the
most-behind state there is, not a small lag. The value reads as the full published generation
because the difference is taken from zero. Before 2026-09-15 that replica published no
applied-generation series at all, and `min()` over no series is an empty vector, so this alert
could not fire for it.

**Check:**

1. `exa serve snapshot show` for the published generation; each replica's `/health` for the one
   it applied (`snapshot.generation`), where it read it (`snapshot.source`: `kv`, `db`, `cache`),
   and whether its poll loop runs (`poller_alive`).
2. A model that fails to load does **not** hold a replica back. The replica applies the new
   generation, keeps the failed model on its last-known-good version, and logs
   `Failed to load '<model>'@<alias>`. Lag means the replica is not reading new generations.

**Fix:**

- **`source` is `cache`, or `poller_alive` is false.** The replica reaches neither the NATS KV
  bucket nor the datastore. Check `EXAMLOPS_NATS_URL` and its datastore settings
  (`PLATFORM_DB` or the Postgres DSN), then restart the replica. Meanwhile it serves its
  last-known-good generation.
- **The replica is stuck in a long load.** A large model downloading blocks the next apply until
  it finishes. It clears on its own; if not, restart the replica.

## ServingSnapshotCompileFailing {#servingsnapshotcompilefailing}

**Meaning:** the control plane's snapshot projector failed to compile for 15 minutes.

**Impact:** replicas keep serving the last generation, so **no promotion, traffic split or shadow
change reaches serving** until it recovers. Nothing is lost.

**Check:** the control plane's `/health` field `runtime.serving_snapshot.error` states the error;
it is almost always MLflow (`MLFLOW_TRACKING_URI`, credentials when MLflow authentication is on).

**Fix:** restore MLflow access. The next tick compiles and publishes; `exa serve snapshot publish`
forces one immediately.

## RayServeReloadFailures {#rayservereloadfailures}

**Meaning:** a reload of one model's aliases failed at least once in 15 minutes.

**Impact:** that model keeps serving its last-known-good version; the change you expected did
not apply.

**Check:** `exa stack logs --service ray-serving --tail 300`, the lines `Reload of '<model>'@<alias>
failed: …`.

**Fix:** by cause:

- **Artifact store unreachable.** Restore it, then `exa serve reload --model <model>`.
- **Verification refused the version.** Sign it.
- **Missing framework in the serving image.** Add it to the image
  (see [framework dependencies](../components/ray-serve.md#framework-dependencies-in-the-serving-image)).

## SLOErrorBudgetFastBurn {#sloerrorbudgetfastburn}

**Meaning:** over the last hour, predictions failed at more than 14.4 times the rate the 30-day
99.5 % objective allows (a 7.2 % error rate), sustained for 5 minutes.

**Impact:** at this rate the whole monthly error budget is gone in about two days.

**Reading the value:** the alert reports the **burn rate** — the observed error rate divided by the
rate the objective allows — so `16×` means sixteen times the sustainable rate, matching the summary's
`> 14.4×` and the `burn_rate` that `exa slo status` prints. Until 2026-09-14 the expression yielded
the *error ratio* instead, and `humanize` renders small numbers with SI prefixes: a 16× burn printed
as **`80m×`**, against a summary reading `> 14.4×`.

**Check and fix:** it is an error-rate problem. Follow
[RayServeHighErrorRate](#rayservehigherrorrate). Freeze promotions until it clears; Grafana
*Error Budget Remaining* shows what is left.

## SLOErrorBudgetSlowBurn {#sloerrorbudgetslowburn}

**Meaning:** over the last 6 hours, predictions failed at more than 3 times the allowed rate (a
1.5 % error rate), sustained for an hour.

**Impact:** not urgent, but at this rate the budget runs out before the month does.

**Reading the value:** as above, the value is the burn rate — `5×` is five times the sustainable
error rate, not a 5 % error rate.

**Check and fix:** usually one model or one input population. Break the error rate down by model
and status (the query at the top of this page) and fix or roll back that model during working
hours.

## ServingGatewayDown {#servinggatewaydown}

**Meaning:** Prometheus has not reached the gateway's Envoy (`job="gateway"`) or its
authorization service (`job="gateway_authz"`) for 2 minutes. These jobs are found by DNS, so the
alert only exists where the gateway has run since Prometheus started.

**Impact:**

- Envoy down: clients calling inference through the gateway get connection errors.
- `gateway-authz` down: Envoy is up but every request is refused with `503`. The gateway fails
  closed; nothing is served without a decision.

Callers of Ray Serve's internal ports are unaffected.

**Check:** `docker compose --profile gateway ps gateway gateway-authz`;
`exa stack logs --service gateway-authz --tail 200`, or the same for `gateway`. For Envoy, a bad
configuration stops it at start, and the log names the line.

**Fix:** restart the stopped service (`docker compose --profile gateway up -d gateway
gateway-authz`). If you removed the gateway on purpose, the target stays reported down until
Prometheus restarts: see [TargetDown](platform.md#targetdown).

## ServingGatewayAuthorizationFailing {#servinggatewayauthorizationfailing}

**Meaning:** Envoy asked `gateway-authz` for a decision and got no usable answer (connection
refused, timeout, or an error response), for 5 minutes. Envoy counts these in
`envoy_http_ext_authz_error`.

**Impact:** each such request was refused with `503`. With every request failing, inference
through the gateway is down.

**Check:**

1. Is `gateway-authz` running and healthy? `curl -s http://gateway-authz:8090/healthz` from a
   container on the same network, or [ServingGatewayDown](#servinggatewaydown). `/healthz` only
   says the process answers — ask `/readyz` whether it has ever reached the credential store. A
   replica stuck on `{"status": "starting"}` has never read it, which points at its configuration
   rather than at a store outage.
2. Envoy's view of it: `envoy_cluster_upstream_rq_timeout{envoy_cluster_name="gateway_authz"}`
   and `envoy_cluster_upstream_cx_connect_fail{envoy_cluster_name="gateway_authz"}`.

**Fix:** restart `gateway-authz`. If it answers but slowly, it is waiting on the platform store:
see [ServingGatewayCredentialStoreUnavailable](#servinggatewaycredentialstoreunavailable).

## ServingGatewayCredentialStoreUnavailable {#servinggatewaycredentialstoreunavailable}

**Meaning:** `gateway-authz` refused requests with `503` for 5 minutes. It answers `503` when it
cannot read the platform store: a virtual key it has not verified in the last
`EXAMLOPS_GATEWAY_KEY_CACHE_SECONDS`, or, under multi-tenancy, a model's project.

**Impact:** keys verified in the last minute keep working. New keys, keys not used recently, and
scoped models the gateway has not seen are refused.

**Check:** `exa stack logs --service gateway-authz --tail 200`; the platform store itself
(Postgres: [the control plane runbook](control-plane.md#controlplanedown); SQLite: the `/state`
volume is mounted into `gateway-authz`).

**Fix:** restore access to the store. Nothing needs restarting; the next request reads it again.

## ServingGatewayHighErrorRate {#servinggatewayhigherrorrate}

**Meaning:** more than 5 % of the gateway's answers were `5xx` over 10 minutes.

**Impact:** callers see failures. They come from the gateway itself or from Ray Serve behind it.

**Check:** where the `5xx` come from:

```promql
sum by (envoy_response_code_class) (rate(envoy_cluster_upstream_rq_xx{envoy_cluster_name="ray_serving"}[5m]))
```

- `5xx` from the `ray_serving` cluster: the model server is failing. Follow
  [RayServeHighErrorRate](#rayservehigherrorrate).
- No upstream `5xx`, but downstream ones: the gateway answered them itself. Usually `503` from a
  failed authorization ([ServingGatewayAuthorizationFailing](#servinggatewayauthorizationfailing)),
  or `504` when Ray Serve took longer than the route timeout.
- With the workload-identity overlay, `503` with `upstream connect error` in the gateway's log and
  no upstream answers: the mutual-TLS hop to the model server fails. Check that `serving-mtls` is
  running (`docker compose ps serving-mtls`) and that the SPIRE agent is healthy. The gateway and
  `serving-mtls` get their certificates from it
  ([Workload identity](../guides/workload-identity.md#every-hop-to-the-model-server-mutual-tls)).

**Fix:** by source, as above.

## ServingEndpointsUnhealthy {#servingendpointsunhealthy}

**Meaning:** the gateway is serving inference from fewer model-server endpoints than exist. One or
more failed the `/ready` health check twice (about ten seconds) and are getting no traffic.

**Impact:** capacity is lower than the deployment suggests, and quietly so — the remaining endpoints
answer everything, until they cannot. This is the *working* case of a failure that used to be
invisible: an endpoint that stops answering keeps its share of the traffic until something notices
([when a model server stops answering](../guides/serving-gateway.md#when-a-model-server-stops-answering)).

**Check:**

```bash
curl -s localhost:19902/stats/prometheus | grep envoy_cluster_membership   # healthy vs total
kubectl -n <ns> get pods -l app=ray-serving -o wide                        # or: docker compose ps
```

Then ask the ejected pod itself: `curl http://<pod>:8001/ready`. A pod that answers `/ready` but is
ejected points at the network between the gateway and that pod; one that does not answer is the
model server's own problem — look for a model still loading, an OOM kill, or a wedged process
([the model server](../components/ray-serve.md)).

**Fix:** by cause.

- **A pod is starting.** Loading the hot set takes tens of seconds; the alert clears by itself. If
  it does not, the readiness probe is missing and Kubernetes is calling it ready too early
  ([the Deployment](../guides/serving-on-kubernetes.md#the-deployment)).
- **A node was lost.** The pod keeps its place in the Kubernetes Service for about two minutes; the
  gateway ejecting it in ten seconds is what protects callers in the meantime
  ([when a whole node goes away](../guides/serving-on-kubernetes.md#when-a-whole-node-goes-away)).
- **The pod is wedged** — alive, accepting connections, answering nothing. Restart it; the gateway
  puts it back two successful probes later, with no action from you.
- **It is not coming back.** Scale the deployment so the remaining capacity is not one node's worth:
  the gateway is protecting callers from a sick pod, not replacing the one you are missing.

## ServingNoHealthyEndpoints {#servingnohealthyendpoints}

**Meaning:** every model-server endpoint failed the gateway's health check, on either serving
cluster — `ray_serving` (REST) or `ray_serving_grpc`. `{{ $labels.envoy_cluster_name }}` in the
alert says which.

!!! note "Why both clusters have to be in this selector"
    [ServingEndpointsUnhealthy](#servingendpointsunhealthy) covers both clusters but deliberately
    **excludes** the total outage (`… and healthy > 0`), so that the warning and this critical do
    not both fire for the same event. Anything this alert's selector omits therefore has **no
    critical alert at all**. `ray_serving_grpc` was omitted until 2026-09-15, which left a complete
    gRPC serving outage silent on both: the warning's `healthy > 0` was false, and this one did not
    select the cluster.

**Impact:** inference is down or nearly so. Note what the gateway does *not* do: with no healthy
endpoint it keeps sending requests anyway (Envoy's panic threshold), so callers get whatever the
model server says rather than a gateway `503`. That is deliberate — a single-replica deployment
whose model is still loading must not be taken out of service by its own proxy — but it means
**this alert, not the error rate, is the signal**.

**Check:** the same commands as above. If `envoy_cluster_membership_total` is 0 as well, the gateway
has no endpoints at all: its upstream name does not resolve
([`gateway.upstream.host`](../guides/serving-gateway.md)), which is a configuration problem rather
than an outage of the model server.

**Fix:** treat it as the model server being down — start with
[RayServeNoModelsLoaded](#rayservenomodelsloaded) and the model server's own logs.
If the pods are healthy and answering `/ready` by hand, the gateway cannot reach them: check the
network policy, the Service name, and that the pods are in the Service's endpoints.

## ServingGatewayAtCeiling {#servinggatewayatceiling}

**Meaning:** the gateway's own request ceiling (the `local_ratelimit` filter, 2000 requests per
second per gateway replica) refused requests with `429` for 10 minutes. This is not a tenant's
quota (`EXAMLOPS_GATEWAY_TENANT_RPM`); that is enforced by `gateway-authz` and is expected to refuse.

**Impact:** callers are refused regardless of their tenant's quota.

**Check:** is the traffic legitimate? Break it down by tenant in the model server's metrics, or
look for a single client looping.

**Fix:** for legitimate growth, run more gateway replicas or raise `max_tokens` and
`tokens_per_fill` in `envoy.yaml` (`serving_ceiling`), after checking that Ray Serve can take the
extra load. For one runaway client, revoke or budget its key (`exa gateway key revoke <hash>`).

