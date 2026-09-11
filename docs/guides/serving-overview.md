# Serving on ExaMLOps: classical models, LLMs and agents

ExaMLOps serves three kinds of workload today, each on its own path. This page says which path a
workload takes, which commands operate it, and — just as important — what is **not** there yet, so
you do not plan around a capability that only exists as a manifest or a stub.

| Kind | Examples | Serving path today |
|---|---|---|
| **Classical ML / deep learning** | scikit-learn regressors and classifiers, PyTorch or Hugging Face models logged to MLflow | Ray Serve `MultiModelServer` (Docker Compose), selected by MLflow alias |
| **LLMs and vision-language models** | open-weight chat, instruct and VLM models | a `vllm serve` process managed by `exa serve llm`, on one of four launchers |
| **Agents** | the platform's own management agent, Skipper | the Skipper FastAPI service, reached with `exa ask`, the dashboard, or its OpenAI-compatible endpoint |

## Classical ML and deep learning — Ray Serve

Models registered in MLflow are served by one Ray Serve application that keeps the lifecycle aliases
(`Production`, `Canary`, `Staging`) hot, loads other versions on demand, and picks up alias moves from
MLflow on a polling interval. The API is on **http://localhost:18001** (interactive docs at `/docs`).

The request protocol is ExaMLOps-specific: a JSON object of named numeric features, optionally with
an alias or version to pin.

```bash
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2, "feature_1": 0.8}, "alias": "Canary"}'
```

Features are flattened into one numeric row, so this path is for **tabular / numeric** inputs. The
full endpoint list is in [All Interfaces](interfaces.md#ray-serve-api).

Operate it with:

```bash
exa serve check                      # health + one prediction per model
exa serve infer-check                # smoke-test the inference pipeline with a synthetic HPC job
exa serve models                     # what is hot-loaded right now
exa serve reload                     # hot-reload Production models from MLflow
exa serve traffic JPCP --production 90 --canary 10 --dry-run   # preview a split
exa serve traffic JPCP --production 90 --canary 10 --reason "canary v18"
exa serve shadow enable JPCP         # mirror traffic to a shadow version
exa serve shadow log JPCP            # last 20 shadow comparisons
```

Shadow mirroring, champion–challenger and A/B tests are described in
[Champion-challenger](shadow-champion-challenger.md).

## LLMs and VLMs — `exa serve llm`

Generation runs in a **`vllm serve`** process; the platform is an OpenAI-compatible HTTP client of it
(the `vllm-server` engine). `exa serve llm` starts or registers that process and records it in an
endpoint registry. Four launchers decide *where* it runs:

| Launcher | What it does |
|---|---|
| `external` (default) | registers a server someone else runs — starts nothing, works on a CPU-only host |
| `compose` | a GPU `vllm` service in the Docker Compose stack |
| `slurm` / `flux` | submits a serving job to an HPC cluster (tensor-parallel in-node, pipeline-parallel across nodes) and records the allocated node's address |
| `kserve` | renders a KServe manifest — see the limitations below |

```bash
exa serve llm start qwen --base-url http://gpu01:8000 --hf-model Qwen/Qwen3-8B
exa serve llm start qwen-vl --launcher slurm --nodes 2 --gpus 4 --dry-run   # preview an HPC launch
exa serve llm list
exa serve llm status qwen            # registry record, substrate status, live vLLM metrics
exa serve llm health qwen            # exits 1 when not ready — usable as a CI gate
exa serve llm args qwen              # the exact `vllm serve` argv the model's engine block renders
exa serve llm chat qwen -m "hello"   # direct smoke test against the server
exa serve llm bench qwen             # time to first token and output tokens/s
exa serve llm stop qwen
```

Every registered endpoint is also a route in the [model gateway](model-gateway.md), so
`exa gateway chat qwen --message "hello"` reaches the same model with virtual keys, budgets,
guardrails, the semantic cache and per-call cost applied. `exa serve llm chat` deliberately bypasses
all of that — use it to check that a model answers, and the gateway for anything else.

```bash
exa models engine list               # vllm / vllm-server, vllm-inproc, sglang, echo
exa models engine validate <model.yaml>
```

Details: [LLM & VLM serving](vlm-serving.md) · [LLM serving engines](llm-serving-engines.md).

## Agents — Skipper

Skipper is the platform's management agent: a LangGraph agent with human approval for writes,
per-session conversation state and a supervisor that routes each turn to a specialist. It runs as a
FastAPI service on port **18004** with a WebSocket chat, a REST API and an optional OpenAI-compatible
`POST /v1/chat/completions` endpoint (conversation state keyed by the `X-Session-ID` header).

```bash
exa ask "which models drifted this week?"
exa ask "now retrain the worst one" --session ops-1     # keep context across turns
exa ask --session ops-1 --approve <action-id>           # approve one pending write
```

Details: [Management Agent](agent.md) · [AgentOps](agentops.md).

## Which path for my workload?

| If your workload is… | Use | Because |
|---|---|---|
| a tabular model logged to MLflow (sklearn, XGBoost-style, small PyTorch) | Ray Serve — register the model, promote an alias, `exa serve reload` | aliases, shadow, traffic split and drift monitoring all apply |
| an LLM or VLM you or a colleague already run with vLLM | `exa serve llm start <name> --base-url …` | nothing to start; the gateway applies keys, budgets and cost |
| an LLM that needs GPUs on an HPC cluster | `exa serve llm start <name> --launcher slurm` (or `flux`) | the serving job goes through the cluster's scheduler |
| a batch of predictions from a file | `exa serve batch submit JPCP input.jsonl --output preds.json` | synchronous batch inference against a model alias from JSON/JSONL input |
| an agent of your own | not supported as a hosted workload yet — see below | |

## Current limitations

These are the parts that exist as code but do not yet do what their names suggest. Plan around them.

- **Kubernetes/KServe is manifest generation only.** `exa serve manifest` and the `kserve` launcher
  produce `InferenceService` / `LLMInferenceService` manifests for a resolved model version,
  validated against the KServe schema the platform pins and, when `kubectl` and a cluster are
  reachable, `kubectl apply --dry-run=server`. Nothing is applied to a cluster. See
  [Kubernetes serving](kubernetes-serving.md).
- **The gateway is a library, not a service.** `examlops.gateway` runs inside the process that calls
  it (`exa gateway chat`, RAG, the challenger judge). There is no standalone gateway endpoint to point
  other clients at.
- **Skipper does not call models through the gateway.** It builds its model client from its own
  configuration, so gateway keys, budgets, guardrails and per-call cost do not apply to its calls.
- **SGLang is a stub.** Only the vLLM engines and the `echo` development engine serve requests.
- **Quantization records provenance only.** `exa models quantize` registers, signs and records a
  quantized version, but no quantizer runs on any host.
- **No customer-agent hosting.** Skipper is the one agent the platform runs. There is no registry,
  versioning or runtime for bringing your own agent.
- **KV-cache-aware routing is simulated.** `exa serve routing simulate` compares routing strategies
  on a synthetic request stream; it does not route live traffic.

## Direction

The platform is moving toward one lifecycle — register, evaluate, gate, promote, deploy, observe, roll
back — shared by classical models, LLMs and agents, with the same governance applied to each and
standard contracts per workload kind: the Open Inference Protocol v2 for predictive models,
OpenAI-compatible APIs for generative models, and A2A for agents. This page is updated as those pieces
ship; until then, the limitations above are the current state.
