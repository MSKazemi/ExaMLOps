---
title: Services and their jobs
description: Every service ExaMLOps runs — what each one does, the port it listens on, what it talks to, what state it keeps and how to operate it.
hide:
  - navigation
---

# Services and their jobs

ExaMLOps runs as 30 cooperating services, stores and external dependencies. Twenty of them are
Docker Compose services: `make stack-up` starts the core ten, `make monitoring-up` six
observability services, `make jupyter-up` JupyterHub and `make seanerbus-up` the bus bridge; the
backup sidecar and vLLM are opt-in profiles (`docker compose --profile backup|vllm up`). The rest
are processes, stores and external systems they work with. This page lists
each one: what it does, where it listens, who it talks to and how to operate it. The
[system map](index.md) shows how they connect; the tours show them at work.

Ports are the host ports of the local development stack.

| Service | Plane | Port | Its job |
|---|---|---|---|
| Ray Serve MultiModelServer | Serving | 18001, 18265, 18080 | Serves every model alias; hot-reloads when an alias moves |
| Inference pipeline (InferencePipelineIngress -> FeatureTransformer -> ModelRouter) | Serving | 18001 | Validates, batches and routes each request by traffic split |
| vLLM OpenAI-compatible server | Serving | 18011 | OpenAI-compatible LLM server on a GPU (optional profile) |
| Control plane API | Control and training | 18002 | Retrain API, approval queue, ModelZoo sync, event relay |
| Prefect server (orchestrator) | Control and training | 14200 | Stores deployments, schedules and flow-run state |
| Prefect deployment runner + training_flow | Control and training | — | Serves each model's deployment and runs the training flow |
| HPC scheduler adapters (mock / Slurm / Flux) | Control and training | — | Submits training to mock, Slurm or Flux |
| MLflow tracking server + model registry | Registry and storage | 15000 | Tracks runs; model registry with lifecycle aliases |
| MinIO object store | Registry and storage | 19000, 19001 | Object storage for artifacts, projects and datasets |
| MinIO bucket init (one-shot) | Registry and storage | — | Creates the buckets on first start, then exits |
| PostgreSQL (MLflow + Prefect metadata) | Registry and storage | — | Databases for MLflow, Prefect and the dashboard |
| Shared platform datastore (platform.db) | Registry and storage | — | Shared platform state: drift, traffic, audit, jobs, projects |
| Backup sidecar | Registry and storage | — | Scheduled backup of databases and buckets (optional profile) |
| SeanerBUS bridge | Integration | 18003 | Connects the site message bus to serving and retraining |
| SeanerBUS message bus (external) | Integration | 5398 | The site's message bus (external system) |
| Event outbox relay (NovaFabric backbone) | Integration | — | Publishes outbox events at least once |
| Dashboard (FastAPI BFF + React SPA) | People and agents | 18099 | Web consoles over the same code paths as the CLI |
| Scoped Docker socket proxy | People and agents | — | Gives the dashboard the containers API only — no images, exec, volumes or networks |
| Skipper agent server (web UI + OpenAI-compatible bridge) | People and agents | 18004 | Plain-English operations agent with confirmed writes |
| Skipper-watch monitoring daemon | People and agents | — | LLM-free watcher for drift and budget breaches |
| ExaMLOps MCP server | People and agents | 8765 | Platform tools for other agents; read-only unless allowed |
| JupyterHub (+ spawned JupyterLab) | People and agents | 18888 | Per-user notebooks and project workbenches (optional profile) |
| Ollama LLM backend (external) | People and agents | 11436 | Local LLM and embeddings for the agent (external) |
| Prometheus | Observability | 19090 | Scrapes metrics and evaluates alert rules |
| Alertmanager | Observability | 19093 | Groups and routes alerts to paging and chat |
| Grafana | Observability | 13000 | Dashboards over metrics, logs and traces |
| Loki | Observability | 13100 | Stores container logs for 7 days |
| Promtail | Observability | — | Ships every container's logs to Loki |
| Grafana Tempo | Observability | 13200 | Stores traces for 48 hours when tracing is on |
| OpenTelemetry Collector (production config, not in compose) | Observability | — | Production trace pipeline (configuration only) |

!!! note "Libraries, not services"
    The model gateway and the inference gateway are Python libraries used inside other
    processes, and the event relay runs as a thread in the control plane. The Helm chart deploys
    the control plane, dashboard and agent; Postgres and object storage are expected to be
    provided by the cluster.

## Serving

Answer predictions.

### Ray Serve MultiModelServer

**Port:** 18001, 18265, 18080  
**Built on:** Ray Serve + FastAPI ingress, python:3.12-slim (serving/ray_serving/Dockerfile); 18001 API, 18265 Ray dashboard, 18080 Prometheus metrics

**What it does**

- Pre-loads a hot set of (model, alias) for Production/Canary/Staging from MLflow at start
- Serves POST /predict/{model} with optional alias or raw version (raw versions in an LRU of 8)
- Polls MLflow every 60s and hot-reloads any model whose alias version moved; also POST /reload and /reload/{model}
- Runs predictions under a 30s hard timeout on a 4-worker pool and recycles the pool if predicts hang
- Mirrors traffic to a shadow alias off the request thread (bounded 16 in-flight, drops when full) and writes shadow_results
- Exports request/latency/hot-set/reload metrics; /ready for liveness, /health returns 503 when hot set is empty

**Talks to:** MLflow tracking server + model registry (HTTP); MinIO object store (S3); Shared platform datastore (platform.db) (SQL); Grafana Tempo (OTLP gRPC)

**Keeps:** in-memory hot set + version LRU

**Operate:** `make stack-up (service: ray-serving); exa serve check | exa serve reload | exa serve infer-check`

**Guide:** [ray serve](../components/ray-serve.md)

### Inference pipeline (InferencePipelineIngress -> FeatureTransformer -> ModelRouter)

**Port:** 18001  
**Built on:** Ray Serve deployment graph on route prefix /infer-pipeline (same Ray cluster), num_cpus=0 per actor

**What it does**

- Ingress validates payloads on POST /infer-pipeline/infer (422 on schema errors)
- FeatureTransformer micro-batches requests up to 32 or 50 ms and checks 384-dim embedding + num_nodes
- ModelRouter applies weighted canary/production traffic splits read from traffic_rules (30s TTL cache)
- Forwards to MultiModelServer POST /predict/{model} with 2 retries on transport errors
- Exposes GET/POST /traffic-rules and persists splits to platform-db

**Talks to:** Ray Serve MultiModelServer (HTTP); Shared platform datastore (platform.db) (SQL)

**Keeps:** traffic_rules (platform-db)

**Operate:** `starts with ray-serving; exa serve traffic JPCP --production 90 --canary 10`

**Guide:** [interfaces](../guides/interfaces.md)

### vLLM OpenAI-compatible server

**Port:** 18011  
**Built on:** vllm/vllm-openai:latest, NVIDIA GPU reservation; compose profile vllm (host port EXAMLOPS_VLLM_HOST_PORT default 18011 -> 8000)

**What it does**

- Serves an OpenAI-compatible LLM endpoint for EXAMLOPS_VLLM_MODEL
- Uses engine flags rendered by engines.to_vllm_args() (same renderer as the Slurm template and KServe manifest)
- Caches Hugging Face weights in the vllm_cache volume
- Exports TTFT / inter-token latency / KV-cache metrics scraped by Prometheus

**Keeps:** volume vllm_cache

**Operate:** `exa serve llm start <model> --launcher compose (or docker compose --profile vllm up -d vllm); HPC: --launcher slurm|flux`

**Guide:** [llm serving engines](../guides/llm-serving-engines.md)

## Control and training

Decide when to retrain and run the training flow.

### Control plane API

**Port:** 18002  
**Built on:** FastAPI + uvicorn under opentelemetry-instrument (platform/services/control_plane/Dockerfile); also a Helm template

**What it does**

- Accepts POST /retrain (bearer token, 20/min rate limit, X-Idempotency-Key) and creates a Prefect flow run behind a circuit breaker (opens after 5 failures, 30s reset)
- Runs the sysadmin approval gate: CI POST /api/changes -> pending_approvals -> POST /approve|/reject/{model} (approve starts retrain); expires stale approvals after 72h
- Receives ModelZoo GitLab/GitHub webhooks and polls the upstream repo every 300s under a leader lease, marking models stale
- Serves model meta/README/images, /status (pings Prefect, MLflow, Ray, dashboard) and /metrics for Prometheus
- Relays the transactional event outbox every 1s on a background thread
- Enforces admission queue caps (4 global / 2 per tenant by default)

**Talks to:** Prefect server (orchestrator) (HTTP (Prefect API)); MLflow tracking server + model registry (HTTP); Ray Serve MultiModelServer (HTTP); Dashboard (FastAPI BFF + React SPA) (HTTP); Shared platform datastore (platform.db) (SQL); Event outbox relay (NovaFabric backbone) (in-process); Grafana Tempo (OTLP gRPC)

**Keeps:** pending_approvals, modelzoo_events, model_freshness, control_plane_commands, admission_queue and event_outbox — in the shared platform.db in the dev stack (`PLATFORM_DB=/state/platform.db`); a standalone deployment uses `$CONTROL_PLANE_DB`, default `/data/approvals.db`

**Operate:** `make control-plane-up ; exa retrain JPCP --dataset PM100Dataset ; exa approvals list|approve|reject`

**Guide:** [control plane](../components/control-plane.md)

### Prefect server (orchestrator)

**Port:** 14200  
**Built on:** prefecthq/prefect:3.6.27-python3.11 (Dockerfile.orchestrator), `prefect server start`

**What it does**

- Stores deployments, schedules (e.g. nightly cron 0 2 * * *) and flow-run state
- Accepts flow-run creation from the control plane (POST /retrain, approvals, ModelZoo auto-retrain)
- Serves the Prefect UI and REST API (/api) on 14200
- Persists metadata in Postgres DB prefect instead of ephemeral SQLite

**Talks to:** PostgreSQL (MLflow + Prefect metadata) (PostgreSQL (asyncpg))

**Keeps:** Postgres DB prefect

**Operate:** `make stack-up (service: orchestrator); UI http://localhost:14200`

**Guide:** [prefect](../components/prefect.md)

### Prefect deployment runner + training_flow

**Port:** internal only  
**Built on:** Python process: pipelines/deploy.py calling flow.serve()/prefect.serve() (not a compose service)

**What it does**

- Registers the training deployments and serves them in-process: one wrapper deployment for all models by default, or one per enabled model YAML (cron, work pool, concurrency limit) with `--registry`
- Runs training_flow: data_extraction -> data_contract_gate -> slurm_submit -> slurm_wait -> result_fetch -> evaluate -> log_mlflow -> promote
- Submits training to the selected scheduler adapter (EXAMLOPS_HPC_SCHEDULER=mock|slurm|flux) and records hpc_jobs rows
- Walks lifecycle rules (Staging/Canary/Production thresholds), sets MLflow aliases and moves the previous Production to Archived
- POSTs /reload/{model} to Ray Serve right after promotion (webhook path)
- Routes runs to per-project MLflow experiments (project/<name>)

**Talks to:** Prefect server (orchestrator) (HTTP (Prefect API)); MLflow tracking server + model registry (HTTP); MinIO object store (S3); HPC scheduler adapters (mock / Slurm / Flux) (in-process Python); Ray Serve MultiModelServer (HTTP); Shared platform datastore (platform.db) (SQL)

**Keeps:** Prefect deployments (in prefect-server)

**Operate:** `exa pipeline deploy [--no-schedule] ; one-off: exa pipeline run --model JPCP --dataset PM100Dataset`

**Guide:** [hpc training workflow](../guides/hpc-training-workflow.md)

### HPC scheduler adapters (mock / Slurm / Flux)

**Port:** internal only  
**Built on:** Python library platform/infra/slurm-adapter (SchedulerAdapter protocol + LocalExecutor/SSHExecutor via paramiko)

**What it does**

- Mock: simulates submission locally, trains inline (sklearn) or runs the script as a subprocess
- Slurm: submits with sbatch, polls squeue then sacct, fetches the StdOut log path
- Flux: submits with `flux batch`, polls `flux jobs` / eventlog
- Caps every scheduler CLI call at 30s, total wait at 24h and 5 consecutive UNKNOWN polls
- Runs commands locally or over SSH (host-key RejectPolicy by default) so a containerised worker can reach a login node
- Discovers nodes/GPUs (SlurmProbe/FluxProbe/NvidiaSmiProbe) for `exa hpc detect/nodes/gpus`

**Talks to:** Prefect deployment runner + training_flow (in-process)

**Keeps:** mock_hpc_jobs/ working dir; hpc_jobs / hpc_nodes / hpc_clusters tables in platform-db

**Operate:** `EXAMLOPS_HPC_SCHEDULER=mock|slurm|flux ; exa hpc detect | exa hpc jobs | exa hpc preflight`

**Guide:** [slurm adapter](../components/slurm-adapter.md)

## Registry and storage

Hold models, artifacts, datasets and platform state.

### MLflow tracking server + model registry

**Port:** 15000  
**Built on:** ghcr.io/mlflow/mlflow:v3.11.1 + psycopg2 + boto3 (Dockerfile.mlflow)

**What it does**

- Records training runs, params and metrics logged by the Prefect training flow
- Holds the model registry with lifecycle aliases (Staging / Canary / Production / Archived)
- Proxies artifact upload/download to MinIO (--artifacts-destination s3://mlflow-artifacts/)
- Answers alias lookups for Ray Serve's hot-set loader and 60s alias poller
- Serves the MLflow UI on 15000

**Talks to:** PostgreSQL (MLflow + Prefect metadata) (PostgreSQL); MinIO object store (S3)

**Keeps:** experiments/runs/registered models in Postgres DB mlflow; artifacts in bucket mlflow-artifacts

**Operate:** `make stack-up (service: mlflow); UI http://localhost:15000`

**Guide:** [mlflow](../components/mlflow.md)

### MinIO object store

**Port:** 19000, 19001  
**Built on:** minio/minio:latest (S3 API :9000 -> 19000, console :9001 -> 19001)

**What it does**

- Serves the S3 API used by MLflow for model artifacts (s3://mlflow-artifacts/)
- Stores per-project storage under s3://examlops-projects/<project>/{artifacts,datasets,cache}/
- Stores dashboard model-doc images in the dashboard-model-docs bucket (created by the dashboard at startup)
- Acts as a dataset backend (minio) for training when no dedicated dataset S3 is configured
- Exposes the MinIO Console UI on 19001

**Keeps:** volume minio_data; bucket mlflow-artifacts; bucket examlops-projects; bucket dashboard-model-docs

**Operate:** `make stack-up (service: minio)`

**Guide:** [interfaces](../guides/interfaces.md)

### MinIO bucket init (one-shot)

**Port:** internal only  
**Built on:** minio/mc:latest, restart: no

**What it does**

- Waits for MinIO to be healthy
- Creates bucket mlflow-artifacts if absent (mc mb --ignore-existing)
- Creates the project-storage bucket ($EXAMLOPS_PROJECTS_BUCKET, default examlops-projects)
- Exits 0; MLflow waits for its successful completion

**Talks to:** MinIO object store (S3 (mc))

**Operate:** `runs automatically on make stack-up`

**Guide:** [project anatomy](../guides/project-anatomy.md)

### PostgreSQL (MLflow + Prefect metadata)

**Port:** internal only  
**Built on:** postgres:15 (Dockerfile.postgres), no host port published

**What it does**

- Hosts the `mlflow` database used as the MLflow backend store
- Hosts the `prefect` database (created by initdb-prefect.sql on first volume init) for Prefect server metadata
- Hosts the dashboard's Alembic-managed tables (dashboard_config, dashboard_audit, model_doc_overrides, model_doc_images) via DATABASE_URL on the mlflow DB
- Health-gated via pg_isready every 5s; MLflow, Prefect, dashboard and backup wait for it
- Optional target for platform state when EXAMLOPS_DB_BACKEND=postgres (separate DSN via EXAMLOPS_POSTGRES_DSN)

**Keeps:** volume postgres_data; DB mlflow; DB prefect

**Operate:** `make stack-up (service: postgres)`

**Guide:** [architecture](../guides/architecture.md)

### Shared platform datastore (platform.db)

**Port:** internal only  
**Built on:** SQLite file (PLATFORM_DB, WAL) by default; Postgres via EXAMLOPS_DB_BACKEND=postgres + EXAMLOPS_POSTGRES_DSN through the examlops.storage seam

**What it does**

- Holds about 130 tables of platform state: audit_events (hash-chained), drift_snapshots/baselines, input_snapshots, traffic_rules, shadow_config/results, hpc_jobs/nodes/clusters, projects, event_outbox, autopilot_runs, virtual_keys, prompt_versions and more
- Shared by CLI, control plane, agent, bridge, dashboard and Ray Serve through a /state bind mount
- Bootstraps its schema once per process
- Coordinates processes: leases/locks, rate windows and idempotency (coord_* tables)

**Keeps:** platform.db (repo root / $EXAMLOPS_STATE_DIR)

**Operate:** `exa audit | exa drift status | exa data retention-prune --days N ; make test-postgres`

**Guide:** [postgres backend](../guides/postgres-backend.md)

### Backup sidecar

**Port:** internal only  
**Built on:** python:3.12-slim + examlops[backup]; entrypoint `exa backup schedule --all`; compose profile backup

**What it does**

- Backs up platform.db (which also holds the control plane's state in the dev stack) and a standalone approvals DB when present (SQLite tier)
- Dumps Postgres databases mlflow and prefect
- Mirrors MinIO buckets (MLflow artifacts + project storage)
- Runs every EXAMLOPS_BACKUP_INTERVAL (default 3600s) and prunes to keep=14
- Replicates off-site when EXAMLOPS_BACKUP_S3_URI is set; skips unavailable tiers without crashing

**Talks to:** PostgreSQL (MLflow + Prefect metadata) (PostgreSQL); MinIO object store (S3); Shared platform datastore (platform.db) (file)

**Keeps:** volume backups_data

**Operate:** `docker compose --profile backup up -d backup ; exa backup ...`

**Guide:** [backup restore](../guides/backup-restore.md)

## Integration

Connect external systems and deliver events.

### SeanerBUS bridge

**Port:** 18003  
**Built on:** Python asyncio + pycapnp (Cap'n Proto over TCP) + httpx; Dockerfile.bridge; compose profile seanerbus

**What it does**

- Registers one req/res inference handler per model seanerbus_uuid (HpcJobV1 -> HpcInferenceResV1) plus retrain (RetrainReqV1) and vector (VectorReqV1) handlers
- Forwards jobs to Ray Serve /infer-pipeline/infer (vector requests go straight to /predict/{model})
- Writes drift snapshots, input-embedding stats (norm/mean/std) and audit events off the event loop
- Tracks per-model rolling error rate (window 50, threshold 0.5, cooldown 300s) and POSTs /retrain to the control plane on drift
- Serves /health, /stats and /metrics on 8003 and reconnects to the bus with exponential backoff (2s to 30s)

**Talks to:** SeanerBUS message bus (external) (Cap'n Proto/TCP :5398); Inference pipeline (InferencePipelineIngress -> FeatureTransformer -> ModelRouter) (HTTP); Ray Serve MultiModelServer (HTTP); Control plane API (HTTP); Shared platform datastore (platform.db) (SQL)

**Operate:** `make seanerbus-up (container) or make seanerbus-bridge-up (bare metal) ; exa seanerbus status`

**Guide:** [seanerbus](../guides/seanerbus.md)

### SeanerBUS message bus (external)

**Port:** 5398  
**Built on:** External system from the sibling seanerbus repo; reached over docker network seanerbus-net

**What it does**

- Carries HPC job / inference / retrain / vector messages between site components and ExaMLOps
- Runs a request generator (reqgen) used by make full-up

**Talks to:** SeanerBUS bridge (Cap'n Proto/TCP)

**Operate:** `cd ../seanerbus && docker compose up -d (external); make full-up`

**Guide:** [seanerbus architecture](../guides/seanerbus-architecture.md)

### Event outbox relay (NovaFabric backbone)

**Port:** internal only  
**Built on:** examlops.events.relay_once: thread inside control plane + `exa events relay` CLI

**What it does**

- Claims unpublished rows from the event_outbox table in batches (default 100)
- Publishes each via EXAMLOPS_EVENT_PUBLISHER: log (default) or Redis Streams; nats/kafka fail loudly and keep the row
- Marks rows published/failed with stable IDs (outbox:<id>) for at-least-once delivery
- Reports pending / published / poison backlog (`exa events stats`)

**Talks to:** Shared platform datastore (platform.db) (SQL)

**Keeps:** event_outbox table

**Operate:** `exa events relay [--loop] ; exa events stats ; in-process in control plane (CONTROL_PLANE_EVENT_RELAY_SECONDS=1)`

**Guide:** [control plane](../guides/control-plane.md)

## People and agents

The ways people and agents operate the platform.

### Dashboard (FastAPI BFF + React SPA)

**Port:** 18099  
**Built on:** FastAPI + Alembic backend; React 19 + Vite + Tailwind frontend built into the same image (node:24-alpine -> python:3.12-slim)

**What it does**

- Serves the React SPA and ~58 API routers (MLOps, facility, LLMOps, FinOps, governance, projects, connections, workbenches)
- Aggregates upstream status in a BFF and streams typed realtime events over SSE (GET /api/v1/stream)
- Starts/stops/restarts containers and tails logs through the scoped Docker socket proxy
- Runs every `exa` command except the CLI-only ones from the CLI Console, in an isolated subprocess (POST /api/v1/cli/runs)
- Proxies copilot questions to Skipper's OpenAI-compatible bridge (POST /api/v1/copilot/ask, propose-only)
- Starts JupyterHub named servers for project workbenches; enforces JWT viewer/admin auth and audits most mutations (approvals are recorded by the control plane instead)

**Talks to:** Control plane API (HTTP); MLflow tracking server + model registry (HTTP (/ajax-api)); Prefect server (orchestrator) (HTTP); Ray Serve MultiModelServer (HTTP); Prometheus (HTTP (PromQL)); Grafana (HTTP/iframe); Loki (HTTP); MinIO object store (S3); Skipper agent server (web UI + OpenAI-compatible bridge) (HTTP (OpenAI-compatible)); Scoped Docker socket proxy (Docker API over TCP 2375); JupyterHub (+ spawned JupyterLab) (HTTP (Hub API)); SeanerBUS bridge (HTTP); PostgreSQL (MLflow + Prefect metadata) (PostgreSQL (asyncpg)); Shared platform datastore (platform.db) (SQL)

**Keeps:** Postgres tables dashboard_config, dashboard_audit, model_doc_overrides, model_doc_images; bucket dashboard-model-docs; platform.db via /state bind mount

**Operate:** `make dashboard-up ; make dashboard-check ; UI http://localhost:18099`

**Guide:** [index](../dashboard/index.md)

### Scoped Docker socket proxy

**Port:** internal only  
**Built on:** tecnativa/docker-socket-proxy:0.2.0, internal only (2375)

**What it does**

- Holds /var/run/docker.sock read-only on behalf of the dashboard
- Opens only the containers API section (list, inspect, logs, and write methods such as start, stop and restart)
- Denies images, exec, volumes, networks, secrets, swarm, build and the rest

**Operate:** `make stack-up (service: docker-socket-proxy)`

**Guide:** [production hardening](../guides/production-hardening.md)

### Skipper agent server (web UI + OpenAI-compatible bridge)

**Port:** 18004  
**Built on:** LangGraph/LangChain ReAct agent behind FastAPI/uvicorn (platform/services/agent/agent_server.py); binds 127.0.0.1:18004

**What it does**

- Serves POST /v1/chat/completions (SSE when stream=true) used by `exa ask` and the dashboard copilot
- Serves a chat web UI at /, a WebSocket chat at /ws/chat/{thread_id}, thread history and memory admin APIs
- Calls platform tools: registry, inference, metrics, training, approvals, modelzoo, services, pipelines, docs, knowledge, platform_ops, finops and the shared MCP tool registry (the default supervisor takes its read tools from it; the single-agent fallback uses it unless `AGENT_USE_MCP_TOOLS=false`)
- Gates mutating tools with a LangGraph interrupt() HITL step and signed, expiring action IDs
- Keeps short-term conversation checkpoints and long-term memory (sqlite-vec store with local embeddings)
- Picks the LLM backend in order Azure OpenAI -> Claude API -> Ollama

**Talks to:** Ollama LLM backend (external) (HTTP); Control plane API (HTTP); MLflow tracking server + model registry (HTTP); Ray Serve MultiModelServer (HTTP); Prometheus (HTTP (PromQL)); Dashboard (FastAPI BFF + React SPA) (HTTP); Shared platform datastore (platform.db) (SQL)

**Keeps:** agent_memory.db (checkpoints); skipper_memory.db (long-term store); skipper_review.db (memory review queue); volume agent_data

**Operate:** `make stack-up (service: agent) or make skipper-server ; CLI: make skipper ; exa ask "..."`

**Guide:** [agent](../guides/agent.md)

### Skipper-watch monitoring daemon

**Port:** internal only  
**Built on:** python -m skipper.watch (--once or --daemon), LLM-free loop

**What it does**

- Scans prediction drift against baseline using a z-score threshold
- Checks platform cost against budget
- On breach, enqueues an outbox event, writes a hash-chained audit event and records an episodic memory
- Holds a cross-process lock so only one daemon is active across replicas

**Talks to:** Shared platform datastore (platform.db) (SQL)

**Operate:** `make skipper-watch [ARGS=--dry-run]`

**Guide:** [agent](../guides/agent.md)

### ExaMLOps MCP server

**Port:** 8765  
**Built on:** FastMCP (optional extra examlops[mcp]); stdio default, HTTP on loopback 127.0.0.1:8765

**What it does**

- Exposes the examlops.mcp tool registry (status, models, drift, traffic, SLO, FinOps, gateway, HPC, projects) to MCP clients
- Publishes resources (examlops://status, models, audit/recent, model/{name}) and prompts (diagnose_drift, promote_safely, platform_triage)
- Registers mutating tools (e.g. trigger_retrain, set_traffic_split, hpc_approve_cluster) only with --allow-writes / EXAMLOPS_MCP_ALLOW_WRITES
- Refuses to bind HTTP to a non-loopback host (no built-in auth)
- Emits an A2A Agent Card via `exa mcp agent-card`

**Talks to:** MLflow tracking server + model registry (HTTP); Control plane API (HTTP); Shared platform datastore (platform.db) (SQL)

**Operate:** `exa mcp serve [--transport http --port 8765] [--allow-writes] ; exa mcp tools`

**Guide:** [agent](../guides/agent.md)

### JupyterHub (+ spawned JupyterLab)

**Port:** 18888  
**Built on:** JupyterHub + DockerSpawner + NativeAuthenticator (Dockerfile.jupyterhub; user image examlops-jupyterlab); compose profile jupyter

**What it does**

- Authenticates users with NativeAuthenticator (open signup off, admin user 'admin')
- Spawns one examlops-jupyterlab container per login on the compose network via DockerSpawner
- Mounts a per-project shared volume (examlops-project-<project>-shared -> /project) on spawn
- Allows up to 10 named servers per user, used as project workbenches
- Registers a 'dashboard' service token with admin:servers scopes so the dashboard can start/stop workbenches

**Talks to:** MinIO object store (S3)

**Keeps:** volume jupyter_hub_data

**Operate:** `make jupyter-up ; make jupyter-add-user USER=<name> HUB_TOKEN=<token>`

**Guide:** [jupyter](../guides/jupyter.md)

### Ollama LLM backend (external)

**Port:** 11436  
**Built on:** External Ollama server; container default http://host.docker.internal:11436 (tunnel); Skipper defaults to 11436, while a stock local `ollama serve` listens on 11434 — set `AGENT_OLLAMA_URL`

**What it does**

- Runs the agent's default chat model (AGENT_MODEL, default llama3.1:8b)
- Provides local embeddings for Skipper memory (nomic-embed-text, 768 dims)

**Operate:** `external; configure AGENT_OLLAMA_URL / AGENT_CONTAINER_OLLAMA_URL`

**Guide:** [agent](../guides/agent.md)

## Observability

Metrics, alerts, logs and traces.

### Prometheus

**Port:** 19090  
**Built on:** prom/prometheus:v2.51.0 (Dockerfile.prometheus); profile monitoring; 7d retention

**What it does**

- Scrapes every 15s: ray-serving:8080, control-plane:8002, seanerbus-bridge:8003, alertmanager, tempo, loki, vllm
- Loads fleet targets (node_exporter/DCGM/vLLM) from file_sd JSON refreshed every 30s, generated by `exa hpc prometheus-sd`
- Evaluates 31 alert rules from alert_rules.yml and sends alerts to Alertmanager
- Stamps external labels cluster/tenant on every series

**Talks to:** Ray Serve MultiModelServer (HTTP scrape); Control plane API (HTTP scrape); SeanerBUS bridge (HTTP scrape); Loki (HTTP scrape); Grafana Tempo (HTTP scrape); vLLM OpenAI-compatible server (HTTP scrape); Alertmanager (HTTP)

**Keeps:** volume prometheus_data

**Operate:** `make monitoring-up ; make alerts-check`

**Guide:** [grafana](../components/grafana.md)

### Alertmanager

**Port:** 19093  
**Built on:** prom/alertmanager:v0.27.0; profile monitoring

**What it does**

- Routes alerts grouped by alertname/cluster/service
- Sends critical alerts to PagerDuty + Slack and warnings to Slack (secrets read from files; missing file = no-op receiver)
- Delivers a heartbeat webhook proving the Prometheus -> Alertmanager path works
- Applies inhibition rules and silences

**Keeps:** volume alertmanager_data

**Operate:** `make monitoring-up`

**Guide:** [grafana](../components/grafana.md)

### Grafana

**Port:** 13000  
**Built on:** grafana/grafana:10.4.0 (Dockerfile.grafana); bound to 127.0.0.1 by default; profile monitoring

**What it does**

- Auto-provisions Prometheus, Loki and Tempo datasources
- Auto-provisions 7 dashboards: overview, online metrics, control plane, drift, approvals, logs, seanerbus
- Allows embedding so the dashboard and Ray dashboard can iframe panels
- Keeps anonymous access off unless GRAFANA_ANONYMOUS_ENABLED=true

**Talks to:** Prometheus (HTTP (PromQL)); Loki (HTTP (LogQL)); Grafana Tempo (HTTP)

**Keeps:** volume grafana_data

**Operate:** `make monitoring-up ; http://localhost:13000`

**Guide:** [grafana](../components/grafana.md)

### Loki

**Port:** 13100  
**Built on:** grafana/loki:2.9.10, filesystem storage; profile monitoring

**What it does**

- Stores container logs pushed by Promtail
- Keeps logs for 168h (7 days) with retention/compaction enabled
- Answers LogQL queries from Grafana and the dashboard

**Keeps:** volume loki_data

**Operate:** `make monitoring-up`

**Guide:** [grafana](../components/grafana.md)

### Promtail

**Port:** internal only  
**Built on:** grafana/promtail:2.9.10; profile monitoring

**What it does**

- Discovers containers through the Docker socket (docker_sd_configs)
- Tails container stdout/stderr with no per-service code change
- Labels streams with project, compose_service, container and stream
- Pushes to Loki /loki/api/v1/push

**Talks to:** Loki (HTTP push)

**Keeps:** volume promtail_data (positions)

**Operate:** `make monitoring-up`

**Guide:** [grafana](../components/grafana.md)

### Grafana Tempo

**Port:** 13200  
**Built on:** grafana/tempo:2.5.0; OTLP receivers 4317 (gRPC) / 4318 (HTTP) on the compose network; profile monitoring

**What it does**

- Receives OTLP traces from control plane, dashboard, ray-serving (OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4317)
- Keeps trace blocks for 48h
- Serves trace queries to Grafana on 3200

**Keeps:** volume tempo_data

**Operate:** `make monitoring-up ; enable export with OTEL_SDK_DISABLED=false`

**Guide:** [grafana](../components/grafana.md)

### OpenTelemetry Collector (production config, not in compose)

**Port:** internal only  
**Built on:** Config file only (otel-collector-config.yml); no compose service or Make target starts it

**What it does**

- Receives OTLP on 4317 (gRPC) / 4318 (HTTP)
- Tail-samples traces: keeps all errors and traces >1000 ms, samples 5% of the rest
- Batches (5s / 1024) with a memory limiter at 80%
- Fans traces to Tempo and metrics to a Prometheus remote-write target

**Talks to:** Grafana Tempo (OTLP)

**Operate:** `not started by the dev stack; config guarded by tests/unit/test_otel_collector_config.py`


## Read more

- [Architecture](../guides/architecture.md)
- [Enterprise installation](../guides/enterprise-installation.md) and [production hardening](../guides/production-hardening.md)
- [Environment variables](../reference/env-vars.md)
