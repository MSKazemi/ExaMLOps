# DataPlane Bridge — Operational Guide

The DataPlane bridge connects ExaMLOps inference to an external Cap'n'Proto/TCP message bus.
It is a separate process managed via Docker Compose (`--profile dataplane`) or bare-metal.

In dev and production, the bridge connects to the real DataPlane from the companion `dataplane` repo (see [DataPlane setup guide](dataplane-sim.md)).

## Protocol

### Message Types

| Type ID | Name | Direction | Purpose |
|---|---|---|---|
| 5 | `VectorReqV1` | DataPlane → Bridge | Generic feature-vector inference request |
| 6 | `VectorResV1` | Bridge → DataPlane | Generic inference result (single prediction) |
| 7 | `HpcJobV1` | DataPlane → Bridge | HPC job inference request |
| 8 | `HpcInferenceResV1` | Bridge → DataPlane | HPC job inference result |
| 9 | `RetrainReqV1` | DataPlane → Bridge | Manual retrain trigger |
| 10 | `RetrainResV1` | Bridge → DataPlane | Retrain acknowledgement |

### HpcJobV1 Fields

| Field | Type | Notes |
|---|---|---|
| `jobId` | Text | Unique HPC job identifier |
| `userId` | UInt32 | Submitting user ID |
| `numNodes` | UInt32 | Requested node count |
| `numCpus` | UInt32 | Requested CPU count |
| `partition` | Text | Slurm partition name |
| `walltimeSecs` | UInt64 | Wall-clock limit in seconds |
| `timestamp` | UInt64 | Submission timestamp (ms) |
| `embedding` | List(Float64) | 384-dim pre-computed embedding vector |
| `modelName` | Text | Target model (empty → `DATAPLANE_DEFAULT_MODEL`) |
| `alias` | Text | MLflow alias (empty → `DATAPLANE_DEFAULT_ALIAS`) |

### Feature routing

The bridge uses a `ModelSchemaRegistry` (loaded from `pipelines/models/*.yaml`) to extract the correct input features for each model from the incoming `HpcJobV1` message. It calls `registry.build_features(model_name, msg)` and `registry.validate_features()` before forwarding to the inference pipeline (`POST /infer-pipeline/infer`).

The `FeatureTransformer` inside the pipeline then extracts only the declared feature fields for the model call — `numNodes` and `userId` are retained as top-level metadata. All current models consume only the 384-dim `embedding` vector, but the schema registry allows future models with different input shapes to be added without bridge code changes.

### Bridge modes

| Mode | Env value | Behaviour |
|---|---|---|
| Req/Res | `reqres` | Register one handler per model UUID (from YAML); handle inference + optional retrain |
| Pub/Sub | `pubsub` | Subscribe to `JOB_TOPIC_UUID` topic (not used with real DataPlane) |
| Both | `both` | All handlers concurrently (legacy) |

The real DataPlane (`dataplane` repo) uses `reqres` mode exclusively. The bridge is hardcoded to `DATAPLANE_MODE=reqres` in Docker Compose.

### Connection-per-role design

Each subscription or service registration gets its own `Connection` object. The dataplane `read_msg()` loop is a single blocking async loop over the TCP stream — one connection per role. In `both` mode the bridge opens four connections concurrently under `asyncio.gather()`:

| Connection | Role |
|---|---|
| `pubsub_conn` | Subscribe to `JOB_TOPIC_UUID`; publish to `RESULT_TOPIC_UUID` |
| `<model>_conn` × N | One per model — serve `dataplane_uuid` from each YAML (per-model req/res) |
| `inf_conn` | Legacy global handler — used only when no YAML UUIDs are present |
| `retrain_conn` | Serve `RETRAIN_UUID` — RetrainReqV1 → RetrainResV1 |
| `vector_conn` | Serve `VECTOR_UUID` — VectorReqV1 → VectorResV1 |

## Prerequisites

1. DataPlane Python bindings installed:
   ```bash
   make dataplane-install
   ```
2. A DataPlane server reachable at `DATAPLANE_HOST:DATAPLANE_PORT`.
3. Ray Serve running (`:18001`) for inference handlers.
4. Control Plane running (`:18002`) for retrain handlers.

## Environment Variables

| Variable | Default | Required | Purpose |
|---|---|---|---|
| `DATAPLANE_HOST` | `localhost` | always | DataPlane server hostname |
| `DATAPLANE_PORT` | `<PORT>` | always | DataPlane server TCP port |
| `DATAPLANE_MODE` | `reqres` | always | Always `reqres` with real DataPlane |
| `DATAPLANE_RETRAIN_UUID` | — | optional | Req/res service UUID for `RetrainReqV1 → RetrainResV1` |
| `DATAPLANE_VECTOR_UUID` | — | optional | Req/res service UUID for `VectorReqV1 → VectorResV1` |
| `DATAPLANE_DEFAULT_MODEL` | `JPCP` | — | Fallback model when `HpcJobV1.modelName` is empty |
| `DATAPLANE_DEFAULT_ALIAS` | `Production` | — | Fallback MLflow alias when `HpcJobV1.alias` is empty |
| `RAY_SERVE_URL` | `http://localhost:18001` | — | Ray Serve base URL (host-side) |
| `CONTROL_PLANE_URL` | `http://localhost:18002` | — | Control plane base URL (host-side) |
| `CONTROL_PLANE_TOKEN` | — | — | Bearer token for `POST /retrain` |
| `DRIFT_WINDOW` | `50` | — | Rolling window size for per-model drift detection |
| `DRIFT_THRESHOLD` | `0.5` | — | Error-rate threshold that triggers `POST /retrain` |
| `DRIFT_COOLDOWN` | `300` | — | Seconds between auto-retrain triggers per model |
| `MODELS_YAML_DIR` | `pipelines/models` | — | Directory scanned by `ModelSchemaRegistry` at startup |

## Per-Model UUIDs

Each model registered in `pipelines/models/*.yaml` can declare a `dataplane_uuid` field. The bridge reads these at startup and registers one req/res handler per model — HPC callers can address a specific model by UUID without embedding a model name in the message payload.

**View UUIDs:**
```bash
exa dataplane list
# or in the dashboard: /dataplane → Model UUIDs table
```

**Assign UUIDs to all models (run once):**
```bash
exa dataplane init-uuids
git add pipelines/models/ && git commit -m "feat: assign DataPlane UUIDs"
```

**Backward compatibility:** If no model YAMLs have `dataplane_uuid` set, the bridge falls back to the legacy `DATAPLANE_INFERENCE_UUID` single-handler behaviour.

## Starting the Bridge

### Prerequisites

Real DataPlane must be running first (from the `dataplane` repo):

```bash
# one-time: create the shared Docker network
docker network create dataplane-net

# start real DataPlane + JPCP request generator
cd ../dataplane && docker compose up -d
```

### Docker Compose (recommended)

```bash
make dataplane-up            # start bridge container (connects to dataplane-reqgen:<PORT>)
make dataplane-bridge-logs   # tail bridge logs
make dataplane-reqgen-logs   # tail reqgen logs (inference_requests.log + dataplane.log)
make dataplane-down          # stop bridge
```

### Bare-metal (dev/debug)

```bash
DATAPLANE_HOST=localhost make dataplane-bridge-up
```

## Smoke Testing

Send one JPCP req/res inference request and print the response:

```bash
make dataplane-test-req
```

Expected output:
```
Sending HpcJobV1 → <UUID>
  job_id=1  user_id=42  num_nodes=8
{
  "prediction": 91.36,
  "model_name": "JPCP",
  "model_version": "18",
  "run_id": "5f015850..."
}
```

## Bridge Status

```bash
exa dataplane status
```

Or directly:
```bash
curl http://localhost:18003/health   # {"status":"ok","mode":"both"}
curl http://localhost:18003/stats    # {"inferences_total":…, "vectors_total":…, …}
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bridge exits immediately | UUID env vars not set for the configured mode | Set the required UUIDs (see table above) |
| `[Errno -3] Temporary failure in name resolution` (bridge loops on reconnect) | `dataplane-reqgen` container is stopped or was never started | `cd ../dataplane && docker compose up -d` — or `make full-up` which starts it automatically |
| `type X, got Y` error in logs | Message type mismatch — sender using wrong type ID | Check `payloadType` integer matches `dataplane_msgs` constants (5-10) |
| `Connection refused` on Ray Serve call | Ray Serve not running | `exa stack up --service ray-serving` or `make stack-up` |
| `503` from control plane | `CONTROL_PLANE_TOKEN` not set | Set the token or start without auth |
| Stats show errors but no retrain | Drift cooldown not elapsed | Wait `DRIFT_COOLDOWN` seconds (default 300) |
| Dashboard shows "Bridge offline" | Bridge not running, or wrong probe URL | Start bridge (`make dataplane-up`). In Docker the dashboard probes `http://dataplane-bridge:8003` (set via `DATAPLANE_BRIDGE_STATUS_URL` in docker-compose). If overriding, set the correct URL via Config → DataPlane → Bridge Status URL. |

## Adding a New Message Handler

1. **Schema:** add the new struct to `platform/clients/dataplane_msgs/msg.capnp` with the next available type ID.
2. **Python class:** add `class MyMsgV1` and `class MyResV1` to `platform/clients/dataplane_msgs/__init__.py` with `from_capnp` / `to_capnp` methods, following the `VectorReqV1` pattern.
3. **Handler:** add `async def _handle_my_msg(req: MyMsgV1) -> MyResV1` to `platform/clients/dataplane_bridge.py`.
4. **UUID:** add `MY_UUID = _parse_uuid("DATAPLANE_MY_UUID")` in the config block.
5. **`_run_reqres`:** add a `my_conn` parameter, add the `if MY_UUID is not None: tasks.append(...)` block.
6. **`main()`:** open `my_conn = Connection(...)` and `await my_conn.connect()` in both `reqres` and `both` branches; include it in `asyncio.gather()`.
7. **Env vars:** add `DATAPLANE_MY_UUID: "${DATAPLANE_MY_UUID:-}"` to the docker-compose `dataplane-bridge` env block and to `dataplane-bridge-up` in the Makefile.
8. **Docs:** add a row to the message-type table in `docs/guides/dataplane.md` and add the env var to the table above.
