# DataPlane Integration — Architecture Deep Dive

Answers three questions: how AI-production identifies the reqgen, how it selects
a model, and how the full request/response path works end-to-end.

---

## 1. How does AI-production "know" the reqgen is related to it?

It doesn't — and it doesn't need to. The contract is purely the **UUID**.
Nothing is configured on the AI-production side about the reqgen. It works the
other way around:

```
dataplane repo                    DataPlane                 ai-productions
──────────────                    (Rust bus)                ─────────────
reqgen hardcodes                                            bridge reads
JPCP_UUID = 30b0f24c              [UUID registry]           jpcp.yaml →
                                                            dataplane_uuid:
conn.request(JPCP_UUID, ...)  ──► routes to whoever    ◄── 30b0f24c
                                  registered 30b0f24c       conn.register(JPCP_UUID)
```

- The reqgen **hardcodes** the UUID in `generator.py`
- The bridge **reads** the same UUID from `pipelines/models/jpcp.yaml`
- If they match → DataPlane connects them. If not → `[?] unexpected payloadType=0`
- Neither side knows anything about the other directly

The UUID in `jpcp.yaml` is the **shared key** agreed out-of-band between the
two repos. The `models.yaml` in the dataplane repo mirrors it.

### UUID locations

| File | Repo | Field |
|------|------|-------|
| `ai-production-inference-request-generator/src/examlops_reqgen/generator.py` | dataplane | `JPCP_UUID = uuid.UUID("30b0f24c-...")` hardcoded |
| `ai-production-inference-request-generator/models.yaml` | dataplane | `uuid: 30b0f24c-...` reference copy |
| `pipelines/models/jpcp.yaml` | ai-productions | `dataplane_uuid: 30b0f24c-...` source of truth |

---

## 2. How does it know which model inside AI-production?

Three layers of resolution:

```
Layer 1 — UUID → model name  (bridge startup, from YAML)
  pipelines/models/jpcp.yaml   → dataplane_uuid: 30b0f24c
  pipelines/models/mack.yaml   → dataplane_uuid: 65611ddc
  pipelines/models/mcbound.yaml→ dataplane_uuid: 1a2c3b5d
  bridge registers one handler per UUID, each handler is bound to a model name

Layer 2 — model name + alias → MLflow version  (Ray Serve)
  HpcJobV1.modelName = "JPCP"
  HpcJobV1.alias     = "Production"
  bridge POSTs: {model_name, alias, embedding, num_nodes, user_id, job_id}
  Ray Serve ModelRouter → MLflow registry → version 18

Layer 3 — version → actual weights  (MLflow)
  alias "Production" → run_id 5f015850 → sklearn model artifact
```

The `_make_inference_handler(model_name)` call in the bridge is what binds UUID
to model name at startup:

```python
# dataplane_bridge.py — _run_reqres()
for model_name, model_uuid in _MODEL_UUIDS.items():   # loaded from YAML
    conn.serve(model_uuid, HpcJobV1, _make_inference_handler(model_name))
#                                     ↑ closure captures "JPCP" for this handler
```

So even if `HpcJobV1.modelName` is empty, the bridge knows it's JPCP because
the request arrived on the JPCP UUID channel.

---

## 3. Full request/response flow — both sides

```
REQGEN (dataplane repo)              DataPlane              BRIDGE → Ray Serve (ai-productions)
───────────────────────              ─────────              ──────────────────────────────────

_make_job():
  job_id    = uuid4()
  num_nodes = 94
  embedding = [384 floats]
  modelName = "JPCP"
  alias     = "Production"
  payloadType = 7  (HpcJobV1)

conn.request(JPCP_UUID, job) ──►  route to 30b0f24c  ──►  raw = read_msg()
                                                           HpcJobV1.from_capnp(raw)
                                                           ↓
                                                           log: ← REQ  job=bcdad28d
                                                                    model=JPCP
                                                                    nodes=94  user=8691
                                                           ↓
                                                           POST /infer-pipeline/infer
                                                             {embedding, model_name, alias,
                                                              num_nodes, user_id, job_id}
                                                           ↓  Ray Serve pipeline
                                                             InferencePipelineIngress
                                                             → FeatureTransformer
                                                             → ModelRouter (alias lookup)
                                                             → sklearn predict()
                                                           ↓
                                                             prediction  = 89.45
                                                             run_id      = 5f015850
                                                             version     = 18
                                                           ↓
                                                           HpcInferenceResV1(
                                                             power_per_node_watts = 89.45,
                                                             model_version        = "18",
                                                             alias                = "Production",
                                                             run_id               = "5f015850"
                                                           )  payloadType = 8
                                                           ↓
                                                           log: → RES  job=bcdad28d
                                                                    prediction=89.45W
                                                           ↓
← RES  payloadType=8      ◄──────  route back to     ◄──  conn.respond_to(raw, res)
JpcpInferenceResV1                 original caller
  power = 89.45W
  version = 18
  run    = 5f015850

log: ← RES  job=bcdad28d  [ok]  196ms
     power=89.45W  version=18  alias=Production  run=5f015850
```

### Key design points

| Point | Detail |
|-------|--------|
| DataPlane is a pure router | It knows nothing about JPCP, Ray Serve, or MLflow — just UUID → registered handler |
| `raw` carries the return address | The object from `read_msg()` is passed to both `from_capnp()` (decode) and `respond_to()` (reply) — correlation is internal to DataPlane |
| `conn.request()` blocks | The reqgen waits synchronously per request; DataPlane holds the correlation and delivers the response when `respond_to()` is called |
| UUID is the only shared state | No service discovery, no config exchange — just a UUID agreed between the two repos |
| `[?] payloadType=0` | Means the bridge has not yet registered that UUID — requests queue or drop until the bridge connects |

### payloadType mapping

| Value | Name | Direction |
|-------|------|-----------|
| 7 | `HpcJobV1` | reqgen → bridge (inference request) |
| 8 | `HpcInferenceResV1` | bridge → reqgen (inference response) |
| 9 | `RetrainReqV1` | external → bridge (retrain trigger) |
| 10 | `RetrainResV1` | bridge → external (retrain ack) |
| 5 | `VectorReqV1` | external → bridge (generic vector inference) |
| 6 | `VectorResV1` | bridge → external (generic vector response) |

---

## Relevant source files

| File | Repo | Role |
|------|------|------|
| `ai-production-inference-request-generator/src/examlops_reqgen/generator.py` | dataplane | Builds and sends `HpcJobV1`, decodes `HpcInferenceResV1` |
| `ai-production-inference-request-generator/src/examlops_reqgen/messages.py` | dataplane | Cap'n'Proto encode/decode for both message types |
| `platform/clients/dataplane_bridge.py` | ai-productions | Registers UUID handlers, calls Ray Serve, replies |
| `platform/clients/dataplane_client.py` | ai-productions | Typed async wrapper around raw `dataplane.client.Connection` |
| `platform/clients/dataplane_msgs/` | ai-productions | Cap'n'Proto schema + Python classes for all message types |
| `pipelines/models/jpcp.yaml` | ai-productions | Source of truth for `dataplane_uuid` |
| `ai-production-inference-request-generator/models.yaml` | dataplane | Mirror of UUIDs — must stay in sync with ai-productions YAMLs |
