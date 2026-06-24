# ExaMLOps Management Agent

The ExaMLOps Management Agent is a conversational CLI that lets operators manage, monitor, and control the ExaMLOps platform through natural language. Built on LangGraph's ReAct loop with a local Ollama LLM backend, it can query the MLflow model registry, run live inference via Ray Serve, pull Prometheus metrics, and trigger Prefect retraining runs — all from a single prompt interface. It fits into the platform as an operator-facing layer on top of the same HTTP APIs used by the dashboard and control plane, requiring no additional services beyond Ollama.

## Prerequisites

If you have `ollama-tunnel` configured (Omega server, port 11436), start the tunnel first:

```bash
ollama-tunnel start          # starts Omega tunnel → localhost:11436 (default)
ollama-tunnel status         # verify: shows models + connection state
```

`AGENT_OLLAMA_URL` and `AGENT_MODEL` are set in `.env`, so `make agent` picks them up automatically.

**Local Ollama:**
If you are running a local Ollama instance, override the URL and model:
```bash
AGENT_OLLAMA_URL=http://localhost:11434 AGENT_MODEL=llama3.2 make agent
```

At least one ExaMLOps service must be running. The agent gracefully reports when a service is unreachable:
- MLflow only → model registry queries work
- MLflow + Ray Serve → model queries and inference work
- Full stack (`make stack-up`) → all tools available

## Quick Start

```bash
ollama-tunnel start   # ensure Omega tunnel is up (if using tunnel; skip for local ollama)
make agent            # reads AGENT_OLLAMA_URL + AGENT_MODEL from .env automatically
```

The agent prints its startup banner and drops into a REPL:

```
ExaMLOps Agent  (model: llama3.1:8b  ·  ollama: http://localhost:11436)
Type your question, or 'exit' to quit.

ExaMLOps Agent >
```

Type any question in plain English. The agent will select and call the appropriate tools, printing `[tool: <name>]` for each tool invocation, then return a formatted answer. Type `exit` or press `Ctrl+C` to quit.

## Example Conversations

### 1. Querying model status

```
ExaMLOps Agent > What models are registered and which versions are in Production?

  [tool: list_models]

The MLflow registry contains the following models:

- **jpcp**: versions=[v3 (None)] aliases=[Production=v3, Canary=v2, Staging=v1]
- **mack**: versions=[v1 (None)] aliases=[Staging=v1]
- **mcbound**: versions=[v2 (None)] aliases=[Production=v2, Staging=v1]

JPCP v3 and MCBound v2 are currently in Production. MACK has only reached Staging.

ExaMLOps Agent > Show me only the JPCP model.

  [tool: list_models]

- **jpcp**: versions=[v3 (None)] aliases=[Production=v3, Canary=v2, Staging=v1]

JPCP has three promoted versions. v3 is live in Production, v2 is in Canary (parallel shadow traffic), and v1 remains in Staging.
```

### 2. Running inference

```
ExaMLOps Agent > Run a prediction for JPCP using features [1.2, 0.8, 3.4, 0.5, 2.1].

  [tool: predict]

The JPCP Production model returned:

- **prediction**: 142.7
- **model version**: v3
- **alias**: Production

ExaMLOps Agent > Run the same input against the Canary version.

  [tool: predict]

The JPCP Canary model (v2) returned:

- **prediction**: 138.4
- **model version**: v2
- **alias**: Canary

The Canary model predicts ~3% lower power consumption for this feature vector. Both results are within normal operating range.
```

### 3. Checking metrics

```
ExaMLOps Agent > How many inference requests have each model received?

  [tool: get_metrics]

Request counts from Prometheus:

- jpcp (status=success): 4,821
- jpcp (status=error): 3
- mcbound (status=success): 1,204
- mack (status=success): 89

JPCP has served the most traffic. The 3 errors on JPCP are worth investigating — check Ray Serve logs for details.

ExaMLOps Agent > What is the average prediction latency over the last 5 minutes?

  [tool: get_metrics]

Average prediction latency (5-minute window):

- jpcp: 18 ms
- mcbound: 22 ms
- mack: 31 ms

All three models are well within acceptable latency bounds. MACK's higher latency likely reflects its XGBoost tree traversal on larger embeddings.
```

### 4. Triggering a retrain

```
ExaMLOps Agent > Retrain the JPCP model on PM100Dataset using dummy data.

  [tool: trigger_retrain]

Retraining has been triggered:

- **model**: JPCP
- **dataset**: PM100Dataset
- **dummy data**: yes
- **flow_run_id**: f3a9c21b-4d0e-4c12-a8f1-7b3e592d1a40

You can poll the run state at: GET http://localhost:18002/retrain/f3a9c21b-4d0e-4c12-a8f1-7b3e592d1a40

ExaMLOps Agent > What happens if I retrain with real Zenodo data?

To use real data, I would call trigger_retrain with is_dummy=False. That causes the pipeline to download the full PM100 dataset from Zenodo (several hundred MB) rather than generating a small synthetic array. Use it for production retrains; keep is_dummy=True for smoke tests and CI.
```

Note: `trigger_retrain` requires `CONTROL_PLANE_TOKEN` to be set. If it is missing, the tool returns:
```
Error: CONTROL_PLANE_TOKEN is not set. Cannot trigger retraining.
```

### 5. Generating a full report

```
ExaMLOps Agent > Generate a full platform status report.

  [tool: generate_report]

## ExaMLOps Status Report — 2026-05-20 09:14 UTC

### Registered Models
- jpcp: versions=[v3 (None)] aliases=[Production=v3, Canary=v2, Staging=v1]
- mack: versions=[v1 (None)] aliases=[Staging=v1]
- mcbound: versions=[v2 (None)] aliases=[Production=v2, Staging=v1]

### Request Counts
Metrics (examlops_predict_requests_total):
  model=jpcp, status=success: 4821
  model=jpcp, status=error: 3
  model=mcbound, status=success: 1204
  model=mack, status=success: 89

### Average Latency (5m)
Metrics (rate(...)):
  model=jpcp: 0.018
  model=mcbound: 0.022
  model=mack: 0.031
```

The report is plain Markdown — you can pipe the session output to a file or paste it into a Notion page or Slack message.

## Changing the Model

The following models are available on the **Omega** tunnel (port 11436) via `ollama-tunnel`. No pull required:

| Model | Size | Notes |
|---|---|---|
| `llama3.1:8b` | 8B | **Default** — fast, solid tool calling |
| `llama3.1:70b` | 70B | Best Llama quality, ~4× slower |
| `llama3.3:70b` | 70B | Latest Llama 3 generation |
| `hermes3:8b` | 8B | Tool-optimised fine-tune, fast |
| `hermes3:70b` | 70B | Best tool use overall |
| `ministral-3:14b` | 14B | Mistral mid-size |
| `qwen3-coder:30b` | 30B | Strong reasoning + code |
| `devstral-small-2:24b` | 24B | Code-focused |
| `gpt-oss:20b` | 20B | OpenAI-style fine-tune |
| `nomic-embed-text` | — | Embeddings only — not for agent use |

Switch model via the env var (no need to edit `.env`):

```bash
AGENT_MODEL=hermes3:70b make agent      # best tool calling
AGENT_MODEL=llama3.1:70b make agent     # best Llama quality
AGENT_MODEL=qwen3-coder:30b make agent  # strong reasoning
```

To use the **Kapa** tunnel instead (16 models, via Monte Cimone SSH, port 11437):

```bash
ollama-tunnel start kapa
AGENT_OLLAMA_URL=http://localhost:11437 make agent
```

**Local Ollama (laptop):** pull the model first, then override the URL:

```bash
ollama pull llama3.1
AGENT_OLLAMA_URL=http://localhost:11434 AGENT_MODEL=llama3.1 make agent
```

## Available Tools

The agent exposes 34 tools across 9 groups. The LLM selects the appropriate tool(s) automatically based on your prompt.

| Group | Tools | Description |
|---|---|---|
| **registry** | `list_models`, `describe_model`, `list_datasets` | Query the MLflow model registry: list all models with their versions and aliases, describe a single model in detail, or list registered datasets. |
| **inference** | `predict`, `predict_pipeline`, `list_loaded_models`, `reload_models` | Run predictions via Ray Serve (`predict` for a feature vector, `predict_pipeline` via the inference pipeline ingress); list or hot-reload currently loaded models. `reload_models` is a write tool requiring confirmation. |
| **metrics/health** | `get_metrics`, `platform_health`, `generate_report` | Execute arbitrary PromQL queries against Prometheus, probe the health of all platform services (MLflow, Ray, Prefect, Control Plane, Dashboard), or generate a full Markdown status report. |
| **training** | `list_pipeline_models`, `trigger_retrain`, `get_retrain_status` | List models known to the pipeline generator, schedule a Prefect retraining run (write — requires confirmation), or poll a flow run for its current state. |
| **approvals** | `list_pending_approvals`, `approve_model`, `reject_model` | List model-change approvals waiting in the control plane; approve or reject a pending model (both write tools requiring confirmation). |
| **modelzoo** | `modelzoo_status`, `modelzoo_events`, `modelzoo_get_config`, `modelzoo_sync`, `modelzoo_set_config` | Inspect ModelZoo freshness badges and push-event history; read or update the runtime config (auto-retrain flag, poll interval, watch branch). `modelzoo_sync` and `modelzoo_set_config` are write tools requiring confirmation. |
| **services** | `list_services`, `service_logs`, `start_service`, `stop_service`, `restart_service` | List running platform services with their status; tail container logs; start, stop, or restart a service via the dashboard's Docker-socket controls. Start/stop/restart are write tools requiring confirmation. |
| **pipelines** | `list_deployments`, `list_runs`, `scaffold_preview`, `scaffold_create` | List Prefect deployments and recent flow runs; preview or create a new model scaffold (`exa scaffold` equivalent). `scaffold_create` is a write tool requiring confirmation; use `scaffold_preview` first. |
| **docs/knowledge** | `search_docs`, `read_doc`, `list_docs`, `get_howto` | Search the repo's `docs/` directory by keyword, read a specific doc file, list all available docs, or look up a how-to answer grounded in the documentation. |

## Slash Commands

Type a slash command at the `ExaMLOps Agent >` prompt instead of a question:

| Command | Description |
|---|---|
| `/help` | Show all slash commands |
| `/tools` | Print every registered tool name |
| `/new` | Start a fresh conversation thread (new `thread_id`) |
| `/resume <id>` | Resume a previously saved thread by its `thread_id` |
| `/threads` | List all saved thread IDs in the SQLite checkpoint store |
| `/model <name>` | Switch the Ollama model for the current session |
| `/report` | Ask the agent to generate a full platform status report |
| `/exit` | Quit the agent REPL |

## Write Confirmation

Write and destructive tools pause before acting. When the agent is about to perform an action such as triggering a retrain, approving a model, starting/stopping a service, or creating a scaffold, the CLI prints a one-line summary and prompts:

```
[confirm] trigger_retrain: Retrain JPCP on PM100Dataset (dummy=True)
Proceed? [y/N]
```

Type `y` or `yes` to proceed; anything else cancels and returns `Cancelled — no action taken.`. The confirmation gate is implemented via LangGraph's `interrupt()` mechanism and the SQLite checkpointer, so the same gate will work transparently for future API or UI callers that implement the LangGraph interrupt protocol.

Write tools: `trigger_retrain`, `approve_model`, `reject_model`, `reload_models`, `modelzoo_sync`, `modelzoo_set_config`, `start_service`, `stop_service`, `restart_service`, `scaffold_create`.

## Persistent Memory

Conversations are stored in a SQLite database (`AGENT_DB`, default `./agent_memory.db`) using LangGraph's `SqliteSaver` checkpointer. Each session is identified by a `thread_id` (auto-generated as `cli-<8 hex chars>` on startup).

- Resume a past session: `/resume <id>`
- List all saved sessions: `/threads`
- Start fresh: `/new` (old sessions remain on disk)

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_MODEL` | `llama3.1:8b` | Ollama model name. Must support tool/function calling. Set in `.env`. |
| `AGENT_OLLAMA_URL` | `http://localhost:11436` | Ollama server base URL. `11436` for ollama-tunnel Omega; `11434` for local `ollama serve`. |
| `AGENT_DB` | `./agent_memory.db` | Path to the SQLite file used by the LangGraph `SqliteSaver` checkpointer for persistent conversation threads. |
| `AGENT_DOCS_ROOT` | `<repo>/docs` | Root directory the docs tools (`search_docs`, `read_doc`, `list_docs`, `get_howto`) search. Defaults to the `docs/` folder at repo root. |
| `MLFLOW_TRACKING_URI` | `http://localhost:15000` | Shared with the rest of the stack — controls where registry tools query MLflow. |
| `RAY_SERVE_URL` | `http://localhost:18001` | Shared with the rest of the stack — controls where inference tools send requests. |
| `PROMETHEUS_URL` | `http://localhost:19090` | Shared with the monitoring stack — controls where metrics tools query. |
| `CONTROL_PLANE_URL` | `http://localhost:18002` | Shared with the rest of the stack — controls where training and approval tools post. |
| `CONTROL_PLANE_TOKEN` | unset | Bearer token for `trigger_retrain`. Must match the value set on the control plane. |
| `DASHBOARD_URL` | `http://localhost:18099` | Dashboard base URL used by service-control and pipeline/scaffold tools (they call the dashboard REST API via an authenticated client). |
| `DASHBOARD_ADMIN_PASSWORD` | unset | Admin password for the dashboard. Required for service-control tools (`start/stop/restart_service`, `service_logs`) and scaffold tools. If unset, those tools return an error. |

`AGENT_MODEL` and `AGENT_OLLAMA_URL` are set in `.env` at the repo root. The `agent` Makefile target sources `.env` automatically, so no manual export is needed.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Error: Ollama is not running at http://localhost:11436` at startup | Omega tunnel is not active | Run `ollama-tunnel start` and verify with `ollama-tunnel status`. |
| `Error: Ollama is not running at http://localhost:11434` | Using local Ollama URL but `ollama serve` is not running | Run `ollama serve`, or switch to the tunnel: `AGENT_OLLAMA_URL=http://localhost:11436 make agent`. |
| `Error: Cannot reach MLflow at http://localhost:15000 — ...` in a tool response | MLflow container not running | Run `make stack-up` or `exa status` to check which services are up. |
| `Error: Cannot reach Ray Serve at http://localhost:18001 — ...` | Ray Serve not started | Run `make stack-up` or `exa stack up --service ray-serving`. |
| `Error: CONTROL_PLANE_TOKEN is not set. Cannot trigger retraining.` | `CONTROL_PLANE_TOKEN` env var is absent | Add `CONTROL_PLANE_TOKEN=<token>` to `.env`, then re-run `make agent`. |
| Agent answers questions without calling tools | Chosen model does not support tool calling well | Use `llama3.1:8b` (default) or `hermes3:8b`. Avoid embedding-only models like `nomic-embed-text`. |
| Agent calls a tool but returns confusing output | LLM hallucinated an argument (e.g. wrong feature count) | Rephrase with explicit values: `"run inference on JPCP with features [1.2, 0.8, 3.4, 0.5, 2.1]"`. |
| `httpx.ReadTimeout` in tool output | Service is slow to respond (e.g. MLflow cold start) | Wait for the service to become healthy (`exa status`). The tool timeout is 10 s. |
