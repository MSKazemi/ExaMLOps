# Ray Serve — Multi-Model Inference

Ray Serve hosts every aliased model from the MLflow registry under a single deployment. A `POST /predict/{model_name}` call routes to the right model version, with optional per-request alias or version selection (Phase 3).

## Start / stop

```bash
# Start Ray Serve as part of the full Docker stack
make stack-up

# Or start/rebuild only the Ray Serve compose service with exa
exa stack up --service ray-serving

# Check it is running
exa serve check
curl http://localhost:18001/health
```

## API endpoints

All endpoints are served on port **18001** by default.

### `GET /health`

Liveness + readiness check. Reports per-model status.

```bash
curl http://localhost:18001/health
```

```json
{
  "status": "ok",
  "models_loaded": 3,
  "models": {
    "JPCP": {"version": "3", "run_id": "abc123", "alias": "Production", "status": "ok"},
    "MACK": {"version": "7", "run_id": "def456", "alias": "Production", "status": "ok"}
  }
}
```

`status` is `"ok"` if at least one model is loaded, `"degraded"` if none are.

### `GET /models`

List all loaded models (hot set).

```bash
curl http://localhost:18001/models
```

```json
[
  {"model_name": "JPCP", "model_version": "3", "run_id": "abc123", "alias": "Production", "status": "ok"},
  {"model_name": "JPCP", "model_version": "2", "run_id": "bcd234", "alias": "Canary",     "status": "ok"}
]
```

### `POST /predict/{model_name}`

Run inference. The `features` dict must contain the column names the model was trained on.

**Basic request** (uses default alias, i.e. `MODEL_STAGE=Production`):

```bash
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2, "feature_1": 0.8, "feature_2": 3.4}}'
```

**With alias selection** (Phase 3):

```bash
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2, "feature_1": 0.8}, "alias": "Canary"}'
```

**With raw version selection** (Phase 3):

```bash
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2, "feature_1": 0.8}, "version": "5"}'
```

Resolution order: `alias` → `version` → `MODEL_STAGE` default.

**Response:**

```json
{
  "model_name": "JPCP",
  "model_version": "3",
  "run_id": "abc123",
  "alias": "Production",
  "prediction": 142.7
}
```

**Errors:**

| Code | Reason |
|---|---|
| 404 | Model not loaded (alias not found, version not in cache, or name typo) |
| 422 | A feature is not numeric |
| 500 | Feature mismatch or model exception |
| 503 | Load shed: the deployment's queue is full (`RAY_MAX_QUEUED_REQUESTS`), or no replica is ready |
| 504 | The model ran past `RAY_PREDICT_TIMEOUT`, or past the caller's `X-ExaMLOps-Budget-Ms` — or that budget ran out while the request was queued, in which case the model never ran |

An optional `X-ExaMLOps-Budget-Ms` header says how many milliseconds the caller will still wait;
see [Overload, deadlines and load shedding](#overload-deadlines-and-load-shedding).

### Open Inference Protocol v2

The same models are also served over [Open Inference Protocol v2](https://kserve.github.io/website/docs/concepts/architecture/data-plane/v2-protocol)
(OIP v2, the "V2" or KServe protocol), which KServe, Triton and MLServer clients and load tools
speak unchanged. Both protocols share one execution path: the same timeout and caller budget, the
same request metrics, and the same shadow mirroring.

| Route | Answers |
|---|---|
| `GET /v2` | Server metadata |
| `GET /v2/health/live` · `/v2/health/ready` | `200` (true) or `400` (false), empty body |
| `GET /v2/models/{name}[/versions/{v}]` | Model metadata; inputs come from the model's MLflow signature |
| `GET /v2/models/{name}[/versions/{v}]/ready` | Whether that model or version is loaded (never triggers a download) |
| `POST /v2/models/{name}[/versions/{v}]/infer` | Inference |

A tabular model takes either **one tensor of rows**, shape `[N, F]`:

```bash
curl -s -X POST http://localhost:18001/v2/models/jpcp/infer -H "Content-Type: application/json" -d '{
  "id": "req-1",
  "inputs": [{"name": "input-0", "shape": [2, 3], "datatype": "FP64",
              "data": [[1.2, 0.8, 3.4], [0.1, 0.2, 0.3]]}]
}'
```

or **one tensor per feature column**, named as in the model's signature (`GET /v2/models/jpcp`
lists them). Columns are matched by name, in any order:

```json
{"inputs": [
  {"name": "cpu",   "shape": [2], "datatype": "FP64", "data": [1.2, 0.1]},
  {"name": "mem",   "shape": [2], "datatype": "FP64", "data": [0.8, 0.2]},
  {"name": "nodes", "shape": [2], "datatype": "INT64", "data": [4, 1]}
]}
```

The answer is one output tensor, one value per row:

```json
{"model_name": "jpcp", "model_version": "3", "id": "req-1",
 "outputs": [{"name": "predict", "datatype": "FP64", "shape": [2], "data": [142.7, 12.3]}]}
```

- **Version and alias.** `/v2/models/jpcp/versions/5/infer` serves version 5. Without a version,
  the default alias serves; `"parameters": {"alias": "Canary"}` picks another. A version in the
  path wins over an alias.
- **Validation before the model runs.** A request whose width, column names, shapes or datatypes do
  not fit the model's signature is refused with `400` and `{"error": "…"}` naming what is wrong.
  `BYTES` inputs are refused; this server takes numeric features.
- **Errors** are `{"error": "…"}`: `400` invalid request, `404` unknown model or version, `503`
  MLflow unreachable while loading, `504` timeout or spent `X-ExaMLOps-Budget-Ms`, `500` the model
  raised.

**`/predict/{model_name}` is deprecated.** It keeps working, and every answer carries
`Deprecation: @1789084800` and `Link: </v2/models/{name}/infer>; rel="successor-version"`. No
removal date is set yet. The platform's own callers all use OIP v2 through `examlops.oip_client`:
the inference router, the bus bridge, the Skipper agent's `predict` tool, the dashboard's test
inference, `exa serve batch submit` and the example client. `tests/unit/test_serving_callers_use_oip.py`
keeps it that way. For a model with a column signature, `/predict` takes the features by name as
well; it used to fail on such models. Conformance suite: `tests/unit/test_oip_v2.py`.

### Open Inference Protocol v2 over gRPC

The same protocol is also served over gRPC, on port `8081` (host `18081`), as the service
`inference.GRPCInferenceService`. KServe, Triton and MLServer gRPC clients work unchanged: the
service is defined by the protocol's own `.proto`, shipped verbatim as
`serving/oip_grpc/open_inference_grpc.proto`.

| RPC | Answers |
|---|---|
| `ServerLive`, `ServerReady` | `live` / `ready` |
| `ServerMetadata` | name, version, extensions |
| `ModelReady` | whether a model (or `version`) is loaded, as `GET …/ready` |
| `ModelMetadata` | name, versions, platform, and inputs from the MLflow signature |
| `ModelInfer` | inference, with `model_version` and `parameters.alias` as over REST |

```bash
grpcurl -plaintext -import-path serving/oip_grpc -proto open_inference_grpc.proto \
  -d '{"model_name": "jpcp", "inputs": [{"name": "input-0", "datatype": "FP64", "shape": [2, 3],
       "contents": {"fp64_contents": [1.2, 0.8, 3.4, 0.1, 0.2, 0.3]}}]}' \
  localhost:18081 inference.GRPCInferenceService/ModelInfer
```

From Python, with the stubs in `serving.oip_grpc`:

```python
import grpc
from serving.oip_grpc import open_inference_grpc_pb2 as pb, open_inference_grpc_pb2_grpc as pbg

stub = pbg.GRPCInferenceServiceStub(grpc.insecure_channel("localhost:18081"))
request = pb.ModelInferRequest(model_name="jpcp", inputs=[pb.ModelInferRequest.InferInputTensor(
    name="input-0", datatype="FP64", shape=[1, 3],
    contents=pb.InferTensorContents(fp64_contents=[1.2, 0.8, 3.4]))])
print(stub.ModelInfer(request, timeout=5).outputs[0].contents.fp64_contents)
```

- **One implementation of the protocol.** Each RPC is answered by the REST implementation above,
  called over loopback inside the model server's container. Validation, errors, version and alias
  selection, the time budget, metrics and shadow mirroring are therefore exactly REST's. On one
  replica, 500 requests at 20-way concurrency ran at the same rate over gRPC and REST (about 380
  requests per second, median 50 ms): the model is the bottleneck, not the extra hop.
- **Tensors.** Inputs may be typed (`contents`, the field for each datatype) or `raw_input_contents`:
  little-endian bytes, one entry per input, with each `BYTES` element prefixed by its 4-byte length.
  `FP16` is accepted raw only, as the protocol requires. Outputs are typed.
- **Deadlines.** A gRPC deadline becomes the request's `X-ExaMLOps-Budget-Ms`, so the server refuses
  work it cannot finish in time, as over REST.
- **Status codes.** `INVALID_ARGUMENT` (a request that does not fit the signature, or a malformed
  tensor), `NOT_FOUND` (unknown model or version), `RESOURCE_EXHAUSTED` (load shed, or a message over
  `RAY_SERVE_GRPC_MAX_MESSAGE_MB`), `UNAVAILABLE` (MLflow unreachable while loading),
  `DEADLINE_EXCEEDED` (timeout or spent budget), `INTERNAL` (the model raised). The detail is the
  REST error message.
- **Names are checked.** A `model_name` or `model_version` that is not a plain name (`../reload`,
  anything with a `/`) is refused with `INVALID_ARGUMENT` before anything runs.
- **It never takes REST down.** If the gRPC server cannot start (the port is taken, say), the model
  server logs the error and keeps serving REST. `RAY_SERVE_GRPC_PORT=0` turns it off.
- **Through the gateway.** The [serving gateway](../guides/serving-gateway.md#what-it-serves) routes
  gRPC on its one port (`:18088`) with the same credential, quota and limits as REST. Under the
  [workload-identity overlay](../guides/workload-identity.md) the gRPC port binds loopback like REST
  and the gateway reaches it over the same mutual TLS hop.
- **Tests.**
  - `tests/unit/test_oip_grpc.py` holds the stubs to the committed proto and the field numbers
    clients depend on. It covers every conversion, the status and deadline mappings, and the name
    check, then runs a real gRPC client end to end against the REST implementation with a real
    MLflow model.
  - A live run of an image built from the tree served `grpcurl` the same predictions as REST, digit
    for digit, from a model registered in a real MLflow.

### `POST /reload`

Hot-reload: re-scan MLflow and refresh the entire hot set (all aliases in `RAY_PRELOAD_ALIASES`).

**Admin route.** `/reload`, `/reload/{model_name}` and `/infer-pipeline/traffic-rules/{model}` need
`Authorization: Bearer $RAY_SERVE_ADMIN_TOKEN`. With the variable unset they answer 503: inference is
unaffected, and alias moves still reach serving within `RAY_RELOAD_POLL_SECONDS` through polling.
`exa serve reload`, `exa serve traffic`, Skipper and the pipeline's promotion webhook send it from
the same variable (CLI config key `ray_serve_admin_token`).

```bash
curl -X POST http://localhost:18001/reload -H "Authorization: Bearer $RAY_SERVE_ADMIN_TOKEN"
```

```json
{"reloaded": ["JPCP", "MACK"], "count": 2}
```

No restart needed. Existing replicas continue serving during the reload.

### `POST /reload/{model_name}`

Hot-reload a single model only. Called automatically by Prefect's `promote_task` after a new Production alias is set.

```bash
curl -X POST http://localhost:18001/reload/JPCP -H "Authorization: Bearer $RAY_SERVE_ADMIN_TOKEN"
```

### `GET /docs` / `GET /redoc`

FastAPI Swagger UI and ReDoc — interactive API documentation generated automatically.

## How models are loaded

### From the serving snapshot (default)

When the control plane has published a [serving snapshot](../guides/serving-snapshot.md), a
replica builds its hot set from it: for every model and every alias in the model's serve aliases,
it loads the version the snapshot names, **by version** (`models:/jpcp/7`), and reads the
`framework` tag the snapshot carries. A new generation is picked up within
`RAY_SNAPSHOT_POLL_SECONDS` (2 s). Only models whose version moved are reloaded, aliases the
snapshot dropped are unloaded, and a failed load keeps the version already served. With
`RAY_ARTIFACT_CACHE` set, the version's files come from a local content-addressed cache that is
fetched once and digest-checked on every use, so a restart does not need MLflow (see the
[serving snapshot guide](../guides/serving-snapshot.md#serving-through-an-outage-the-artifact-cache)). `GET /health`
reports `snapshot.generation` and `snapshot.source` (`kv`, `db` or `cache`). With a snapshot in
force, the replica does not poll MLflow.

### Hot set from MLflow (no snapshot, and on `/reload`)

While no snapshot has been published, and on every `POST /reload` or `POST /reload/{name}`, the server loads every `(model, alias)` pair where the alias is in `RAY_PRELOAD_ALIASES` (default: `Production,Canary,Staging`):

1. Calls `mlflow.MlflowClient().search_registered_models()`
2. For each model and each alias in `RAY_PRELOAD_ALIASES`, attempts `client.get_model_version_by_alias(name, alias)`
3. Reads the `framework` tag on the model version and dispatches to the right MLflow loader (`mlflow.sklearn` / `mlflow.pytorch` / `mlflow.transformers`)
4. Stores the loaded model in the in-memory hot set

Models where an alias is not set are silently skipped for that alias.

### Framework dependencies in the serving image

Step 3 dispatches on the model version's `framework` tag, but the matching library must also be installed in the Ray Serve image — otherwise the load raises `ModuleNotFoundError` and the `(model, alias)` entry is dropped from the hot set (the failure is logged, not surfaced at startup). A model can therefore train and register successfully in MLflow yet never appear in `GET /models`.

Add the model's framework library to `serving/ray_serving/requirements.txt` and rebuild the image. Currently pinned there:

| Framework | Models | Required package |
|---|---|---|
| sklearn | JPCP, MCBound | `scikit-learn` (always present) |
| xgboost (sklearn-flavour) | MACK (`XGBClassifier`) | `xgboost==3.2.0` |

Pin the same major version used to train the model so the pickled estimator deserializes cleanly.

### On-demand version cache (Phase 3)

Per-request `version=` lookups that are not in the hot set are loaded on demand into a bounded LRU cache (size controlled by `RAY_VERSION_CACHE_SIZE`, default 8). The least-recently-used entry is evicted when the cache is full.

## Auto-reload mechanisms (Phase 3)

Two complementary mechanisms keep served models in sync with the MLflow registry:

### Polling

With a serving snapshot in force, the background task follows the snapshot instead (above). Only
while none exists does it re-scan MLflow alias state every `RAY_RELOAD_POLL_SECONDS` (default 60
seconds). Set that to `0` to disable MLflow polling entirely (webhook-only mode), and
`RAY_SNAPSHOT_MODE=off` to ignore the snapshot.

### Webhook

Prefect's `promote_task` fires `POST /reload/{model_name}` immediately after setting a new Production alias, with the admin token when `RAY_SERVE_ADMIN_TOKEN` is set where the flow runs. This gives sub-second propagation after a successful pipeline run. Polling acts as a safety net in case the webhook is missed or refused.

Under the [workload-identity overlay](../guides/workload-identity.md#every-hop-to-the-model-server-mutual-tls) the model server listens on loopback only, and only the control plane, the dashboard and the agent may call its admin routes. A webhook from a Prefect runner on the host no longer arrives, and the serving snapshot carries the promotion instead.

### Traffic splits and shadow

`exa serve traffic`, the dashboard Traffic page and Skipper write splits to the shared platform
store; the inference router reads them (30 s cache). Model names match case-insensitively, so a
split set for `JPCP` applies to requests for `jpcp`. A split applies to traffic addressed to the
model's **default alias** (`Production`, what bus jobs send); a request pinned to another alias,
such as `Staging`, gets that alias. Shadow configuration (`exa serve shadow`) is matched the same
way. With a serving snapshot in force, shadow targets and the router's splits come from the
snapshot and the request path reads no table; a split change reaches the router within a few
seconds, once the control plane has compiled it.

### Verify before load

`EXAMLOPS_SERVING_VERIFY` controls signature verification of model artifacts: `off`, `warn`
(the default: verify every load and audit a failure, never refuse) or `enforce` (refuse an
unsigned, tampered, untrusted or unverifiable artifact; serving keeps the last-known-good version).
Serving downloads the version once, verifies it, and loads exactly those bytes. Versions are signed
with Ed25519 when the training pipeline registers them. The replica holds only the signer's public
key (`EXAMLOPS_SIGNING_PUBLIC_KEYS`) and checks the signature record the serving snapshot carries,
so verification works with the database unreachable. Keys, rotation and the steps from `warn` to
`enforce`: [ML supply-chain security](../guides/supply-chain-security.md).

## Replicas and scaling

The model server runs `RAY_NUM_REPLICAS` replicas (default 2). Each one loads the whole hot set
itself; replicas share no state, so any of them can answer any request. The inference pipeline's
three stages (ingress, feature transformer, router) run `INFERENCE_PIPELINE_REPLICAS` replicas each
(default 2). They hold no state either: traffic splits and the serving snapshot come from the
shared store.

**Autoscaling.** Set `RAY_AUTOSCALE_MAX_REPLICAS` and Ray scales the model server between
`RAY_NUM_REPLICAS` (the floor) and that ceiling:

| Variable | Default | Meaning |
|---|---|---|
| `RAY_AUTOSCALE_MAX_REPLICAS` | unset (fixed count) | Ceiling. A value below the floor is raised to it |
| `RAY_AUTOSCALE_TARGET_ONGOING` | `5` | In-flight requests per replica that Ray aims for |
| `RAY_AUTOSCALE_UPSCALE_DELAY_S` | `30` | How long demand must stay high before a replica is added |
| `RAY_AUTOSCALE_DOWNSCALE_DELAY_S` | `300` | How long it must stay low before one is removed |

A new replica loads the full hot set before it takes traffic, so size the ceiling by memory as
well as by throughput, and scale in slowly. The mapping goes through the platform's autoscale
policy (`examlops.autoscale`); a test feeds its output through Ray's own `AutoscalingConfig`,
because Ray ignores keys it does not know. Earlier versions wrote the target under an old key
name, so every policy ran with Ray's default target of 2.

**When a replica dies.** Ray stops routing to it and starts a replacement. Requests that were
running on it at that moment get a plain-text `500 Internal Server Error` from Ray's proxy (the
model server answers its own errors in JSON, so the two can be told apart). The pipeline's router
retries such a request once, within the deadline and the retry budget. It does not retry twice,
because the request may be what killed the replica: a query that crashes replicas must not take a
third one with it.

A direct call to `/v2/models/{name}/infer` has no router in front of it. Through the
[serving gateway](../guides/serving-gateway.md), Envoy retries transport failures and `503`, not a
`500`, so a direct caller sees the `500` and can retry it: inference has no side effects on the
server.

**On Kubernetes** a replica is a pod, and losing one is the cluster's business rather than Ray's:
what a deletion, a rolling upgrade or a lost node costs the callers — and which pod settings change
that — is measured in [Running the model server on Kubernetes](../guides/serving-on-kubernetes.md).
The short version: the server stops Ray Serve as soon as it is asked to stop, so a caller holding
keep-alive connections loses what those connections were carrying at that instant (single figures
out of tens of thousands), and one retry erases it.

`tests/integration/test_serving_replica_failover_live.py` (`EXAMLOPS_RAY_LIVE=1`) runs two replicas
in a private local Ray cluster, kills one while requests are in flight on it, and requires every
request to succeed. It checks that the kill caught requests in flight; with the router's retry
removed, 4 of its 320 requests fail. It never touches a Ray cluster already running on the host.

## Overload, deadlines and load shedding

Three mechanisms keep an overloaded or slow model from turning into a platform-wide slowdown.

**One deadline per request.** The inference pipeline (`POST /infer-pipeline/infer`) fixes a request's
deadline once, at the ingress: the caller's `X-ExaMLOps-Budget-Ms` header (milliseconds it will
still wait, capped at `INFERENCE_DEADLINE_MAX_SECONDS`), or `INFERENCE_DEADLINE_SECONDS` (30 s)
without one. Every hop spends from that budget and passes on what is left:

```text
client ──X-ExaMLOps-Budget-Ms: 2000──▶ ingress  (deadline fixed here)
         ingress ─ payload _budget_ms ─▶ FeatureTransformer ─▶ ModelRouter
         ModelRouter ──X-ExaMLOps-Budget-Ms: <remaining>──▶ /v2/models/{model}/infer
```

- The router's per-attempt timeout is the remaining budget, not a fixed 10 s per attempt.
- The model server runs the model under the smaller of `RAY_PREDICT_TIMEOUT` and the remaining
  budget. A request whose budget ran out while it waited in a queue is answered 504 **without
  running the model** — nobody is waiting for that answer.
- A pipeline that hangs anyway is answered 504 at the deadline (plus 0.5 s for a downstream's
  own, more specific answer) instead of whenever the slowest hop's timeout fires.

The budget travels as a duration, never a timestamp, because the hops run on different hosts
whose clocks disagree — the same choice gRPC makes with `grpc-timeout`. Time in transit is not
deducted, so a budget errs slightly long, never short.

**Retries that cannot become a storm.** The router retries the model server only on a transport
error, a 503, or (once) a replica that died during the request, at most `INFERENCE_ROUTE_RETRIES`
times, only while the deadline leaves room for
another attempt, and only while its retry budget allows (gRPC-style `retryThrottling`: a failed
attempt costs a token, a success earns `INFERENCE_RETRY_TOKEN_RATIO` back, retries need more than
half of `INFERENCE_RETRY_MAX_TOKENS`). A blip is retried; an outage is not multiplied by three.
The default of 100 tokens lets a burst of about 50 failures be retried: a replica that dies fails
every request it held at once, and each gets its one retry. A sustained outage still earns only
about one retry per ten successes. The default was 10, and the live failover test
(`tests/integration/test_serving_replica_failover_live.py`) lost a request whenever more than five
were on the dying replica.
400, 404 and 504 are never retried, and neither is the model server's own 500: a slow model
answers no faster the second time, and a failing one fails again.

**Why an inference failed.** When no prediction comes back, the pipeline answers HTTP 500
`{"error": "inference_failed", "detail": …, "cause": …}`, and `cause` says why:

| `cause` | Meaning | Evidence about the model? |
|---|---|---|
| `model` | The model server answered this request with an error: the model failed on it | yes |
| `timeout` | The model server's own deadline ran out: slow, not wrong | no |
| `transport` | No answer reached the pipeline after its retries | no |
| `replica_lost` | The serving replica died with the request on it (after the one retry) | no |
| `protocol` | A success answer that is not an inference answer | no |
| `pipeline` | The pipeline's own router failed | no |

Drift tracking and retrain triggers count only `model`, or an answer without a `cause`, from an
older pipeline, which keeps the old meaning. The bus bridge does, so an outage or a lost replica no
longer counts as a worse model.

The router counts both: `ray_examlops_router_retries_total{reason}` has one entry per retry, and
per retry the budget refused (`budget_spent`). `InferenceRetryBudgetSpent` alerts on the second.

**Load shedding.** `RAY_MAX_QUEUED_REQUESTS` (model server) and `INFERENCE_MAX_QUEUED_REQUESTS`
(pipeline ingress) bound how many requests may wait at each caller — the HTTP proxy or a
deployment handle — beyond the `RAY_MAX_ONGOING_REQUESTS` a replica is already working on. Past
the bound Ray Serve answers 503 at once; the pipeline adds `Retry-After: 1`. Both default to `-1`
(unbounded, Ray's default) because the right bound depends on the site: roughly, the requests the
replicas can finish inside one deadline, minus those already in flight —

```text
bound ≈ replicas × (deadline_seconds / p99_latency_seconds) − max_ongoing_requests
```

For 2 replicas, a 30 s deadline and a 0.2 s p99, that is `2 × 150 − 100 = 200`. Even unbounded,
the deadline keeps an overload short-lived: queued requests past their budget are dropped at the
model server without running.

| Answer | Meaning | Client should |
|---|---|---|
| 503 `overloaded` | shed, or retries exhausted against a shedding server | back off (`Retry-After`) and retry |
| 504 `deadline_exceeded` | the budget ran out somewhere on the path | not retry with the same budget |

**What the bound is worth, measured.** The overload drill
(`tests/integration/test_serving_overload_drill_live.py`, `EXAMLOPS_CHAOS_LIVE=1`) puts 600
requests a second at one replica that answers this model in about 15 ms — roughly ten times what it
can finish — with the queue bounded at 4 and again unbounded:

| | `RAY_MAX_QUEUED_REQUESTS=4` | unbounded (`-1`) |
|---|---|---|
| Answers | 744 predictions, 2856 shed with 503 | every request answered |
| p50 of a prediction | 175 ms | 6.0 s |
| p99 of a prediction | 312 ms | 16.0 s |
| 5xx other than the shed 503, dropped connections | none | none |

Both servers stayed healthy and answered in about 18 ms once the burst stopped. Unbounded is not
"more available": it answers everything far too late for a caller that has already given up, which
is what the bound exists to prevent. Shedding early is what keeps the answers that *are* given
useful.

## Metrics

Ray Serve exports Prometheus metrics on port **8080** (configured via `RAY_METRICS_EXPORT_PORT`).
Ray adds the `ray_` prefix to every name the code declares:

| Metric | Type | Labels |
|---|---|---|
| `ray_examlops_predict_requests_total` | Counter | `model_name`, `alias`, `status` (`success`/`error`/`not_found`/`invalid`/`timeout`/`deadline_exceeded`) |
| `ray_examlops_predict_latency_seconds` | Histogram | `model_name`, `alias` |
| `ray_examlops_prediction_value` | Histogram | `model_name` |
| `ray_examlops_model_version` | Gauge | `model_name`, `alias`; the value is the version served |
| `ray_examlops_models_loaded` | Gauge | `replica` |
| `ray_examlops_serving_snapshot_applied_generation` | Gauge | `replica` |
| `ray_examlops_reload_total` | Counter | `scope`, `status`, `replica` |
| `ray_examlops_shadow_total` | Counter | `model_name`, `status` |
| `ray_examlops_router_requests_total` | Counter | `model_name`, `outcome` (`success` or the pipeline's error: `overloaded`, `deadline_exceeded`, `inference_failed`, `model_not_found`, `validation_error`); from the inference pipeline's router |
| `ray_examlops_router_retries_total` | Counter | `reason`: `transport`, `overloaded`, `replica_lost` for a retry made; `budget_spent` for one the retry budget refused |

Two Ray 2.55 behaviours shape how these are produced:

- **Tracing off must not mean metrics off.** Ray records metrics through the OpenTelemetry SDK,
  which `OTEL_SDK_DISABLED=true` (the platform's tracing-off default) disables. The model server
  removes that value before it starts Ray. Tracing stays off.
- **Gauges are republished.** Ray clears a gauge's value each time it is collected, and a replica
  reports to Ray's metrics agent about every 10 seconds. A gauge set once, at load time, is
  therefore seen by one scrape and then disappears. Each replica sets its gauges again from its
  own state every `RAY_GAUGE_REFRESH_SECONDS` (5). For the same reason, do not scrape this port
  more often than every 10 seconds: a faster scraper sees each gauge only every other time.

`tests/integration/test_serving_metrics_live.py` (`EXAMLOPS_RAY_LIVE=1`) shows both on a real Ray.

These are visualised in the Grafana **ExaMLOps Online Metrics** dashboard. See [Grafana](grafana.md).

## Ray Dashboard

The Ray cluster dashboard is at **http://localhost:18265**, published on loopback only: its Jobs
API runs arbitrary code without authentication. From another machine, use an SSH tunnel
(`ssh -L 18265:localhost:18265 <host>`). It shows:

- Serve deployments and replica status
- Per-replica memory and CPU usage
- Request throughput and error rates
- Actor logs

## Configuration

| Env variable | Default | Purpose |
|---|---|---|
| `MLFLOW_TRACKING_URI` | `http://localhost:15000` | MLflow server to load models from |
| `MODEL_STAGE` | `Production` | Default alias when no `alias` or `version` is given in the request |
| `RAY_NUM_REPLICAS` | `2` | Replicas of the model server; the floor when autoscaling is on |
| `RAY_AUTOSCALE_MAX_REPLICAS` | unset | Turns on Ray autoscaling up to this many replicas ([Replicas and scaling](#replicas-and-scaling)) |
| `RAY_AUTOSCALE_TARGET_ONGOING` / `_UPSCALE_DELAY_S` / `_DOWNSCALE_DELAY_S` | `5` / `30` / `300` | Autoscaling target and delays |
| `INFERENCE_PIPELINE_REPLICAS` | `2` | Replicas of each inference-pipeline stage |
| `RAY_SERVE_PORT` | `8001` | Internal HTTP serving port (host-exposed as 18001) |
| `RAY_SERVE_GRPC_PORT` | `8081` | Open Inference Protocol v2 over gRPC (host `18081` in Compose); `0` turns it off. It binds `RAY_SERVE_HOST`. |
| `RAY_SERVE_GRPC_MAX_MESSAGE_MB` | `8` | Largest gRPC request or answer, like the gateway's REST body limit. Larger requests get `RESOURCE_EXHAUSTED`. |
| `RAY_SERVE_HOST` | `0.0.0.0` | Address the HTTP port binds. The [workload-identity overlay](../guides/workload-identity.md#every-hop-to-the-model-server-mutual-tls) sets `127.0.0.1`, so the mutual-TLS sidecar in the model server's own network namespace is the one way in and `18001` is not published. |
| `RAY_PRELOAD_ALIASES` | `Production,Canary,Staging` | Comma-separated aliases pre-loaded into the hot set on startup and reload |
| `RAY_VERSION_CACHE_SIZE` | `8` | LRU cache size for on-demand raw-version requests |
| `RAY_RELOAD_POLL_SECONDS` | `60` | Background alias-poll interval in seconds; set to `0` to disable |
| `RAY_SERVE_RELOAD_URL` | unset | Ray Serve URL used by Prefect's promote_task for the per-model reload webhook |
| `RAY_METRICS_EXPORT_PORT` | `8080` | Prometheus metrics port |
| `RAY_GAUGE_REFRESH_SECONDS` | `5` | How often each replica republishes its gauges (Ray exports a gauge only once per set); `0` disables |
| `RAY_PREDICT_TIMEOUT` | `30` | Hard ceiling (s) on one `model.predict()`; a caller's shorter budget lowers it |
| `RAY_MAX_ONGOING_REQUESTS` | `100` | Concurrent requests per replica |
| `RAY_MAX_QUEUED_REQUESTS` / `INFERENCE_MAX_QUEUED_REQUESTS` | `-1` | Load-shedding bound for the model server / the pipeline ingress (`-1` = unbounded) |
| `INFERENCE_DEADLINE_SECONDS` / `INFERENCE_DEADLINE_MAX_SECONDS` | `30` / `300` | Default and maximum request budget |
| `INFERENCE_ROUTE_RETRIES` | `2` | Ceiling on router retries (transport errors, 503, and one retry of a replica lost mid-request) |
| `INFERENCE_RETRY_MAX_TOKENS` / `INFERENCE_RETRY_TOKEN_RATIO` | `100` / `0.1` | Router retry budget: a burst of ~50 retries, then ~1 per 10 successes |
| `RAY_MODELS_DIR` | unset | Phase 14: directory of per-model YAML files (e.g., `usecases/seanergy/models`). Takes precedence over `RAY_REGISTRY_PATH` |
| `RAY_REGISTRY_PATH` | unset | Path to `model_registry.yaml`; enables per-model `serve_aliases`. Unset = use global `RAY_PRELOAD_ALIASES` for all models |
| `RAY_REGISTRY_ENV` | unset | Env overlay name (e.g., `prod`) loaded alongside `RAY_REGISTRY_PATH` |

### Per-model serve aliases (Phase 14 — per-model YAML)

By default all models share the same `RAY_PRELOAD_ALIASES` list. Set `RAY_MODELS_DIR` to enable per-model alias control from the per-model YAML files:

```bash
# Ray Serve reads per-model aliases from pipelines/models/*.yaml
RAY_MODELS_DIR=pipelines/models exa stack up --service ray-serving
```

Each model's `serving.aliases:` list in its YAML file controls which MLflow aliases are pre-loaded:

```yaml
# pipelines/models/jpcp.yaml
serving:
  model_id: jpcp
  aliases: [Production, Canary, Staging]
```

When `RAY_MODELS_DIR` is unset, `RAY_REGISTRY_PATH` (legacy monolithic registry) is tried next. When both are unset, Ray Serve falls back to the global `RAY_PRELOAD_ALIASES` env var.

## Benchmarking and load testing

`exa serve benchmark` sends requests one after another and reports their latency. That measures a
server nobody is loading. To see what happens under load, use `exa serve loadtest`:

```bash
# 50 requests per second for a minute; fail (exit 1) if p99 > 300 ms or more than 1 % fail
exa serve loadtest jpcp --rate 50 --duration 60 --p99-ms 300 --max-error-rate 0.01

# Through the serving gateway: its key from the environment, never the command line
EXAMLOPS_LOADTEST_TOKEN=exa-… exa serve loadtest jpcp --url http://localhost:18088

# A model without a column signature: pass a real OIP v2 request
exa serve loadtest mack --body request.json --rate 20 -o json
```

It is an **open-loop** test. Requests leave on a fixed schedule whatever the server does, and each
latency counts from when the request was due. A tester that waits for each answer before sending
the next slows down with the server, and so reports the service time of a server that is not
overloaded ("coordinated omission"). This one reports the queueing callers really suffer.

| Column | Means |
|---|---|
| Sent / OK / Failed | Requests sent, answered 2xx, and not (any other status, a transport error, or no answer) |
| Shed | `429` and `503`: the gateway's quota, or the model server's queue bound, refusing |
| Dropped | Due while the client already had `--max-in-flight` requests outstanding. Any drop fails the run: the test no longer measured the schedule it was given |
| p50–max | Latency of successful requests, in milliseconds, from when each was due |

**Finding capacity.** Raise `--rate` step by step. Latency stays near the service time until the
offered rate reaches what the replicas can serve. Past that point the queue, and p99 with it,
grows for as long as the run lasts. That point is the capacity. Use it to size
`RAY_MAX_QUEUED_REQUESTS` (see [Overload](#overload-deadlines-and-load-shedding)), then run
again above capacity: requests should now be shed with `503`, and p99 should stay bounded.

The request is built from the model's `GET /v2/models/{name}` metadata (one zero-valued row) when
the model has a column signature. Without one, the metadata does not say how wide a row is, and
the command asks for `--body`.
