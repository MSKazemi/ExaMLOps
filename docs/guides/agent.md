# Skipper — the ExaMLOps management agent

Skipper — the ExaMLOps management agent — lets operators manage, monitor, and control the ExaMLOps platform through natural language. It is built on **LangGraph's ReAct loop** (`langgraph.prebuilt.create_react_agent`) and exposes **45 tools across 10 groups** that query the MLflow model registry, run live inference via Ray Serve, pull Prometheus metrics, inspect drift and audit history, set traffic splits, promote versions, and trigger Prefect retraining runs — all from a single prompt interface. It fits into the platform as an operator-facing layer on top of the same HTTP APIs used by the dashboard and control plane, plus the shared `platform_db` SQLite store, requiring no additional services of its own.

It runs in two modes:

- **CLI** — an interactive REPL (`make skipper`) with slash-commands, write-confirmation prompts, and persistent conversation threads.
- **HTTP server** — a FastAPI service (`agent_server.py`, default port **18004**) serving a streaming WebSocket chat at `/ws/chat/{thread_id}`, a REST history/info API at `/api/*`, and an embedded HTML chat UI at `/`.

## Architecture

```
skipper/
├── graph.py        ReAct graph: create_react_agent(llm, tools=TOOLS, prompt=SYSTEM_PROMPT, checkpointer)
├── llm.py          Backend selection: Azure Foundry → Claude → Ollama (build_llm / check_backend)
├── memory.py       SqliteSaver checkpointer (persistent threads, keyed by thread_id)
├── prompts.py      SYSTEM_PROMPT — tool groups, reasoning rules, write-protection policy
├── confirm.py      @confirmed_write decorator → LangGraph interrupt() human-in-the-loop gate
├── config.py       Env-var resolution (backends, service URLs, paths)
├── cli.py          Interactive REPL + slash commands
├── server.py       FastAPI WebSocket chat + REST + HTML UI
├── chat_html.py    Embedded web chat interface
└── tools/          45 @tool functions in 10 modules, aggregated into TOOLS
    ├── _http.py    request_json() (retry + backoff) + DashboardClient (lazy JWT, auto-reauth)
    ├── registry.py inference.py metrics.py training.py approvals.py
    └── modelzoo.py services.py pipelines.py docs.py platform_ops.py
```

The graph is the standard ReAct cycle — the LLM reasons, emits tool calls, the tools execute against platform HTTP APIs (and `platform_db`), results are fed back, and the model synthesizes a Markdown answer. Conversation state persists across turns via a SQLite `SqliteSaver` checkpointer keyed by `thread_id`. Write tools pause mid-graph for operator confirmation using LangGraph's `interrupt()` mechanism.

### Next-gen architecture (ADRs 0099–0106)

Skipper was extended in eight additive, graceful-degrading phases so it is useful across **every**
ExaMLOps use case — management, monitoring, help, incident response, FinOps/Green-AI, and governance —
while staying **local-first** (no new paid API by default). All of it degrades: with no embeddings /
policy / `platform.db` / hosted model, the agent still works.

| Area | What | Where | ADR |
|---|---|---|---|
| **Supervisor topology** | A deterministic router dispatches each turn to a scoped **specialist** sub-agent (`manager`/`monitor`/`helper`/`finops`/`governor`/`general`) so a local 8B model only sees ~10–20 relevant tools. One parent `StateGraph` → streaming/HITL/memory unchanged. `AGENT_SUPERVISOR_MODE=auto\|single`. | `supervisor.py`, `skills.py`, `router.py` | 0100 |
| **Unified capability surface** | The `examlops.mcp` registry (single source of truth for Skipper, `exa mcp serve`, and the A2A card) covers drift, serving, SLO/fairness, FinOps, gateway, eval, lineage + grounded help. `exa mcp capabilities` lists it grouped by use case. | `examlops/mcp/tools.py` | 0099/0100 |
| **Self-instrumentation** | Each turn's tool calls record to `agent_sessions`/`agent_tool_calls` (`tool_success_rate` is real) + an in-loop circuit-breaker aborts runaway turns. | `instrument.py` | 0103 |
| **Layered write-safety** | Gated, **tiered** MCP writes (A=autopilot-OK, B=confirm, C=human-only); every mutating tool gets exposure + HITL `interrupt()` + policy + audit; tier-C is never bound to the agent. | `mcp_bridge.py`, `memory_eval.py` | 0102 |
| **Proactive monitoring** | `skipper-watch` — an LLM-free daemon that raises drift/cost alerts to the events outbox + audit + episodic memory. `python -m skipper.watch --once\|--daemon`. | `watch.py`, `baselines.py` | 0104 |
| **Self-improving memory** | Consolidation promotes recurring incidents → review-gated candidate procedures; reinforcement deprecates procedures that use failing tools. `python -m skipper.consolidate`. | `consolidate.py`, `reinforce.py` | 0106 |
| **7-tier memory** | Working · experience · **knowledge/docs-RAG** · **monitoring/baseline** · **outcome** · **consolidation** · **tenant scoping** (see [Long-Term Memory](#long-term-memory-phase-25)). | `memory.py`, `knowledge.py`, `scoping.py` | 0101/0104/0105 |

## LLM Backends

`check_backend()` selects a backend at startup — by which environment variables are set **and** whether that backend actually answers — in this preference order:

| Order | Backend | Trigger vars | Default model | Notes |
|---|---|---|---|---|
| 1 | **Azure OpenAI / AI Foundry** | `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` | `gpt-5.5` (`AZURE_OPENAI_DEPLOYMENT`) | Driven via `langchain-openai` `ChatOpenAI` against the OpenAI-compatible Foundry **v1** endpoint (`base_url` + `api_key`, deployment name as model id). Temperature left unset — gpt-5.x reasoning models reject overrides. |
| 2 | **Claude API** | `ANTHROPIC_API_KEY` | `claude-opus-4-8` (`ANTHROPIC_MODEL`) | `ChatAnthropic` with **adaptive thinking** (`thinking={"type": "adaptive"}`), `max_tokens=16000`. |
| 3 | **Ollama** (fallback) | none required | `llama3.1:8b` (`AGENT_MODEL`) | `ChatOllama` at `AGENT_OLLAMA_URL`, `temperature=0`, with `keep_alive` / `reasoning` tuning for CPU-only servers. |

`check_backend()` reports the backend that will actually be used as `{ok, type, model}` and drives the startup banner. The sections below default to the Ollama setup (most common for local dev); set the Azure or Claude vars in `.env` to switch.

**Preferred means preferred-when-usable.** A configured backend whose credential is *rejected* is skipped, not used: the candidates are probed in order and the first working one wins. `build_llm()` then builds whichever one was found working, so the backend the banner reports and the backend that serves your question are always the same. Two extra fields appear when it matters:

| Field | When | Meaning |
|---|---|---|
| `skipped` | a backend was tried and rejected first | which ones, in order — the CLI prints this as a warning, because falling back silently would change every answer's quality without telling you |
| `fix` | nothing is usable | the environment variable to repair, for the **preferred** backend (the one you meant to use) |

With nothing usable, the CLI exits 1 naming what it tried and what to fix, rather than starting and failing on the first token. Until 2026-08-20 it did neither: `build_llm()` chose a backend on env-var *presence* alone, so a rejected Azure key produced a client that raised `AuthenticationError` on the first request while a working Claude/Ollama path sat unused — and the error message blamed Ollama regardless of what had actually failed.

**What `ok` actually means.** The probe authenticates, so `ok` answers *"can this backend serve a request?"* — not merely *"does the host resolve?"*. It is `false` in two distinct failure modes:

| Probe result | `ok` | Meaning |
|---|---|---|
| transport error (DNS / connect / timeout) | `false` | endpoint is down or unroutable |
| `401` / `403` | `false` | endpoint is **up** but the key is invalid, revoked, or issued for another resource |
| any other status (`200`, `404`, `5xx`) | `true` | routable; a `404` on a provider's `/models` path does not imply chat completions is broken |

The 401 case is called out because it is the one that wastes an afternoon: a live gateway rejecting a rotated key looks identical to a healthy backend from the outside. Earlier versions probed the Azure endpoint *unauthenticated* and so reported `ok: true` for a key the service refused.

## Prerequisites

If you have `ollama-tunnel` configured (Omega server, port 11436), start the tunnel first:

```bash
ollama-tunnel start          # starts Omega tunnel → localhost:11436 (default)
ollama-tunnel status         # verify: shows models + connection state
```

`AGENT_OLLAMA_URL` and `AGENT_MODEL` are set in `.env`, so `make skipper` picks them up automatically.

**Local Ollama:**
If you are running a local Ollama instance, override the URL and model:
```bash
AGENT_OLLAMA_URL=http://localhost:11434 AGENT_MODEL=llama3.2 make skipper
```

At least one ExaMLOps service must be running. The agent gracefully reports when a service is unreachable:
- MLflow only → model registry queries work
- MLflow + Ray Serve → model queries and inference work
- Full stack (`make stack-up`) → all tools available

## Quick Start

```bash
ollama-tunnel start   # ensure Omega tunnel is up (if using tunnel; skip for local ollama)
make skipper            # reads AGENT_OLLAMA_URL + AGENT_MODEL from .env automatically
```

The agent prints its startup banner and drops into a REPL:

```
Skipper · ExaMLOps agent  (model: llama3.1:8b  ·  ollama: http://localhost:11436)
Type your question, or 'exit' to quit.

skipper >
```

Type any question in plain English. The agent will select and call the appropriate tools, printing `[tool: <name>]` for each tool invocation, then return a formatted answer. Type `exit` or press `Ctrl+C` to quit.

## Example Conversations

### 1. Querying model status

```
skipper > What models are registered and which versions are in Production?

  [tool: list_models]

The MLflow registry contains the following models:

- **jpcp**: versions=[v3 (None)] aliases=[Production=v3, Canary=v2, Staging=v1]
- **mack**: versions=[v1 (None)] aliases=[Staging=v1]
- **mcbound**: versions=[v2 (None)] aliases=[Production=v2, Staging=v1]

JPCP v3 and MCBound v2 are currently in Production. MACK has only reached Staging.

skipper > Show me only the JPCP model.

  [tool: list_models]

- **jpcp**: versions=[v3 (None)] aliases=[Production=v3, Canary=v2, Staging=v1]

JPCP has three promoted versions. v3 is live in Production, v2 is in Canary (parallel shadow traffic), and v1 remains in Staging.
```

### 2. Running inference

```
skipper > Run a prediction for JPCP using features [1.2, 0.8, 3.4, 0.5, 2.1].

  [tool: predict]

The JPCP Production model returned:

- **prediction**: 142.7
- **model version**: v3
- **alias**: Production

skipper > Run the same input against the Canary version.

  [tool: predict]

The JPCP Canary model (v2) returned:

- **prediction**: 138.4
- **model version**: v2
- **alias**: Canary

The Canary model predicts ~3% lower power consumption for this feature vector. Both results are within normal operating range.
```

### 3. Checking metrics

```
skipper > How many inference requests have each model received?

  [tool: get_metrics]

Request counts from Prometheus:

- jpcp (status=success): 4,821
- jpcp (status=error): 3
- mcbound (status=success): 1,204
- mack (status=success): 89

JPCP has served the most traffic. The 3 errors on JPCP are worth investigating — check Ray Serve logs for details.

skipper > What is the average prediction latency over the last 5 minutes?

  [tool: get_metrics]

Average prediction latency (5-minute window):

- jpcp: 18 ms
- mcbound: 22 ms
- mack: 31 ms

All three models are well within acceptable latency bounds. MACK's higher latency likely reflects its XGBoost tree traversal on larger embeddings.
```

### 4. Triggering a retrain

```
skipper > Retrain the JPCP model on PM100Dataset using dummy data.

  [tool: trigger_retrain]

Retraining has been triggered:

- **model**: JPCP
- **dataset**: PM100Dataset
- **dummy data**: yes
- **flow_run_id**: f3a9c21b-4d0e-4c12-a8f1-7b3e592d1a40

You can poll the run state at: GET http://localhost:18002/retrain/f3a9c21b-4d0e-4c12-a8f1-7b3e592d1a40

skipper > What happens if I retrain with real Zenodo data?

To use real data, I would call trigger_retrain with is_dummy=False. That causes the pipeline to download the full PM100 dataset from Zenodo (several hundred MB) rather than generating a small synthetic array. Use it for production retrains; keep is_dummy=True for smoke tests and CI.
```

Note: `trigger_retrain` requires `CONTROL_PLANE_TOKEN` to be set. If it is missing, the tool returns:
```
Error: CONTROL_PLANE_TOKEN is not set. Cannot trigger retraining.
```

### 5. Generating a full report

```
skipper > Generate a full platform status report.

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
AGENT_MODEL=hermes3:70b make skipper      # best tool calling
AGENT_MODEL=llama3.1:70b make skipper     # best Llama quality
AGENT_MODEL=qwen3-coder:30b make skipper  # strong reasoning
```

To use the **Kapa** tunnel instead (16 models, via Monte Cimone SSH, port 11437):

```bash
ollama-tunnel start kapa
AGENT_OLLAMA_URL=http://localhost:11437 make skipper
```

**Local Ollama (laptop):** pull the model first, then override the URL:

```bash
ollama pull llama3.1
AGENT_OLLAMA_URL=http://localhost:11434 AGENT_MODEL=llama3.1 make skipper
```

## Available Tools

The agent exposes **45 tools across 10 groups**. The LLM selects the appropriate tool(s) automatically based on your prompt.

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
| **docs/knowledge** | `search_docs`, `read_doc`, `list_docs`, `get_howto` | Search the repo's `docs/` directory, read a specific doc file, list all available docs, or look up a how-to answer grounded in the documentation. `search_docs` takes a whole question: the literal phrase is tried first, and on a miss the query degrades to its terms (stop-words dropped, hyphenated compounds split), ranked by how many distinct terms each file matches. |
| **platform_ops** | `compare_model_versions`, `get_model_lineage`, `get_drift_status`, `get_input_drift_status`, `query_audit_log`, `set_traffic_split`, `promote_model`, `trigger_auto_retrain`, `validate_model_serving`, `get_platform_summary`, `diagnose_platform` | Operational observability and lifecycle control (Phase 19/21/22). Compare metric/param deltas between versions; trace the pipeline→dataset→model lineage; read prediction-drift and input-embedding-drift status (CRITICAL/WARNING/OK); query the platform audit log; set per-alias traffic percentages; metric-gate a promotion; fire drift-based auto-retrains (cooldown-aware); smoke-test a model against a latency SLA; and produce a quick `get_platform_summary` or a full prioritized `diagnose_platform` report. `set_traffic_split`, `promote_model`, and `trigger_auto_retrain` are write tools requiring confirmation. |

## Slash Commands

Type a slash command at the `skipper >` prompt instead of a question:

| Command | Description |
|---|---|
| `/help` | Show all slash commands |
| `/tools` | Print every registered tool name |
| `/new` | Start a fresh conversation thread (new `thread_id`) |
| `/resume <id>` | Resume a previously saved thread by its `thread_id` |
| `/threads` | List all saved thread IDs in the SQLite checkpoint store |
| `/history [n]` | Show the last `n` messages in the current thread (default 10) |
| `/export [file]` | Export the current thread to a Markdown file |
| `/grep <pattern>` | Search conversation history for matching text |
| `/watch <secs> <query>` | Repeat a query every N seconds until `Ctrl+C` (polling loop) |
| `/model <name>` | Switch the LLM model for the current session (rebuilds the graph immediately) |
| `/report` | Ask the agent to generate a full platform status report |
| `/exit` (or `/quit`) | Quit the agent REPL |

## Write Confirmation

Write and destructive tools pause before acting. When the agent is about to perform an action such as triggering a retrain, approving a model, starting/stopping a service, or creating a scaffold, the CLI prints a one-line summary and prompts:

```
[confirm] trigger_retrain: Retrain JPCP on PM100Dataset (dummy=True)
Proceed? [y/N]
```

Type `y` or `yes` to proceed (accepted: `y/yes/ok/okay/approve/confirm/true/1`); anything else cancels and returns `Cancelled — no action taken.`. The confirmation gate is implemented via LangGraph's `interrupt()` mechanism and the SQLite checkpointer, so the same gate works transparently for the WebSocket server (it emits an `{"type": "interrupt", "payload": ...}` event and resumes with a `Command`) and for any future API or UI caller that implements the LangGraph interrupt protocol.

The **14 write tools**: `trigger_retrain`, `approve_model`, `reject_model`, `reload_models`, `modelzoo_sync`, `modelzoo_set_config`, `start_service`, `stop_service`, `restart_service`, `scaffold_create`, `set_traffic_split`, `promote_model`, `trigger_auto_retrain`, `record_procedure` (durable memory write).

### What a mutating tool guarantees

Every mutating MCP tool follows one contract, and the part worth knowing is what happens when
something downstream is broken:

| | Guarantee |
|---|---|
| **Exposure** | Mutating tools are not registered at all unless `EXAMLOPS_MCP_ALLOW_WRITES` is truthy. With writes off the surface is read-only — 46 tools, none mutating. |
| **Policy** | Each call is checked against the `agent_write` policy. `require_approval` counts as *denied* for an agent: there is no human at the tool-call boundary. |
| **Audit** | A successful write leaves an `audit_events` row (`source=mcp`). |
| **Audit failure** | The tool still reports `ok: true` — the action happened, and saying otherwise would send the caller to retry something already done — but the reply carries an `audit_warning` so an unaudited governance write is never silent. |
| **Refusal** | When the target service refuses, the tool returns `ok: false` with the *service's own reason*, not just a status code — an agent asked to retrain an unknown dataset is told which datasets exist, so it can correct itself instead of guessing. |

```json
{"ok": true, "cluster": "lxp", "state": "ACTIVE",
 "audit_warning": "action succeeded but was not audited: audit chain unavailable"}
```

`tests/unit/test_mcp_write_audit_contract.py` holds the first four rows for every mutating
tool; `tests/unit/test_cli_client.py` holds the fifth, which is shared with the `exa` CLI
because both go through the same HTTP client.

```json
{"ok": false, "status": 400,
 "error": "HTTP 400 from http://control-plane:8002/retrain: Dataset 'NotADataset' not supported by JPCP. Supported: ['PM100Dataset', 'FDataDataset']"}
```


## Short-Term Memory (conversations)

Conversations are stored in a SQLite database (`AGENT_DB`, default `./agent_memory.db`) using LangGraph's `SqliteSaver` checkpointer. Each session is identified by a `thread_id` (auto-generated as `cli-<8 hex chars>` on startup).

- Resume a past session: `/resume <id>`
- List all saved sessions: `/threads`
- Start fresh: `/new` (old sessions remain on disk)

## Long-Term Memory (Phase 25)

> **Check before you rely on it:** `curl -s localhost:18004/api/info | jq .memory` — `active: false` means no long-term memory is in play. The default `AGENT_EMBED_BACKEND=ollama` needs a reachable Ollama; the offline alternative is `AGENT_EMBED_BACKEND=sentence-transformers` with `AGENT_EMBED_MODEL=all-MiniLM-L6-v2` and `AGENT_EMBED_DIMS=384` — all three together, because the dims must match the model or the store refuses to build.

Beyond per-conversation history, Skipper has **cross-session long-term memory** — it learns operational procedures, remembers past incidents, and retains operator preferences. It is LangGraph-native, fully self-hosted, and **additive**: if the store or the embedding backend is unavailable, the agent simply runs with short-term memory only. See ADR 0033 / 0034 and `design/architecture-skipper-memory.md`.

**How it works**

- A LangGraph `SqliteStore` (backed by `sqlite-vec`) in its own `skipper_memory.db` (separate from `platform.db` and the checkpointer), with **local embeddings** — Ollama `nomic-embed-text` by default, or `sentence-transformers` fully offline. No cloud, no external service.
- Four memory kinds by namespace: **procedural** (`proc` — reusable ops procedures, the highest-value kind), **episodic** (`episode` — past incidents), **preference** (`pref` — per-operator), **KB** (`kb` — stable tribal knowledge).
- Memory holds the agent's *experience* + preferences + stable facts only. Current platform state (model versions, drift, cost, approvals, audit rows) is **always queried live and pointed to, never copied** — memory can go stale; the platform DB is the source of truth. Incidents store foreign-key ids into `platform_db`, not row copies.

**The 7-tier memory stack (next-gen, ADRs 0101/0104/0105/0106)** — the four kinds above are tier T1; the full stack:

| Tier | What it stores | Enabled by |
|---|---|---|
| **T0 Working** | per-thread conversation (checkpointer) | always |
| **T1 Experience** | proc / episode / pref / kb (above) | `AGENT_MEMORY_ENABLED` + embeddings |
| **T2 Knowledge / docs-RAG** | chunked+embedded `docs/**` + ADRs → grounded "how do I…?" answers with citations (`search_knowledge`); reuses `examlops.vector_store`. Ingest: `make skipper-knowledge-ingest`. Degrades to ripgrep. | `AGENT_KNOWLEDGE_ENABLED` |
| **T3 Monitoring / baseline** | recorded "what's normal" (drift/input/cost/SLO) as pointers, not copies (`recall_baseline`) + an auto-fed incident timeline from `skipper-watch` | always (best-effort) |
| **T4 Outcome** | per-turn tool telemetry → real `tool_success_rate` (feeds T-reinforcement) | `AGENT_INSTRUMENT_ENABLED` |
| **T5 Consolidation** | recurring incidents → review-gated candidate procedures; failing-tool procedures deprecated. `make skipper-consolidate`. | `python -m skipper.consolidate` |
| **X Tenant scoping** | namespaces prefixed by project (`EXAMLOPS_PROJECT`) + a shared bucket, authz-gated | `AGENT_MEMORY_TENANT_SCOPED` (off) |

**Tools** (present only when the store is enabled)

| Tool | Purpose | Gated? |
|---|---|---|
| `recall_memory(query, kind)` | Semantic search over a memory kind before planning | no |
| `remember_preference(topic, value)` | Save an operator preference | no (low-risk) |
| `record_procedure(task_class, steps, ...)` | Save a reusable procedure learned from a successful run | **yes** (confirmation) |

**Governance (SM3, ADR 0034)**

- Every memory mutation is written to `platform_db.audit_events` (`source=agent-memory`, actor, action, digest) — inspect with `exa audit --source agent-memory`.
- Durable procedure writes are **confirmation-gated** (HITL) unless `AGENT_MEMORY_REQUIRE_CONFIRM=false`.
- A red-team invariant guarantees a poisoned memory cannot cause an unsafe action: every dangerous tool stays confirmation-gated regardless of memory content.
- **Enumerate / export / erase** memory (GDPR) — deletions cascade and are audited; the immutable audit log is a separate store, untouched by erasure:

```bash
exa agent memory stats                          # counts per kind, and which store file
exa agent memory list proc                      # list procedures
exa agent memory list pref --scope alice        # everything attached to one operator
exa agent memory export --out memory-backup.json
exa agent memory delete pref --scope alice      # erase alice's preferences (audited)
```

`exa agent memory` is the surface ADR 0034 specified. It imports the agent package lazily,
so it works wherever the agent is installed and says so plainly where it is not (set
`EXAMLOPS_AGENT_DIR` if the agent lives outside this repo). Because erasure is
irreversible, `--json` on its own is **not** taken as consent the way it is for other
mutating commands — `exa --json agent memory delete` refuses unless you also pass `--yes`.

The same operations are also reachable without the CLI, which is what the agent container
uses:

```bash
make skipper-memory ARGS=stats
# or, from platform/services/agent/:
python -m skipper.memory_admin stats
```

**Tutorial:** `docs/tutorials/skipper-memory.md`. **Env vars:** see the memory rows in `docs/reference/env-vars.md`.

## HTTP Server & Web UI

Besides the CLI, the agent ships a FastAPI server (`agent_server.py`) that serves the same ReAct graph over HTTP — useful for embedding the agent in a browser or driving it programmatically.

```bash
python platform/services/agent/agent_server.py     # binds 0.0.0.0:18004 (AGENT_SERVER_PORT)
# or:
uvicorn skipper.server:app --port 18004
```

| Surface | Path | Description |
|---|---|---|
| Web chat UI | `GET /` | Embedded HTML chat interface (`chat_html.py`). |
| Backend info | `GET /api/info` | Live backend `{ok, type, model}` from `check_backend()`, plus `memory` — the configured embedding backend/model/dims/db **and `active`**, read from the compiled graph's store. `enabled: true` with `active: false` means the embedding backend is unreachable and the agent is running on short-term memory only. |
| Thread list | `GET /api/threads` | All saved thread IDs in the checkpoint store. |
| Thread history | `GET /api/threads/{thread_id}/history` | Messages for a given thread. |
| Streaming chat | `WS /ws/chat/{thread_id}` | WebSocket chat. Server streams `{"type": "token"}` chunks, `{"type": "tool", "name": ...}` events, `{"type": "interrupt", "payload": ...}` for the write-confirmation gate, and `{"type": "done"}` to end a turn. |
| Chat completions | `POST /v1/chat/completions` | OpenAI-compatible bridge (SSE when `stream=true`, JSON otherwise) consumed by the [kube-q](https://github.com/MSKazemi/kube_q) `kq` client. Conversation state is keyed by the `X-Session-ID` header → LangGraph `thread_id`. |
| Health | `GET /healthz` | Liveness probe for the bridge (`{"status": "ok"}`). |

The WebSocket honours the same write-confirmation gate as the CLI: on an `interrupt` event the client replies with an affirmative/negative decision, which the server feeds back into the graph as a LangGraph `Command` to resume or cancel the pending write.

### kube-q (`kq`) terminal client

The `POST /v1/chat/completions` + `GET /healthz` bridge lets the general-purpose
[kube-q](https://github.com/MSKazemi/kube_q) client (`kq`) drive the ExaMLOps
agent — bringing session history, full-text search, conversation branching,
token/cost tracking, and HITL approvals to the terminal, **without forking**.

`kq` is a declared dependency, not a suggestion: install it with the **`chat` extra**, which is
also part of `[dev]`, so a development checkout has a working `exa chat` after `make install-dev`.

```bash
uv pip install 'examlops[chat]'   # the kq client (kube-q, unforked from PyPI)

make skipper-server          # run the agent + bridge (port 18004)
exa chat                     # ← the normal way in: resolves the agent URL from the CLI's config
exa -c lxp chat              #   …so another environment needs no exported variable
exa chat -- --resume last    #   anything after `--` goes straight through to kq

make skipper-chat            # equivalent, but pinned to localhost
kq --url http://localhost:18004 --query "which models are in production?" --output plain
```

`exa chat` is a launcher, deliberately — ExaMLOps adapts *to* `kq` through the bridge rather than
writing a second REPL, so session history, search, branching, HITL approval and cost accounting all
arrive for the cost of resolving one URL. What it adds over `make skipper-chat` is the CLI's own
configuration: context, agent URL, and `AGENT_API_KEY` forwarding. It launches `kq`; it does not
bundle it, and if the client is absent it says which command installs it.

HITL: write tools trip a LangGraph `interrupt()`; `kq` shows an approval panel and
switches its prompt to `HITL>`. `/approve` and `/deny` are relayed to the graph as
`Command(resume=…)`. Set `AGENT_API_KEY` on the server to require a bearer token
**on `/v1/chat/completions`** (pass it to `kq --api-key` / `KUBE_Q_API_KEY`). It does not gate the
WebSocket chat or the thread-history endpoints, and `AGENT_SERVER_HOST` defaults to `0.0.0.0` — so
a key alone does not make the agent safe to expose; bind loopback and tunnel. See
`platform/services/agent/kube-q/README.md` for the profile and full workflow.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | unset | API key for the Azure OpenAI / AI Foundry backend. When set together with `AZURE_OPENAI_ENDPOINT`, this backend is preferred over Claude and Ollama. |
| `AZURE_OPENAI_ENDPOINT` | unset | Foundry **v1** project endpoint base URL, e.g. `https://<resource>.services.ai.azure.com/openai/v1/` (OpenAI-compatible). |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-5.5` | Deployment name shown in Foundry, used as the model id. |
| `ANTHROPIC_API_KEY` | unset | API key for the Claude backend. Used when Azure is not configured. |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | Claude model id (adaptive thinking enabled, `max_tokens=16000`). |
| `AGENT_MODEL` | `llama3.1:8b` | Ollama model name (fallback backend). Must support tool/function calling. Set in `.env`. |
| `AGENT_OLLAMA_URL` | `http://localhost:11436` | Ollama server base URL. `11436` for ollama-tunnel Omega; `11434` for local `ollama serve`. |
| `AGENT_OLLAMA_KEEP_ALIVE` | `30m` | Pins the Ollama model in memory between turns (avoids 30–60 s reloads on CPU-only servers). |
| `AGENT_OLLAMA_REASONING` | `false` | `false` disables thinking models' extra reasoning tokens (snappier); `true` forces it on; `default`/`none` leaves the model default. |
| `AGENT_SERVER_PORT` | `18004` | Port for the HTTP/WebSocket chat server (`agent_server.py`). |
| `AGENT_API_KEY` | unset | Optional bearer token gating `POST /v1/chat/completions` (the kube-q bridge). Unset ⇒ open (local dev). When set, clients must send `Authorization: Bearer <key>` (e.g. `kq --api-key <key>`). |
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
| Startup banner shows the Azure backend but every request fails | `AZURE_OPENAI_API_KEY` is revoked, rotated, or belongs to a different Foundry resource | Confirm with `check_backend()` — it now returns `ok: false` on `401`/`403`. Regenerate the key in the Azure AI Foundry portal (Keys and Endpoint) and update `AZURE_OPENAI_API_KEY`. A quick manual check: `curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $AZURE_OPENAI_API_KEY" "$AZURE_OPENAI_ENDPOINT/models"` — `200` is good, `401` is the key. |
| Ollama tunnel unit is `active (running)` but nothing listens on the port | The tunnel uses `ExitOnForwardFailure=yes`, so a dead remote makes it exit and systemd restart it in a loop — the unit looks healthy between respawns | Check the port, not the unit: `ss -ltnp \| grep 11436`. If the remote Ollama host is retired, the tunnel cannot succeed; point `AGENT_OLLAMA_URL` at a live host or use another backend. |
| `Error: Ollama is not running at http://localhost:11434` | Using local Ollama URL but `ollama serve` is not running | Run `ollama serve`, or switch to the tunnel: `AGENT_OLLAMA_URL=http://localhost:11436 make skipper`. |
| `Error: Cannot reach MLflow at http://localhost:15000 — ...` in a tool response | MLflow container not running | Run `make stack-up` or `exa status` to check which services are up. |
| `Error: Cannot reach Ray Serve at http://localhost:18001 — ...` | Ray Serve not started | Run `make stack-up` or `exa stack up --service ray-serving`. |
| `Error: CONTROL_PLANE_TOKEN is not set. Cannot trigger retraining.` | `CONTROL_PLANE_TOKEN` env var is absent | Add `CONTROL_PLANE_TOKEN=<token>` to `.env`, then re-run `make skipper`. |
| Agent answers questions without calling tools | Chosen model does not support tool calling well | Use `llama3.1:8b` (default) or `hermes3:8b`. Avoid embedding-only models like `nomic-embed-text`. |
| Agent calls a tool but returns confusing output | LLM hallucinated an argument (e.g. wrong feature count) | Rephrase with explicit values: `"run inference on JPCP with features [1.2, 0.8, 3.4, 0.5, 2.1]"`. |
| `httpx.ReadTimeout` in tool output | Service is slow to respond (e.g. MLflow cold start) | Wait for the service to become healthy (`exa status`). The tool timeout is 10 s. |

## Measuring answer quality (`exa eval operator-qa`)

The agent is meant to answer "any kind of question about ExaMLOps". Whether it *does* is a
measurement, not an opinion — so there is a fixed set of 30 questions a new operator actually
asks in their first week, spanning orientation, training, the registry, serving, drift,
governance, HPC and cost.

```bash
exa eval operator-qa                        # ask all 30, print the pass rate
exa eval operator-qa --category serving     # just one area
exa eval operator-qa --out ./qa.jsonl       # keep the answers
exa --json eval operator-qa                 # machine-readable, for CI
```

Grading is **deterministic**: each question declares what any correct answer must name (for
example, an answer about splitting traffic has to mention `exa serve traffic`). No judge model is
involved, which means a run costs one call per question instead of two, the score cannot drift as
a judge model changes, and the suite needs no judge calibration — which, under
[ADR 0111](judge-calibration.md), an LLM judge would need before it were allowed to gate anything.

Read the expectations as **necessary, not sufficient**: naming `exa drift status` does not prove
the answer was good, but failing to name it proves it was not. The suite catches regressions and
blind spots; it does not certify quality.

Two properties matter when you read a result:

- **An unreachable agent exits non-zero** with the transport error, rather than reporting a score
  of zero. An outage and a bad agent are different findings and must not look alike.
- **An empty answer never passes.** A dead backend returning empty strings scores 0, not a
  vacuous pass.

The question set lives in `examlops.evaluation.operator_qa`. Every `exa …` command it expects is
checked against the live CLI tree by `tests/unit/test_operator_qa.py`, so the set cannot start
asserting a command that does not exist — an expectation like that would fail against any agent,
however good, and read as the agent's fault.

To feed the answers into the persisted eval store, pass the JSONL on to `exa eval run`:

```bash
exa eval operator-qa --out ./qa.jsonl
exa eval run operator-qa --items ./qa.jsonl --model skipper
```
