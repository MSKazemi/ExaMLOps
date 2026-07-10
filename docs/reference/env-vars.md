# Environment Variables Reference

All variables that control ExaMLOps behaviour. Unset variables use the defaults shown. Variables marked **required** must be set before starting the relevant service.

---

## Fault Tolerance

Shared knobs for the `examlops.resilience` layer — HTTP timeouts, SQLite lock
handling, and the per-subsystem timeouts/retries. Defaults are safe; override only
to tune for a slow/unreliable environment.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_HTTP_CONNECT_TIMEOUT` | `5.0` | Connect timeout (s) for outbound HTTP via the shared client |
| `EXAMLOPS_HTTP_READ_TIMEOUT` | `10.0` | Read timeout (s) for outbound HTTP via the shared client |
| `EXAMLOPS_HTTP_WRITE_TIMEOUT` | `10.0` | Write timeout (s) for outbound HTTP |
| `EXAMLOPS_HTTP_POOL_TIMEOUT` | `5.0` | Connection-pool acquisition timeout (s) |
| `EXAMLOPS_DB_BUSY_TIMEOUT_MS` | `5000` | SQLite `busy_timeout` (ms) — how long a writer waits for a lock before `database is locked` |
| `RAY_MLFLOW_TIMEOUT` | `10` | Per-call MLflow REST timeout (s) in Ray Serve (`MLFLOW_HTTP_REQUEST_TIMEOUT`) |
| `RAY_MLFLOW_MAX_RETRIES` | `3` | MLflow REST retry count in Ray Serve |
| `RAY_PREDICT_TIMEOUT` | `30` | Hard ceiling (s) on a single `model.predict()`; exceeding it returns HTTP 504 |
| `RAY_PREDICT_WORKERS` | `4` | Thread-pool size backing the predict timeout |
| `RAY_MAX_ONGOING_REQUESTS` | `100` | Max concurrent requests per Ray Serve replica |
| `INFERENCE_ROUTE_RETRIES` | `2` | Transient-error retries on the inference-pipeline → MultiModelServer hop |
| `EXAMLOPS_SLURM_CMD_TIMEOUT` | `30` | Timeout (s) on each `sbatch`/`squeue`/`sacct` call |
| `EXAMLOPS_SLURM_MAX_WAIT_S` | `86400` | Ceiling (s) on `wait_until_complete` before `JobTimeoutError` |
| `EXAMLOPS_SLURM_MAX_UNKNOWN_POLLS` | `5` | Consecutive UNKNOWN/absent polls tolerated before a job is declared lost |
| `EXAMLOPS_TASK_IO_RETRIES` | `3` | Retry count for I/O-bound Prefect tasks (data/MLflow/promote) |
| `EXAMLOPS_TASK_DATA_TIMEOUT_S` | `1800` | `timeout_seconds` for the data-extraction / evaluate tasks |
| `EXAMLOPS_TASK_MLFLOW_TIMEOUT_S` | `600` | `timeout_seconds` for the MLflow logging task |
| `EXAMLOPS_TASK_FETCH_TIMEOUT_S` | `300` | `timeout_seconds` for result-fetch / promote tasks |
| `EXAMLOPS_TASK_SUBMIT_TIMEOUT_S` | `300` | `timeout_seconds` for the Slurm submit task |
| `AGENT_DB` | `./agent_memory.db` | Skipper checkpointer SQLite path (resolved to absolute; WAL + busy_timeout applied) |

Docker Compose memory ceilings (OOM isolation) are also env-overridable:
`RAY_MEM_LIMIT` (`6g`), `RAY_CPUS` (`2.0`), `MLFLOW_MEM_LIMIT` (`2g`),
`ORCHESTRATOR_MEM_LIMIT` (`2g`), `POSTGRES_MEM_LIMIT` (`1g`),
`CONTROL_PLANE_MEM_LIMIT` (`512m`), `DASHBOARD_MEM_LIMIT` (`1g`),
`JUPYTERHUB_MEM_LIMIT` (`2g`), `BRIDGE_MEM_LIMIT` (`1g`).

---

## CLI, agent surface & config contexts

Variables read by the `exa` CLI's next-gen surface (MCP/A2A, `exa ask`, config contexts).

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_MCP_ALLOW_WRITES` | unset (read-only) | When truthy (`1`/`true`/`yes`/`on`), `exa mcp serve` registers mutating tools (e.g. `trigger_retrain`). Equivalent to `exa mcp serve --allow-writes`. |
| `AGENT_URL` | `http://localhost:18004` | Skipper agent OpenAI-compatible bridge that `exa ask` calls. Also settable via `exa config set agent <url>`. |
| `AGENT_API_KEY` | unset | Bearer token sent by `exa ask` when the agent bridge is token-gated. |
| `EXAMLOPS_CONTEXT` | unset | Selects a named config context for the invocation (same effect as `exa -c <name>` / `exa config use <name>`, without persisting). Resolution order: env var → active context → config file → default. |

The `exa` CLI also honours the standard endpoint/token vars (`CONTROL_PLANE_URL`, `MLFLOW_TRACKING_URI`, `RAY_SERVE_URL`, `PREFECT_API_URL`, `DASHBOARD_URL`, `CONTROL_PLANE_TOKEN`), which a config context or `~/.config/examlops/config.toml` can override. Inspect the effective values and their source with `exa env`.

---

## Pipeline

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_SLURM_MODE` | `mock` | `mock` runs training inline (safe for dev/Docker); `slurm` submits via `sbatch` |
| `EXAMLOPS_SLURM_PARTITION` | unset | Slurm partition name (real Slurm mode only) |
| `EXAMLOPS_SLURM_TIME` | `2:00:00` | Wall-clock time limit for Slurm jobs |
| `EXAMLOPS_SLURM_NODES` | `1` | Number of nodes per Slurm job |
| `EXAMLOPS_SLURM_MEM` | `16G` | Memory per node |
| `EXAMLOPS_SLURM_CPUS` | `4` | CPUs per task |
| `GPU_COST_PER_HOUR` | `2.50` | USD cost per GPU-hour used by `exa models cost --record` |
| `CPU_COST_PER_HOUR` | `0.05` | USD cost per CPU-hour (used for Flux/CPU-only jobs in `exa models cost --record`) |

### HPC scheduler adapter (Phase 23)

The scheduler backend and its transport are independent. `EXAMLOPS_SLURM_MODE` and every
`EXAMLOPS_SLURM_*` key keep working; `EXAMLOPS_HPC_*` takes precedence when set.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_HPC_SCHEDULER` | derived from `EXAMLOPS_SLURM_MODE` | `mock` \| `slurm` \| `flux` — which scheduler backend to submit to |
| `EXAMLOPS_HPC_TRANSPORT` | `ssh` if `EXAMLOPS_HPC_SSH_HOST` set, else `local` | `local` (subprocess/shared-FS) \| `ssh` (paramiko + SFTP) |
| `EXAMLOPS_HPC_SSH_HOST` | unset | SSH host of the cluster login node (e.g. `lxp-cpu01`) |
| `EXAMLOPS_HPC_SSH_USER` | unset | SSH username (falls back to agent/default) |
| `EXAMLOPS_HPC_SSH_KEY` | unset | Path to the SSH private key (else agent/default keys) |
| `EXAMLOPS_HPC_SSH_PORT` | `22` | SSH port |
| `EXAMLOPS_HPC_REMOTE_REPO` | repo root | Path to the deployed ExaMLOps repo on the cluster |
| `EXAMLOPS_HPC_REMOTE_PYTHON` | `<remote_repo>/.venv/bin/python` | Remote interpreter that runs the training script |
| `EXAMLOPS_HPC_REMOTE_WORKDIR` | adapter working dir | Root for per-job dirs on the cluster |
| `EXAMLOPS_HPC_GPUS` | unset | GPUs per job (`0`/unset ⇒ no GPU flag; lxp Flux has 0 enrolled) |
| `EXAMLOPS_HPC_ACCOUNT` | unset | Account/bank (`--account` for Slurm, `--bank` for flux-accounting) |
| `EXAMLOPS_HPC_QOS` | unset | QoS/queue (`--qos` for Slurm, `--queue` for Flux) |
| `EXAMLOPS_HPC_CONSTRAINT` | unset | Node constraint (`--constraint` / `--requires`) |
| `EXAMLOPS_HPC_NTASKS` | `1` | Tasks per job |
| `EXAMLOPS_HPC_PARTITION` / `_TIME` / `_NODES` / `_MEM` / `_CPUS` | fall back to `EXAMLOPS_SLURM_*` | Scheduler-neutral resource mirrors |
| `MLFLOW_TRACKING_URI` | `http://localhost:15000` | MLflow server endpoint for logging and model loading |
| `MLFLOW_S3_ENDPOINT_URL` | `http://localhost:19000` | MinIO S3-compatible endpoint for MLflow artifact storage |
| `AWS_ACCESS_KEY_ID` | `minioadmin` | MinIO access key |
| `AWS_SECRET_ACCESS_KEY` | `minioadmin` | MinIO secret key |
| `PREFECT_API_URL` | `http://localhost:14200/api` | Prefect server API endpoint |
| `PREFECT_DEPLOYMENT_NAME` | `examlops_scheduled_training/nightly` | Prefect deployment slug used by `POST /retrain` |

---

## Dataset Backends (Phase 1)

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_DATA_BUCKET` | `examlops-data` | S3/MinIO bucket consulted by `MinIOBackend` |

Selection is per-pipeline-run via `--backend` CLI flag or `backend_name` Prefect flow parameter. Omitting falls back to the default Zenodo flow.

---

## Ray Serve (Phase 3)

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_STAGE` | `Production` | Default MLflow alias when no per-request alias or version is specified |
| `RAY_NUM_REPLICAS` | `2` | Ray Serve replicas per deployment |
| `RAY_SERVE_PORT` | `8001` | Ray Serve internal HTTP port (host-exposed as `18001`) |
| `RAY_PRELOAD_ALIASES` | `Production,Canary,Staging` | Comma-separated MLflow aliases pre-loaded into the hot set at startup and on reload |
| `RAY_VERSION_CACHE_SIZE` | `8` | LRU cache size for raw-version (`/predict` with `version=`) lookups |
| `RAY_RELOAD_POLL_SECONDS` | `60` | Background MLflow alias-poll interval in seconds; `0` disables polling |
| `RAY_SERVE_RELOAD_URL` | unset | Ray Serve URL for the Prefect promotion webhook (`POST /reload/{model_id}`); unset disables the webhook |
| `RAY_METRICS_EXPORT_PORT` | `8080` | Prometheus metrics export port used by Ray |

---

## Control Plane (Phase 4 + 11 + 12)

| Variable | Default | Purpose |
|---|---|---|
| `CONTROL_PLANE_PORT` | `8002` | HTTP port for the retrain API (host-exposed as `18002`) |
| `CONTROL_PLANE_TOKEN` | **required** | Bearer token for `POST /retrain` and approval endpoints; unset causes write endpoints to return 503 |
| `CONTROL_PLANE_URL` | `http://control-plane:8002` | Control plane URL used by the dataplane simulator and CI notify script |
| `CONTROL_PLANE_DB` | `/data/approvals.db` | SQLite file path for the Phase 11 pending approval store; falls back to `./approvals.db` if `/data/` is not writable |

### ModelZoo Integration (Phase 12)

| Variable | Default | Purpose |
|---|---|---|
| `MODELZOO_WEBHOOK_SECRET` | unset | Shared secret for GitLab/GitHub push webhook verification. GitLab: plain equality check (`X-Gitlab-Token` header). GitHub: HMAC-SHA256 (`X-Hub-Signature-256`). Unset disables signature verification (not recommended for production). |
| `MODELZOO_AUTO_RETRAIN` | `false` | When `true`, a confirmed push event automatically triggers `POST /retrain` for every registered model. Takes effect at runtime — changes via `PUT /modelzoo/config` are picked up immediately without restart. |
| `MODELZOO_POLL_SECONDS` | `300` | Background GitLab poller interval in seconds. The poller checks for new commits on `MODELZOO_WATCH_BRANCH` and records them as push events. Set to `0` to disable polling entirely. Changes via `PUT /modelzoo/config` take effect immediately. |
| `MODELZOO_WATCH_BRANCH` | `main` | Branch that both the poller and webhooks watch. Push events for other branches are silently ignored. |
| `GITLAB_PROJECT_ID` | unset | GitLab project ID (integer) or namespace/path (e.g. `my-group/seanergys-modelzoo`) used by the background poller. Required for polling; webhooks do not need it. |
| `GITLAB_TOKEN` | unset | GitLab Personal Access Token or Project Access Token with `read_repository` scope. Required for the background poller. Webhooks do not use this. |
| `AI_PROD_GITLAB_PROJECT_ID` | unset | ai-production GitLab project ID (e.g. `88`). When set together with `AI_PROD_PIPELINE_TRIGGER_TOKEN`, the control plane automatically triggers an ai-production CI pipeline run whenever a new modelzoo commit is detected (webhook or poller path). |
| `AI_PROD_PIPELINE_TRIGGER_TOKEN` | unset | GitLab pipeline trigger token for the ai-production project. Created under ai-production → Settings → CI/CD → Pipeline trigger tokens. Causes `_trigger_ci_pipeline()` to fire `POST /api/v4/projects/<AI_PROD_GITLAB_PROJECT_ID>/trigger/pipeline` on every new modelzoo commit. |

---

## Dashboard (Phase 6)

| Variable | Default | Purpose |
|---|---|---|
| `DASHBOARD_VIEWER_PASSWORD` | **required** | Viewer-role login password |
| `DASHBOARD_ADMIN_PASSWORD` | **required** | Admin-role login password |
| `DASHBOARD_JWT_SECRET` | **required** | HS256 signing secret for JWT tokens (minimum 32 characters) |
| `DASHBOARD_SECRET_KEY` | **required** | Fernet key (base64-encoded, 44 characters) for secrets-at-rest encryption |
| `DASHBOARD_JWT_TTL_HOURS` | `12` | JWT token expiry in hours |
| `DASHBOARD_PORT` | `8099` | Dashboard HTTP port (host-exposed as `18099`) |
| `GITLAB_URL` | `https://gitlab.com` | GitLab base URL for ModelZoo repository integration |
| `GITLAB_TOKEN` | unset | GitLab PAT with `read_repository` scope; used by the Datasets and Models pages to list files from the modelzoo repo. Also used as a last-resort fallback for the "Run CI Pipeline" button when no DB or env trigger token is configured. |
| `GITLAB_PROJECT_ID` | unset | ModelZoo project ID; used by the Datasets/Models pages. Also used by the "Run CI Pipeline" fallback path when `AI_PROD_GITLAB_PROJECT_ID` is absent. |
| `GITLAB_BRANCH` | `main` | Branch consulted by the ModelZoo stats router |
| `DATABASE_URL` | `postgresql+asyncpg://mlops:mlops@postgres/mlflow` | Postgres connection string for dashboard metadata |
| `AI_PROD_PIPELINE_TRIGGER_TOKEN` | unset | GitLab pipeline trigger token for the ai-production project. When set, "Run CI Pipeline" on the Datasets page uses this token to fire the ai-production pipeline (which runs `test:modelzoo`). Takes priority over the `GITLAB_TOKEN` PAT fallback. |
| `AI_PROD_GITLAB_PROJECT_ID` | unset | ai-production project ID (e.g. `88`). Used together with `AI_PROD_PIPELINE_TRIGGER_TOKEN` for the "Run CI Pipeline" fallback. |
| `EXAMLOPS_DOCS_ROOT` | auto-detected | Project root for the docs tree endpoint; set explicitly in Docker if needed |
| `SEANERBUS_BRIDGE_STATUS_URL` | `http://localhost:8003` | URL the dashboard backend uses to probe the bridge `/health` and `/stats`. In Docker set to `http://seanerbus-bridge:8003` (the docker-compose default); in bare-metal dev keep the default `http://localhost:8003`. The DB config key `seanerbus_bridge_status_url` overrides this env var. |

Generate the required secrets:
```bash
# JWT secret
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Fernet key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## SeanerBUS Bridge

The bridge (`platform/clients/seanerbus_bridge.py`) connects to the real SeanerBUS (from the seanerbus repo, TCP :5398) in `reqres` mode. Per-model handler UUIDs are read automatically from `pipelines/models/*.yaml` (`seanerbus_uuid` field). No topic UUIDs need to be set.

| Variable | Default | Purpose |
|---|---|---|
| `SEANERBUS_HOST` | `seanerbus-reqgen` | Where the bridge finds SeanerBUS. Container on `seanerbus-net` → its container name (default). **Bare-metal on the host** → `host.docker.internal` (needs the bridge's `extra_hosts: host.docker.internal:host-gateway`) or the Docker bridge gateway IP (e.g. `172.19.0.1`). Not `localhost` (that's the container, not the host). For `make stack-up` this is interpolated from the **root** `.env`. See `docs/guides/seanerbus-sim.md`. |
| `SEANERBUS_PORT` | `5398` | SeanerBUS TCP port. Bare-metal SeanerBUS must bind `0.0.0.0:<port>`, not `127.0.0.1`. |
| `SEANERBUS_MODE` | `reqres` | Bridge mode — always `reqres` with the real SeanerBUS |
| `SEANERBUS_RETRAIN_UUID` | unset | Optional: req/res UUID for `RetrainReqV1 → RetrainResV1` |
| `SEANERBUS_VECTOR_UUID` | unset | Optional: req/res UUID for `VectorReqV1 → VectorResV1` |
| `SEANERBUS_DEFAULT_MODEL` | `JPCP` | Fallback model name when `HpcJobV1.modelName` is empty |
| `SEANERBUS_DEFAULT_ALIAS` | `Production` | Fallback MLflow alias when `HpcJobV1.alias` is empty |
| `RAY_SERVE_URL` | `http://localhost:18001` | Ray Serve URL used by the bridge to forward inference requests |
| `MODELS_YAML_DIR` | unset | Directory of per-model YAMLs for `ModelSchemaRegistry`; defaults to `pipelines/models/` |
| `DRIFT_WINDOW` | `50` | Rolling-window length for the bridge drift tracker |
| `DRIFT_THRESHOLD` | `0.5` | Per-model error-rate threshold that fires `/trigger-retrain` |
| `DRIFT_COOLDOWN` | `300` | Minimum seconds between drift-triggered retrains per model |

---

## Management Agent

The LangGraph ReAct agent (`platform/services/agent/`) launched via `make skipper` (CLI) or `agent_server.py` (HTTP/WebSocket, port 18004). The LLM backend is chosen by which keys are set, in order: **Azure Foundry → Claude → Ollama**. When using `ollama-tunnel` (Omega server, port 11436), start the tunnel first. Vars are set in `.env` and sourced automatically.

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | unset | Azure OpenAI / AI Foundry key. With `AZURE_OPENAI_ENDPOINT` set, this backend is preferred over Claude/Ollama. |
| `AZURE_OPENAI_ENDPOINT` | unset | Foundry v1 endpoint base URL (`https://<resource>.services.ai.azure.com/openai/v1/`, OpenAI-compatible). |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-5.4-mini` | Foundry deployment name, used as the model id. |
| `ANTHROPIC_API_KEY` | unset | Claude backend key. Used when Azure is not configured. |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | Claude model id (adaptive thinking, `max_tokens=16000`). |
| `AGENT_MODEL` | `llama3.1:8b` | Ollama model name (fallback). Via ollama-tunnel: any model from the Omega/Kapa list. Must support tool calling. |
| `AGENT_OLLAMA_URL` | `http://localhost:11436` | Ollama server base URL. Omega tunnel default. Use `localhost:11434` for a local `ollama serve`. |
| `AGENT_OLLAMA_KEEP_ALIVE` | `30m` | Pins the Ollama model in memory between turns (avoids reload latency on CPU-only servers). |
| `AGENT_OLLAMA_REASONING` | `false` | Disable (`false`) / force (`true`) / leave-default (`default`) thinking models' extra reasoning tokens. |
| `AGENT_SERVER_PORT` | `18004` | Port for the HTTP/WebSocket chat server (`agent_server.py`). |
| `AGENT_API_KEY` | unset | Optional bearer token gating the OpenAI-compatible `POST /v1/chat/completions` bridge consumed by the kube-q (`kq`) client. Unset ⇒ open (local dev); when set, send `Authorization: Bearer <key>` (e.g. `kq --api-key <key>` / `KUBE_Q_API_KEY`). |
| `PROMETHEUS_URL` | `http://localhost:19090` | Prometheus endpoint for the `get_metrics` tool |
| `RAY_SERVE_URL` | `http://localhost:18001` | Ray Serve endpoint for the `predict` / inference tools |
| `AGENT_DB` | `./agent_memory.db` | SQLite file backing the LangGraph checkpointer — conversations persist here and are resumable by thread id (`/resume`) |
| `AGENT_DOCS_ROOT` | `<repo>/docs` | Root directory the docs/knowledge tools (`search_docs`, `read_doc`, `list_docs`) search and read |
| `AGENT_HTTP_TIMEOUT` | `10.0` | Per-request timeout (seconds) for the agent's HTTP tool calls |
| `DASHBOARD_URL` | `http://localhost:18099` | Dashboard base URL used by the service-control and pipeline/scaffold tools |
| `DASHBOARD_ADMIN_PASSWORD` | unset | Admin password the agent's `DashboardClient` logs in with; unset ⇒ the dashboard-backed tools (service control, pipelines, scaffold) return an actionable error and the rest of the agent is unaffected |

### Long-term memory (Phase 25)

Cross-session memory for the agent (procedures / incidents / preferences / KB). All additive — if the store or embedding backend is unavailable the agent runs with short-term memory only. See `docs/guides/agent.md` (Long-Term Memory) and ADR 0033/0034.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_MEMORY_ENABLED` | `true` | Master switch for long-term memory. `false` ⇒ short-term (conversation) memory only. |
| `AGENT_MEMORY_DB` | `./skipper_memory.db` | SQLite file for the long-term `SqliteStore` (separate from `AGENT_DB` and `platform.db`). |
| `AGENT_EMBED_BACKEND` | `ollama` | Local embedding backend: `ollama` (via `AGENT_OLLAMA_URL`) or `sentence-transformers` (fully offline, in-process). No cloud. |
| `AGENT_EMBED_MODEL` | `nomic-embed-text` | Embedding model name. Must match `AGENT_EMBED_DIMS`. |
| `AGENT_EMBED_DIMS` | `768` | Embedding dimension. Must match the model (nomic-embed-text=768, bge-m3=1024, all-MiniLM-L6-v2=384). Mismatch ⇒ logged + long-term memory disabled. |
| `AGENT_SUMMARIZE_ENABLED` | `false` | Enable the context-trimming `pre_model_hook` for long threads (langchain-core `trim_messages`). |
| `AGENT_MAX_CONTEXT_TOKENS` | `12000` | Token budget for the trim hook when enabled. |
| `AGENT_MEMORY_REQUIRE_CONFIRM` | `true` | Confirmation-gate durable `record_procedure` writes (HITL). |
| `AGENT_MEMORY_AUDIT` | `true` | Write every memory mutation to `platform_db.audit_events`. |
| `AGENT_ACTOR` | `$EXAMLOPS_ACTOR`/`$USER`/`operator` | Actor recorded in preference memory + memory audit events. |

`MLFLOW_TRACKING_URI`, `CONTROL_PLANE_URL`, and `CONTROL_PLANE_TOKEN` are shared with the pipeline / control plane sections above — set them once and the agent picks them up automatically. The agent exposes 45 base tools across 10 groups (plus 3 memory tools when the store is enabled); the **14 write/destructive tools** pause for operator confirmation (`Proceed? [y/N]`) before acting.

---

## Internal Service URLs (Docker Stack)

Used by services to reach each other inside the Docker Compose network (internal ports, not host-exposed ports):

| Variable | Default | Purpose |
|---|---|---|
| `MLFLOW_URL` | `http://mlflow:5000` | Internal MLflow URL |
| `PREFECT_URL` | `http://prefect:4200` | Internal Prefect URL |
| `RAY_SERVE_URL` | `http://ray-serve:8001` | Internal Ray Serve URL |
| `RAY_DASHBOARD_URL` | `http://ray-serve:8265` | Internal Ray Dashboard URL |
| `PROMETHEUS_URL` | `http://prometheus:9090` | Internal Prometheus URL |
| `GRAFANA_URL` | `http://grafana:3000` | Internal Grafana URL |

---

## Public URLs (Browser-Facing)

Override the URLs sent to the browser when the dashboard is accessed from a remote machine or behind a proxy:

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_MLFLOW_URL` | `http://localhost:15000` | Clickable MLflow URL returned to the browser |
| `PUBLIC_PREFECT_URL` | `http://localhost:14200` | Clickable Prefect URL returned to the browser |
| `PUBLIC_RAY_DASHBOARD_URL` | `http://localhost:18265` | Clickable Ray Dashboard URL returned to the browser |
| `PUBLIC_PROMETHEUS_URL` | `http://localhost:19090` | Clickable Prometheus URL returned to the browser |
| `PUBLIC_GRAFANA_URL` | `http://localhost:13000` | Clickable Grafana URL returned to the browser |

Example for remote server access (lxp-cpu01 at 23.109.46.77):
```bash
PUBLIC_MLFLOW_URL=http://23.109.46.77:15000
PUBLIC_PREFECT_URL=http://23.109.46.77:14200
PUBLIC_RAY_DASHBOARD_URL=http://23.109.46.77:18265
PUBLIC_PROMETHEUS_URL=http://23.109.46.77:19090
PUBLIC_GRAFANA_URL=http://23.109.46.77:13000
```

---

## JupyterHub

| Variable | Default | Purpose |
|---|---|---|
| `JUPYTERHUB_PORT` | `8888` | JupyterHub internal HTTP port (host-exposed as `18888`) |
| `DOCKER_NETWORK_NAME` | `examlops_default` | Docker network that spawned user containers join — must match the Compose project network |

---

## YAML Model Registry (Phase 9)

| Variable | Default | Purpose |
|---|---|---|
| `RAY_MODELS_DIR` | unset | Phase 14 — directory of per-model YAML files (e.g., `pipelines/models`); takes precedence over `RAY_REGISTRY_PATH` for per-model alias control |
| `RAY_REGISTRY_PATH` | unset | Legacy path to `model_registry.yaml`; superseded by `RAY_MODELS_DIR` (Phase 14). Still works as fallback when `RAY_MODELS_DIR` is unset |
| `RAY_REGISTRY_ENV` | unset | Env overlay name (e.g., `prod`) loaded alongside `RAY_REGISTRY_PATH`; maps to `pipelines/envs/<ENV>.yaml` |

---

## Monitoring — Scraped Services (Phase 13)

Prometheus scrapes two services. No env vars required — targets are hardcoded in `platform/infra/docker-compose/prometheus.yml`.

| Job | Target (internal) | Metrics path | Notes |
|---|---|---|---|
| `ray_serve` | `ray-serving:8080` | `/metrics` | Ray built-in metrics export (`RAY_METRICS_EXPORT_PORT=8080`) |
| `control_plane` | `control-plane:8002` | `/metrics` | Phase 13 approval gate metrics (no auth required) |
| `alertmanager` | `alertmanager:9093` | `/metrics` | Phase 17 — Alertmanager self-monitoring |
| `tempo` | `tempo:3200` | `/metrics` | Phase 17 — Tempo self-monitoring |

## Observability — Tracing (Phase 17)

OpenTelemetry distributed tracing, exported to Grafana Tempo. Off by default; enable by setting
`OTEL_SDK_DISABLED=false` and bringing up the monitoring profile.

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_SDK_DISABLED` | `true` (in compose) | Master switch; `true` makes all instrumentation a no-op. Set `false` to enable tracing |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://tempo:4317` | OTLP gRPC collector endpoint (Tempo) |
| `OTEL_SERVICE_NAME` | per service | Span service name (`control-plane` / `dashboard` / `ray-serving`) |

The control plane and dashboard are auto-instrumented via the `opentelemetry-instrument` launcher
(no app code changes); the Ray Serve inference pipeline uses the `examlops.observability` helper.

---

## Setting Variables

**`.env` file at the project root (recommended for local dev):**

```bash
# Required secrets
DASHBOARD_VIEWER_PASSWORD=viewer-password
DASHBOARD_ADMIN_PASSWORD=admin-password
DASHBOARD_JWT_SECRET=<output of secrets.token_urlsafe(32)>
DASHBOARD_SECRET_KEY=<output of Fernet.generate_key()>
CONTROL_PLANE_TOKEN=my-retrain-token

# Overrides (if not using defaults)
MLFLOW_TRACKING_URI=http://localhost:15000
PREFECT_API_URL=http://localhost:14200/api
EXAMLOPS_SLURM_MODE=mock
```

Docker Compose picks up `.env` automatically. The pipeline and services also read it when running outside Docker.

**On the command line:**
```bash
EXAMLOPS_SLURM_MODE=slurm exa pipeline run --dummy
BACKEND=minio exa pipeline run --model JPCP
```

**Inside Docker Compose** (`platform/infra/docker-compose/docker-compose.yml`):
```yaml
environment:
  MLFLOW_URL: http://mlflow:5000
  CONTROL_PLANE_TOKEN: ${CONTROL_PLANE_TOKEN}
```
