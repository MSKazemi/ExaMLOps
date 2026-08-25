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

## Platform datastore, coordination & governance

The variables every process that touches platform state needs. They are read across the CLI, the
dashboard, the control plane and the pipelines, so an inconsistent setting between two processes is
the usual cause of "it works from the CLI but not in the dashboard".

| Variable | Default | Purpose |
|---|---|---|
| `PLATFORM_DB` | `./platform.db` (`/repo/platform.db` inside the containers) | Path to the shared SQLite datastore — audit, drift, traffic, promotion, costs, projects and ~100 other tables. Override with a `tmp_path` in tests. |
| `EXAMLOPS_DB_BACKEND` | `sqlite` | Datastore engine. `postgres` routes every `platform_db` helper through the translating connection in `examlops.storage.pg`. Must be set on **every** process that touches platform state, or that process silently keeps using `platform.db`. |
| `EXAMLOPS_POSTGRES_DSN` | unset | Connection string used when the backend is `postgres`. |
| `EXAMLOPS_POSTGRES_SCHEMA` | `public` | Scopes an instance to one schema — how the test suite isolates, and how two instances share one server. |
| `EXAMLOPS_POSTGRES_POOL` | `1` (on) | Per-`(dsn, schema)` connection pooling. `0` opts out. |
| `EXAMLOPS_POSTGRES_CONNECT_TIMEOUT` | `2.0` | Seconds to wait for the datastore before declaring it unreachable. Bounds one probe per process, before the pool is built, so a downed datastore costs a moment rather than the driver's 30 s default on every command. Unix-socket and multi-host DSNs skip the probe. |
| `EXAMLOPS_POSTGRES_UNREACHABLE_TTL` | `5.0` | How long an "unreachable" verdict is cached, so one command probes once. It expires, so a long-lived process recovers when the server returns. |
| `EXAMLOPS_COORDINATOR` | `db` | Cross-process coordination backend: `db` (via the datastore) or `redis` (cross-host HA). |
| `EXAMLOPS_EVENT_PUBLISHER` | `log` | Event publisher: `log` (dependency-free) or `redis` (Redis Streams). `nats` and `kafka` are reserved fail-loud placeholders, not operational backends. Drain the outbox with `exa events relay`. |
| `EXAMLOPS_NATS_URL` | unset | Reserved NATS endpoint. Selecting the NATS publisher fails loudly until its transport is implemented. |
| `EXAMLOPS_KAFKA_BROKERS` | unset | Reserved comma-separated Kafka brokers. Selecting the Kafka publisher fails loudly until its transport is implemented. |
| `EXAMLOPS_REDIS_PREFIX` | `examlops:coord` | Namespace prefix for Redis coordination keys; use a distinct value per installation sharing a Redis database. |
| `EXAMLOPS_REDIS_EVENT_STREAM` | `examlops.events` | Redis Stream name used by the implemented event publisher. |
| `EXAMLOPS_REDIS_EVENT_MAXLEN` | `100000` | Approximate maximum Redis Stream length; must be a positive integer. |
| `EXAMLOPS_EVENT_MAX_ATTEMPTS` | `5` | Maximum automatic outbox publication attempts. Exhausted rows remain as poison evidence for operator inspection. |
| `EXAMLOPS_CONFIG` | `~/.config/examlops/config.toml` | Overrides the CLI config path so containers and CI can pin a config and tests run hermetically. |
| `EXAMLOPS_PROJECT` | active CLI context or `default` | Explicit project/tenant context for CLI commands and trusted server-side agent scoping. Request callers cannot override an authenticated agent tenant with payload data. |
| `EXAMLOPS_USECASE_DIR` | `usecases/seanergy` | Selects the use-case pack (ADR 0094). The platform core names no concrete model or dataset; this is how it reaches content. |
| `EXAMLOPS_AGENT_DIR` | derived from the repo | Where the Skipper package lives for explicit `exa agent memory … --local` recovery. Normal memory administration uses `AGENT_URL`. |
| `EXAMLOPS_PROJECTS_BUCKET` | `examlops-projects` | MinIO bucket holding per-project `artifacts/`, `datasets/`, `cache/`. |

### Governance & secrets

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_SECRETS_KEYS` | unset | Secrets KEK keyring, `key_id:fernet_key,…`. |
| `EXAMLOPS_SECRETS_ACTIVE_KEY` | first key | Which key new writes are encrypted with. Rotate online with `exa secrets rewrap`; `DASHBOARD_SECRET_KEY` is a decrypt-only legacy fallback. |
| `EXAMLOPS_ACTOR` | `$USER` | Actor stamped into every audit event. Set it in CI and in scripts, or the log records whichever account the runner happens to use. |
| `EXAMLOPS_AUDIT_WORM_PATH` | unset | External append-only WORM anchor for audit checkpoints — a local file in dev, an S3 Object-Lock path or Rekor log in production. Verify with `exa audit verify-worm`. |
| `EXAMLOPS_OIDC_ISSUER` | unset | OIDC issuer for RS256 access-token validation. Unset ⇒ SSO off (single-tenant). See also `EXAMLOPS_OIDC_AUDIENCE` / `_JWKS` / `_TENANT_CLAIM` / `_SUBJECT_CLAIM`. |
| `CONTROL_PLANE_ALLOWED_HOSTS` | `*` | Comma-separated allow-list for the control plane's Host header. `*` is a development default. |
| `EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS` | unset (off) | `1` opts into adding unknown SSH host keys. Off means `RejectPolicy` — an unknown host fails rather than being trusted. |

### Autopilot

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_AUTOPILOT_ENABLED` | unset (disabled) | Master kill-switch for the self-driving loop. Falls back to the `autopilot_config` table when unset; `exa autopilot enable/disable` sets it persistently. |
| `EXAMLOPS_AUTOPILOT_LEASE_TTL` | `900` | TTL (s) of the distributed cycle lease — only one cycle runs at a time, and a crashed holder's lease expires. |
| `EXAMLOPS_AUTOPILOT_MAX_RETRAINS` | `1` | Per-cycle cap on retrains, so a storm of drift cannot become a storm of jobs. |

### Data versioning & fleet

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_LAKEFS_REPO` | the dataset name | lakeFS repository backing dataset revisions. |
| `EXAMLOPS_LAKEFS_REF` | `main` | lakeFS ref revisions are recorded against. |
| `EXAMLOPS_HPC_CAPACITY_TTL` | `30` | TTL (s) of the cached per-cluster fleet capacity rollup. |

### Event backbone & admission control

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_EVENT_BROKER_URL` | unset | Broker URL for the selected publisher. The implemented Redis path also accepts `EXAMLOPS_REDIS_URL`; NATS/Kafka variables are reserved for future backends. |
| `EXAMLOPS_ADMISSION_MAX_RUNNING` | `4` | Global cap on concurrently running admitted jobs. |
| `EXAMLOPS_ADMISSION_PER_TENANT` | `2` | Per-tenant fair-share cap. |

### Postgres pool & OIDC claim names

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_POSTGRES_POOL_MIN` / `EXAMLOPS_POSTGRES_POOL_MAX` | `1` / `10` | Pool size per `(dsn, schema)` per process. `EXAMLOPS_POSTGRES_POOL=0` opts out of pooling entirely. |
| `EXAMLOPS_OIDC_JWKS` | unset | JWKS for RS256 validation — a URL, or inline JSON. |
| `EXAMLOPS_OIDC_TENANT_CLAIM` / `EXAMLOPS_OIDC_SUBJECT_CLAIM` | provider defaults | Which claim carries the tenant / the subject. |

---

## Backup & restore

`exa backup` writes a **tiered bundle**: each tier is independent, and a tier whose tooling or
endpoint is unavailable is recorded as `skipped` rather than failing the cycle. The Compose
`backup` profile runs the same command as a sidecar (`docker compose --profile backup up -d backup`).

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_BACKUP_TIERS` | `sqlite,config` | Which tiers a cycle runs. Full set: `sqlite,config,postgres,objects` — what `exa backup schedule --all` selects. **On a Postgres deployment the default is not enough**: the sqlite tier deliberately *skips* `platform.db` (its state is in Postgres), so without the `postgres` tier a bundle carries no platform state. |
| `EXAMLOPS_BACKUP_DIR` | `./backups` | Where bundles are written (`/backups` in the sidecar). |
| `EXAMLOPS_BACKUP_INTERVAL` | `3600` | Seconds between scheduled cycles. |
| `EXAMLOPS_BACKUP_RETAIN` | `keep=14` | Retention spec — `keep=N`, `days=N`, or both comma-separated. |
| `EXAMLOPS_BACKUP_ON_PROMOTE` | `false` | Take a bundle before a model promotion. |
| `EXAMLOPS_BACKUP_PG_DBS` | `mlflow,prefect` | Databases the postgres tier dumps. Add the platform database here when the datastore is Postgres. |
| `EXAMLOPS_BACKUP_BUCKETS` | unset | Comma-separated MinIO/S3 buckets for the objects tier. Unset ⇒ the tier is skipped. |

Off-site replication (the bundle is tar+gzipped and uploaded):

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_BACKUP_S3_URI` | unset | Off-site target, e.g. `s3://bucket/prefix`. **Setting it is enough** — the scheduler pushes whenever a URI is configured; `--push` only forces it. Unset ⇒ bundles never leave the host. |
| `EXAMLOPS_BACKUP_S3_ENDPOINT` | falls back to `MLFLOW_S3_ENDPOINT_URL` | S3 endpoint for the off-site target. |
| `EXAMLOPS_BACKUP_S3_ACCESS_KEY` / `EXAMLOPS_BACKUP_S3_SECRET` | fall back to `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Credentials for the off-site target, so it can be a different account from the platform's own object store. |

The postgres tier shells out to `pg_dump`, so it reads the standard libpq variables rather than a
DSN. These are **PostgreSQL's contract, not ExaMLOps's** — the defaults below are what the tier
falls back to, not what libpq would do on its own:

| Variable | Default | Purpose |
|---|---|---|
| `PGHOST` / `PGPORT` | `localhost` / `5432` | Server the dump connects to. |
| `PGUSER` / `PGPASSWORD` | `POSTGRES_USER` / `POSTGRES_PASSWORD`, then `mlops` | Credentials. The `POSTGRES_*` fallback exists so the sidecar can reuse the Compose stack's own values. |

---

## Control-plane safety limits

Defaults are in the module header of `platform/services/control_plane/app.py`.

| Variable | Default | Purpose |
|---|---|---|
| `RETRAIN_RATE_LIMIT_PER_MIN` | `20` | Token-bucket capacity for `POST /retrain`; the refill rate is this per minute. Exceeding it returns 429 with a retry hint. |
| `APPROVAL_EXPIRY_HOURS` | `72` | Pending approvals older than this are swept to `expired`. `0` disables the sweep, leaving stale entries pending indefinitely. |
| `IDEMPOTENCY_TTL_SECONDS` | `300` | How long a response is replayed for a repeated idempotency key. |
| `NOTIFICATION_WEBHOOK_URL` | unset | Webhook the CI notifier posts model changes to. Unset ⇒ the notification is skipped, not failed. Set it as a **masked** CI variable. |
| `RETRAIN_DATASET` | `FDataDataset` | Dataset the CI retrain-on-merge job passes. |
| `RETRAIN_DUMMY` | `false` | `true` makes that job run a dummy (no real training). |

---

## Agent (Skipper) — server, checkpointing & review queue

!!! warning "Authenticate deliberate network exposure"

    The server defaults to loopback. If you bind another interface, configure distinct principals
    with `AGENT_API_KEYS_JSON` (or the legacy `AGENT_API_KEY`) and put the endpoint behind TLS.
    Memory administration always requires a configured credential. The built-in browser exchanges
    its key for an HttpOnly, same-site session cookie.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_SERVER_HOST` | `127.0.0.1` | Interface the agent server binds. See the warning above. |
| `AGENT_CHECKPOINT_BACKEND` | `sqlite` | Conversation checkpoint store. `postgres` uses `AGENT_POSTGRES_DSN`, then `DATABASE_URL` — and **selecting `postgres` with neither set falls back to `sqlite`** rather than failing, so check the startup log if checkpoints are not where you expect. |
| `AGENT_POSTGRES_DSN` | falls back to `DATABASE_URL` | DSN for the Postgres checkpointer. |
| `AGENT_GRAPH_TIMEOUT` | `300.0` | Seconds one LangGraph run may take before it is abandoned. |
| `AGENT_STREAM_IDLE_TIMEOUT` | `120.0` | Seconds of silence on a streaming response before it is closed. |
| `AGENT_TURN_LEASE_SECONDS` | `AGENT_GRAPH_TIMEOUT + 30` (minimum) | Renewable cross-replica lease for one active turn per authenticated session. A smaller configured value is raised to the timeout-derived minimum. |
| `AGENT_ACTION_SIGNING_KEY` | falls back to `CONTROL_PLANE_TOKEN` or configured agent credentials | Server-only key for signed, owner-bound HITL action IDs and browser sessions. Configure an independent random key in production. |
| `AGENT_ACTION_TTL_SECONDS` | `600` | Lifetime of a typed approve/deny action ID. Expired IDs cannot resume a mutation. |
| `AGENT_BROWSER_SESSION_TTL_SECONDS` | `28800` | Lifetime in seconds of the HttpOnly browser session cookie issued after agent authentication. |
| `AGENT_MEMORY_REVIEW_QUEUE` | `false` | Queue memory writes for human review instead of applying them. |
| `AGENT_MEMORY_REVIEW_DB` | `./skipper_review.db` | Where that review queue lives — separate from `AGENT_MEMORY_DB`. |

---

## Local LLM serving (vLLM)

`exa serve llm` starts an OpenAI-compatible vLLM server, either through Compose or on an HPC
allocation. `EXAMLOPS_VLLM_MODEL` and `EXAMLOPS_VLLM_ARGS` are **set by the launcher** for the
server process — you name the weights with `--hf-model`, not by exporting them (the raw
`docker compose --profile vllm up vllm` path is the exception; see the
[VLM serving guide](../guides/vlm-serving.md)).

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_VLLM_BASE_URL` | unset | Point the client at an already-running server. Unset, and with no `engine.base_url` in the model YAML, the engine falls back to `EchoEngine` rather than failing. |
| `EXAMLOPS_VLLM_PORT` | `8000` | Port the server listens on inside its container/allocation. |
| `EXAMLOPS_VLLM_HOST_PORT` | `18011` | Port published on the host by the Compose launcher. |
| `EXAMLOPS_VLLM_API_KEY` | empty | Bearer token the server requires, if any. |
| `EXAMLOPS_VLLM_IMAGE` | `docker://vllm/vllm-openai:latest` | Image for the container/Apptainer launcher. |
| `EXAMLOPS_VLLM_LAUNCHER` | auto | Force a launcher (`compose` / `hpc`) instead of detecting one. |
| `EXAMLOPS_VLLM_WORK_DIR` | unset | Scratch directory for launcher state — read per call, so it can be changed without reloading the module. |
| `EXAMLOPS_VLLM_MODULES` | unset | `module load` lines to emit into the HPC launch script. |
| `EXAMLOPS_VLLM_RAY_PORT` | `6379` | Ray head port for multi-node tensor parallelism. |
| `EXAMLOPS_VLLM_TIMEOUT` | `120` | Seconds to wait for the server to become ready. |
| `EXAMLOPS_VLLM_CONNECT_TIMEOUT` | `10` | Per-request connect timeout. |


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

## Programmable MLOps — providers & policy (Phase 37)

Select which calculation provider a domain uses without editing code (ADR 0077). The generic pattern is `EXAMLOPS_<DOMAIN>_PROVIDER`, resolved as: `--provider`/`--placement-provider` flag → this env var → config `provider:` → registered default.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_PLACEMENT_PROVIDER` | `least-loaded` | Placement scoring provider (`least-loaded`, `expression`, or an `exa.providers.placement` plugin). Formula/coefficients read from `~/.config/examlops/providers.yaml` (`placement:` block). |
| `EXAMLOPS_COST_PROVIDER` | `flat-rate` | Cost calculation provider (finops); config in `~/.config/examlops/finops.yaml`. |
| `EXAMLOPS_CARBON_PROVIDER` | `green-ai-default` | Carbon calculation provider (finops); config in `finops.yaml`. |

Governance is declarative via `~/.config/examlops/policy.yaml` (no env var — file presence is the switch). Rules gate mutating operations (`retrain`, `promote`, `connect_cluster`, `agent_write`); with no file the effect is `allow`. See `docs/guides/programmable-mlops.md`.

---

## Next-Gen 40 (data · LLMOps · observability · governance · serving)

All additive and **graceful-degrading** — unset means the local/pure-python fallback is used, so every feature works with no external service. See the per-feature guides in `docs/guides/`.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_LAKEFS_ENDPOINT` / `_REPO` / `_REF` | unset / dataset / `main` | **A1** data versioning — set the endpoint to use lakeFS commit ids; else a deterministic content hash. |
| `EXAMLOPS_DATASET_REVISION` | unset | **A1** pin a pipeline run to a recorded dataset revision (set by `exa pipeline run --dataset-revision`). |
| `EXAMLOPS_OPENLINEAGE_URL` | unset (no-op) | **A2** OpenLineage — Marquez endpoint; unset ⇒ `emit_lineage` skips HTTP but still dual-writes `platform_db`. |
| `OTEL_SDK_DISABLED` | `true` | **C1** master tracing switch; `false` enables OTLP export of GenAI spans. |
| `EXAMLOPS_GENAI_CAPTURE_CONTENT` | unset (off) | **C1** capture prompt/completion content on spans (redactor-gated, D8). |
| `EXAMLOPS_VAULT_ADDR` / `EXAMLOPS_SECRETS_KEY` | unset | **D7** secrets — OpenBao address; else Fernet-local store keyed by `EXAMLOPS_SECRETS_KEY` (or `DASHBOARD_SECRET_KEY`). |
| `EXAMLOPS_VAULT_STRICT` | unset (fall back) | When truthy, a configured-but-unreachable OpenBao/Vault **fails the read** instead of silently downgrading to the local store or an environment variable. Set it wherever Vault is the system of record. |
| `EXAMLOPS_SECRET_TENANTS` | unset | **D7** per-tenant secret path-prefix scoping. |
| `EXAMLOPS_MULTITENANCY` | unset (off) | **D6** RBAC — off ⇒ every `authz.check` allows (single-tenant compat); truthy ⇒ default-deny enforcement. |
| `EXAMLOPS_SIGNING_KEY` | unset | **D3** supply-chain — HMAC model-signing key; else read from D7 secret `model-signing/key`. |
| `EXAMLOPS_SERVING_BACKEND` | `ray-compose` | **E1** serving backend — `ray-compose` (default) or `kserve-k8s`. |
| `EXAMLOPS_VECTOR_BACKEND` | `sqlite` | **B5** vector store — `sqlite` (persistent fallback) or `pgvector`. |
| `EXAMLOPS_PGVECTOR_DSN` | unset | **B5** Postgres+pgvector DSN (required for the `pgvector` backend). |
| `EXAMLOPS_GATEWAY_DEFAULT_MODEL` | `default` | **B2** logical model name for the default gateway route. |
| `EXAMLOPS_CACHE_EMBED_BACKEND` | unset | **B3** semantic cache — use a real local embedder instead of the token-hash fallback. |

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
| `EXAMLOPS_HPC_PARTITION` | falls back to `EXAMLOPS_SLURM_PARTITION` | Partition / queue to submit into. |
| `EXAMLOPS_HPC_TIME` | `2:00:00` (or `EXAMLOPS_SLURM_TIME`) | Wall-clock limit per job. |
| `EXAMLOPS_HPC_NODES` | `1` (or `EXAMLOPS_SLURM_NODES`) | Nodes per job. |
| `EXAMLOPS_HPC_MEM` | `16G` (or `EXAMLOPS_SLURM_MEM`) | Memory per job. |
| `EXAMLOPS_HPC_CPUS` | `4` (or `EXAMLOPS_SLURM_CPUS`) | CPUs per task. |
| `EXAMLOPS_HPC_REGISTRY` | `~/.config/examlops/clusters.yaml` | HPC Fleet cluster-definition registry file (Phase 36) |
| `EXAMLOPS_HPC_CLUSTER` | unset | Default fleet cluster for commands taking `--cluster` (Phase 36) |
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
| `EXAMLOPS_DATA_S3_ENDPOINT` | unset | Dedicated endpoint for the **dataset** object store (e.g. the SEANERGYS Day-0 store on JSC S3), separate from the platform MinIO holding MLflow artifacts/models. Unset ⇒ the minio dataset backend falls back to `MLFLOW_S3_ENDPOINT_URL` (single shared instance, legacy behaviour). |
| `EXAMLOPS_DATA_S3_ACCESS_KEY` / `EXAMLOPS_DATA_S3_SECRET_KEY` | unset | Credentials for the dedicated dataset store. Unset ⇒ fall back to `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. |

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
| `CONTROL_PLANE_TOKEN` | unset | Legacy operator bearer credential. It maps to principal `legacy`, tenant `default`, with `read` and `write` scopes; optional when the structured credential map is configured. |
| `CONTROL_PLANE_CREDENTIALS_JSON` | unset | JSON object keyed by bearer secret. Each value requires `principal`, `tenant`, and non-empty `scopes` containing only `read` and/or `write`. Malformed input or a secret duplicated from `CONTROL_PLANE_TOKEN` fails all bearer authentication closed. |
| `CONTROL_PLANE_URL` | `http://control-plane:8002` | Control plane URL used by the dataplane simulator and CI notify script |
| `CONTROL_PLANE_DB` | `PLATFORM_DB`, else `/data/approvals.db` | SQLite path for commands, approvals, admission, outbox, and ModelZoo state. Inside the service this becomes the shared `PLATFORM_DB`, keeping transactions and the relay on one file. |
| `CONTROL_PLANE_COMMAND_LEASE_SECONDS` | `300` | Time before an interrupted durable Prefect dispatch can be reclaimed with the same idempotency key. |
| `CONTROL_PLANE_RETRAIN_LOCK_SECONDS` | command lease (minimum `60`) | Coordinator lease for one tenant/model/dataset retrain dispatch. |
| `CONTROL_PLANE_POLLER_LEASE_SECONDS` | `30` (minimum `3`) | Coordinator lease used to elect the singleton ModelZoo poller. |
| `CONTROL_PLANE_EVENT_RELAY_SECONDS` | `1` | In-process outbox relay interval; `0` disables it. |
| `CONTROL_PLANE_EVENT_RELAY_BATCH_SIZE` | `100` | Maximum outbox rows claimed per relay pass. |
| `RETRAIN_RATE_LIMIT_PER_MIN` | `20` | Per-tenant write limit enforced by the selected `EXAMLOPS_COORDINATOR`. |

### ModelZoo Integration (Phase 12)

| Variable | Default | Purpose |
|---|---|---|
| `MODELZOO_WEBHOOK_SECRET` | unset | Shared secret for GitLab/GitHub push webhook verification. GitLab compares `X-Gitlab-Token` in constant time; GitHub verifies `X-Hub-Signature-256` HMAC-SHA256. Unset makes both webhook routes return 503. |
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
| `EXAMLOPS_DOCS_ROOT` | auto-detected | Project root the `/documents` docs router reads (`README.md` + `docs/`). Compose sets it to `/repo` (the bind-mounted repo); the dashboard image also bakes `docs/` so the router auto-detects `/app` when no bind mount exists (K8s). |
| `EXAMLOPS_PROVIDERS_DIR` | `~/.config/examlops/providers` | Root of notebook/CLI/dashboard-authored calculation providers (ADR 0074); files live at `<root>/<project>/<domain>/<name>.py`. Point every process (CLI, dashboard, notebooks) at one shared path so authored plugins resolve everywhere. Compose sets the dashboard to `/repo/.providers`. |
| `EXAMLOPS_HOST_REPO` | repo path on the deploy host | Host path of the platform repo, bind-mounted into each spawned JupyterHub notebook (read-only) so a notebook can `import examlops`; the `.providers` subdir is mounted read-write for shared plugin authoring. |
| `SEANERBUS_BRIDGE_STATUS_URL` | dashboard `http://localhost:8003` · `exa` `http://localhost:18003` | Where the bridge's `/health` and `/stats` are. The two defaults differ because the two readers do: the dashboard is also run bare-metal beside a bare-metal bridge, which serves on `8003` with no port mapping, while `exa` runs on the host, where the container publishes `18003`. In Docker the dashboard is set to `http://seanerbus-bridge:8003` (the docker-compose default). Set this variable to point both at one bridge — `exa seanerbus status` and `exa production verify` honour it (config key `seanerbus_bridge` under `[urls]`), as does the dashboard (whose DB config key `seanerbus_bridge_status_url` overrides it). |

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
| `SEANERBUS_PUBLISH_RESULTS` | `true` | Publish inference results back onto the bus. `false` makes the bridge consume-only. |
| `SEANERBUS_INFERENCE_UUID` | unset | **Legacy** global req/res UUID, used only when no per-model `seanerbus_uuid` is present in any model YAML. Prefer `exa seanerbus init-uuids`. |
| `SEANERBUS_JOB_TOPIC_UUID` / `SEANERBUS_RESULT_TOPIC_UUID` | unset | Pub/sub topic UUIDs for the job and result streams. |
| `RAY_SERVE_URL` | `http://localhost:18001` | Ray Serve URL used by the bridge to forward inference requests |
| `MODELS_YAML_DIR` | unset | Directory of per-model YAMLs for `ModelSchemaRegistry`; defaults to `pipelines/models/` |
| `DRIFT_WINDOW` | `50` | Rolling-window length for the bridge drift tracker |
| `DRIFT_THRESHOLD` | `0.5` | Per-model error-rate threshold that fires `/trigger-retrain` |
| `DRIFT_COOLDOWN` | `300` | Minimum seconds between drift-triggered retrains per model |

---

## Management Agent

The LangGraph agent (`platform/services/agent/`) runs as a developer REPL with `make skipper` or as
the HTTP/WebSocket service with `make skipper-server` (`skipper.server`, port 18004). The LLM backend
is chosen by which keys are set, in order: **Azure Foundry → Claude → Ollama**. When using
`ollama-tunnel` (Omega server, port 11436), start the tunnel first. Vars are set in `.env` and sourced
automatically.

| Variable | Default | Purpose |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | unset | Azure OpenAI / AI Foundry key. With `AZURE_OPENAI_ENDPOINT` set, this backend is preferred over Claude/Ollama. |
| `AZURE_OPENAI_ENDPOINT` | unset | Foundry v1 endpoint base URL (`https://<resource>.services.ai.azure.com/openai/v1/`, OpenAI-compatible). |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-5.5` | Foundry deployment name, used as the model id. |
| `ANTHROPIC_API_KEY` | unset | Claude backend key. Used when Azure is not configured. |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | Claude model id (adaptive thinking, `max_tokens=16000`). |
| `AGENT_MODEL` | `llama3.1:8b` | Ollama model name (fallback). Via ollama-tunnel: any model from the Omega/Kapa list. Must support tool calling. |
| `AGENT_OLLAMA_URL` | `http://localhost:11436` | Ollama server base URL. Omega tunnel default. Use `localhost:11434` for a local `ollama serve`. |
| `AGENT_CONTAINER_OLLAMA_URL` | `http://host.docker.internal:11436` | Compose-only Ollama URL. This avoids treating the agent container's own loopback as the host's Ollama server. |
| `AGENT_OLLAMA_KEEP_ALIVE` | `30m` | Pins the Ollama model in memory between turns (avoids reload latency on CPU-only servers). |
| `AGENT_OLLAMA_REASONING` | `false` | Disable (`false`) / force (`true`) / leave-default (`default`) thinking models' extra reasoning tokens. |
| `AGENT_SERVER_PORT` | `18004` | Port for the HTTP/WebSocket chat server (`skipper.server`). |
| `AGENT_API_KEY` | unset | Legacy single credential protecting completions, status, history, and WebSocket tools. It remains the dashboard fallback and maps to the `primary` principal. Prefer distinct caller credentials in `AGENT_API_KEYS_JSON`. |
| `AGENT_API_KEYS_JSON` | unset | JSON object mapping trusted principal names to distinct bearer credentials, for example `{"dashboard":"<dashboard-key>","cli-operator":"<cli-key>"}`. Names become server-derived conversation and memory owners; callers cannot choose them. Use distinct credentials wherever memory isolation matters. |
| `DASHBOARD_AGENT_API_KEY` | unset | Dashboard BFF credential forwarded to the agent. Its value must appear under the `dashboard` principal (or another intentionally named dashboard principal) in `AGENT_API_KEYS_JSON`. The dashboard prefers this over legacy `AGENT_API_KEY`. |
| `AGENT_REQUIRE_API_KEY` | `false` | Refuse agent-server startup when no API key is configured. The Helm deployment sets this to `true`. |
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
| `AGENT_MEMORY_DB` | `./skipper_memory.db` | Server-side SQLite file for the long-term `SqliteStore` (separate from `AGENT_DB` and `platform.db`). `exa agent memory` accesses it remotely by default; `--local` opts into direct file recovery. |
| `AGENT_EMBED_BACKEND` | `ollama` | Local embedding backend: `ollama` (via `AGENT_OLLAMA_URL`) or `sentence-transformers` (fully offline, in-process). No cloud. |
| `AGENT_EMBED_MODEL` | `nomic-embed-text` | Embedding model name. Must match `AGENT_EMBED_DIMS`. |
| `AGENT_EMBED_DIMS` | `768` | Embedding dimension. Must match the model (nomic-embed-text=768, bge-m3=1024, all-MiniLM-L6-v2=384). Mismatch ⇒ logged + long-term memory disabled. |
| `AGENT_SUMMARIZE_ENABLED` | `false` | Enable the context-trimming middleware for long threads (langchain-core `trim_messages`). |
| `AGENT_MAX_CONTEXT_TOKENS` | `12000` | Token budget for the trim hook when enabled. |
| `AGENT_MEMORY_REQUIRE_CONFIRM` | `true` | Confirmation-gate durable `record_procedure` writes (HITL). |
| `AGENT_MEMORY_AUDIT` | `true` | Write every memory mutation to `platform_db.audit_events`. |
| `AGENT_PROC_DEPRECATE_THRESHOLD` | `0.5` | (Phase 7, ADR 0106) Success rate below which a tool is "failing"; procedures relying on it are deprecated by `skipper.consolidate`. |
| `AGENT_PROC_DEPRECATE_MIN_CALLS` | `3` | Minimum recorded calls before a tool's success rate is trusted for deprecation. |
| `AGENT_CONSOLIDATE_MIN_EPISODES` | `3` | Incidents per model before consolidation promotes a candidate procedure to the HITL review queue. |
| `AGENT_MEMORY_TENANT_SCOPED` | `false` | Enables legacy environment-derived tenant prefixes for local/background memory operations. Authenticated HTTP requests are always isolated by their verified principal and server tenant, independently of this flag. |
| `AGENT_MEMORY_SHARED_BUCKET` | `global` | Tenant name of the shared memory bucket every project can read (cross-project tribal knowledge). |
| `AGENT_ACTOR` | `$EXAMLOPS_ACTOR`/`$USER`/`operator` | Actor recorded in preference memory + memory audit events. |

### Self-instrumentation (Phase 2, ADR 0103)

The reactive chat loop records each turn's tool calls into the shared `examlops.agentops` tables so `tool_success_rate` reflects real usage, and guards the turn with an in-loop circuit-breaker. Both are best-effort/fail-open — a missing `examlops.agentops` or `platform.db` never breaks a chat turn.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_INSTRUMENT_ENABLED` | `true` | Record turn tool-calls to `agent_sessions`/`agent_tool_calls` (feeds `tool_success_rate`). `false` ⇒ no telemetry. |
| `AGENT_CIRCUIT_BREAKER` | `true` | Abort a runaway turn in-loop (repeating tool loop, step blow-up, all-error burst) instead of only noticing post-hoc. |

### Reasoning topology (Phase 4, ADR 0100)

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_SUPERVISOR_MODE` | `auto` | `auto` builds the supervisor graph (deterministic router → scoped specialist sub-agents: manager/monitor/helper/finops/governor/general). `single` forces the legacy single ReAct agent. Any build failure in `auto` degrades to `single`. |
| `AGENT_USE_MCP_TOOLS` | `true` | Single-agent fallback sources tools from the `examlops.mcp` registry (reads + gated/tiered writes). The default supervisor path mixes MCP reads + in-repo writes regardless. |

### Proactive monitoring — skipper-watch (Phase 6, ADR 0104)

`python -m skipper.watch --once|--daemon` (`make skipper-watch`) reads drift/cost signals and, on a breach, raises an alert to the events outbox + audit log + episodic memory. LLM-free base loop.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_WATCH_ENABLED` | `true` | Kill-switch for the `--daemon` loop. |
| `AGENT_WATCH_INTERVAL_S` | `300` | Daemon cycle interval (seconds). |
| `AGENT_WATCH_DRIFT_Z` | `3.0` | Prediction-drift z-score threshold that raises a drift alert. |
| `AGENT_WATCH_COST_BUDGET` | `0` | Platform cost ceiling (USD) for the FinOps signal; `0` disables the cost check. |

### Knowledge / docs-RAG memory (Phase 3, ADR 0101)

Semantic search over the documentation (`search_knowledge` tool + `make skipper-knowledge-ingest`), reusing the platform's `examlops.vector_store` seam driven by Skipper's local embeddings. Degrades to the ripgrep docs tool when embeddings/vector-store are unavailable — never worse than today.

The degradation is graceful but no longer silent: `make skipper-knowledge-ingest` exits **1** when it indexed nothing because embeddings or the vector store were unavailable, and says which. Without that, a deployment could report a successful ingest while the index stayed empty and Skipper answered ungrounded.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_KNOWLEDGE_ENABLED` | `true` | Master switch for the docs-RAG tier. `false` ⇒ `search_knowledge` always uses ripgrep. |
| `AGENT_KNOWLEDGE_KB` | `skipper-knowledge` | Vector-store collection name for the indexed docs. |
| `AGENT_KNOWLEDGE_ROOTS` | `docs;design/adr` | Semicolon-separated roots to ingest (only `*.md` files). |
| `AGENT_KNOWLEDGE_CHUNK_SIZE` / `AGENT_KNOWLEDGE_OVERLAP` | `60` / `15` | Chunk size (words) and overlap for `rag.chunk_text`. |

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

Example for remote server access (`<REMOTE_HOST>` = the deploy node's address):
```bash
PUBLIC_MLFLOW_URL=http://<REMOTE_HOST>:15000
PUBLIC_PREFECT_URL=http://<REMOTE_HOST>:14200
PUBLIC_RAY_DASHBOARD_URL=http://<REMOTE_HOST>:18265
PUBLIC_PROMETHEUS_URL=http://<REMOTE_HOST>:19090
PUBLIC_GRAFANA_URL=http://<REMOTE_HOST>:13000
```

---

## Upstream model library

`seanergys_modelzoo` is an **upstream** library with its own repository and its own CI —
it is not part of ExaMLOps. Per ADR 0094 the platform core never imports it; only the
use-case pack does, through the `pipelines.usecase` loader seam. It is therefore not
vendored in this repository.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_MODELZOO_DIR` | `<repo>/modelzoo` | Where to find the `seanergys_modelzoo` checkout. Every runtime path-resolution site (pipeline engine, Ray Serve, control plane, use-case pack, `exa data`/`exa synth`) honours it. |

The deploy pipeline clones it into `$EXAMLOPS_DEPLOY_PATH/modelzoo` — the default location —
so nothing needs setting in a standard deployment. Point the variable elsewhere if you keep
the checkout outside the repo. Tests that need the library **skip** when it is absent rather
than failing, so a clone without it still gets a green suite.

---

## Remote deploy node (site-specific)

The committed tree carries deliberately generic defaults so that no particular
site's addressing or filesystem layout is published. Set the real values in
`.env` on the deploy node (and locally if you use `make remote-rebuild`).

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_DEPLOY_HOST` | `examlops-deploy` | ssh host or alias of the deploy node; used by `make remote-rebuild` |
| `EXAMLOPS_DEPLOY_PATH` | `/opt/examlops` | Checkout path on the deploy node; used by `make remote-rebuild` and the `platform/ci/*.sh` helper scripts |
| `EXAMLOPS_HOST_REPO` | `/opt/examlops` | The same path as seen by the Docker host — bind-mounted into spawned JupyterHub notebooks and used by the compose stack |
| `EXAMLOPS_STATE_DIR` | same as `EXAMLOPS_HOST_REPO` | Persistent databases and authored providers. GitLab deployments set this to a stable sibling directory while application releases use commit-addressed directories. |
| `EXAMLOPS_HOST_STATE` | same as `EXAMLOPS_HOST_REPO` | Host-side persistent-state path passed by Compose to JupyterHub for sibling-container bind mounts. Normally derived from `EXAMLOPS_STATE_DIR`; do not set it separately. |
| `EXAMLOPS_GITLAB_HOST_ENTRY` | `gitlab.example.com:127.0.0.1` | `"<host>:<ip>"` DNS pin injected into the control plane via `docker-compose.lxp.yml` `extra_hosts`, for deploy nodes that cannot resolve an internal GitLab |

```bash
EXAMLOPS_DEPLOY_HOST=my-deploy-node
EXAMLOPS_DEPLOY_PATH=/srv/examlops
EXAMLOPS_HOST_REPO=/srv/examlops
EXAMLOPS_STATE_DIR=/srv/examlops-state
EXAMLOPS_GITLAB_HOST_ENTRY=gitlab.internal.example.com:10.0.0.5
```

> If you are upgrading an existing deployment that relied on the previous
> hardcoded defaults, set `EXAMLOPS_HOST_REPO` and `EXAMLOPS_DEPLOY_PATH`
> explicitly **before** pulling — otherwise the bind mounts and the JupyterHub
> spawner will point at the new generic default instead of your real checkout.

---

## JupyterHub

| Variable | Default | Purpose |
|---|---|---|
| `DOCKER_NETWORK_NAME` | `examlops_default` | Docker network that spawned user containers join — must match the Compose project network |
| `JUPYTERHUB_PUBLIC_URL` | unset | Browser-facing Hub URL the dashboard links to. Unset ⇒ the Workbenches console shows no link. |
| `JUPYTERHUB_API_URL` | unset | Hub REST endpoint the dashboard uses to read workbench state |
| `JUPYTERHUB_DASHBOARD_TOKEN` | unset | Hub API token the dashboard authenticates with |
| `JUPYTERHUB_WORKBENCH_USER` | unset | Hub user whose server a workbench is spawned as |

There is no `JUPYTERHUB_PORT`. The Hub listens on **8000** inside the container and Compose maps
`18888:8000`; change the published port in `docker-compose.yml`, not through an environment
variable.

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
| `OTEL_TRACES_SAMPLER` | `parentbased_traceidratio` | Sampler. The parent-based default keeps a trace whole once it is sampled. |
| `OTEL_TRACES_SAMPLER_ARG` | `0.05` | Sampled fraction. Bounded to 5% deliberately: an unsampled tracer on a busy inference path costs more than the traces are worth. |

The control plane and dashboard are auto-instrumented via the `opentelemetry-instrument` launcher
(no app code changes); the Ray Serve inference pipeline uses the `examlops.observability` helper.

---

## Feature gates

Every gate here is **off unless set**, and a gate that is off gates nothing — none of them will
appear in a log or a CI summary until you switch it on.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_SLO_GATE_ENABLED` | off | Make `exa slo` failures block a promotion instead of reporting. |
| `EXAMLOPS_FAIRNESS_GATE_ENABLED` | off | Make subgroup-fairness failures block. |
| `EXAMLOPS_SYNTHETIC_ONLY_GATE` | off | Refuse to train on anything but synthetic data — for a use case that may not touch real records yet. |
| `EXAMLOPS_KSERVE_LIVE_APPLY` | off | Actually apply generated KServe manifests to the cluster. Off ⇒ the manifest is produced and the endpoint stays `PENDING`. |

Accepted truthy values are `1`, `true`, `yes`, `on` (case-insensitive); anything else is off.

---

## Carbon intensity signal

Unset ⇒ the carbon provider uses its static coefficient rather than a live grid signal.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_GRID_INTENSITY_URL` | unset | Endpoint returning current grid carbon intensity. |
| `EXAMLOPS_GRID_INTENSITY_ZONE` | unset | Zone/region appended to that URL. |
| `EXAMLOPS_GRID_INTENSITY_TOKEN` | unset | Bearer token for the signal provider. |

---

## Paths, identity & misc

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_CONFIG_DIR` | `~/.config/examlops` | Directory holding `config.toml`, `providers.yaml`, `finops.yaml`, `policy.yaml`. `EXAMLOPS_CONFIG` overrides the config *file* specifically. |
| `EXAMLOPS_REPO_ROOT` | auto-detected | Repository root, when the platform runs from somewhere the walk-up cannot find it. |
| `EXAMLOPS_TENANT` | `default` | Tenant recorded on writes when multi-tenancy is on. |
| `EXAMLOPS_VAULT_TOKEN` | unset | Token for the OpenBao/Vault secrets backend. Unset ⇒ the backend degrades to Fernet, then to env. |
| `EXAMLOPS_POLICY_ENGINE` | built-in | `opa` uses Rego via a local OPA binary, **if it is available** — otherwise the built-in engine stays in use, silently. |
| `EXAMLOPS_POLICY_BUNDLE_DIR` | `~/.config/examlops/bundle` | Where Rego bundles are read from. |
| `EXAMLOPS_LLM_LAUNCHER` | `external` | Default launcher for `exa serve llm` — one of `external`, `compose`, `slurm`, `flux`, `kserve` (`slurm` and `flux` are the same HPC launcher under two scheduler names). `--launcher` overrides it. |
| `EXAMLOPS_LLM_COST_PROVIDER` | from `finops.yaml` | Provider for LLM token cost. |
| `EXAMLOPS_KSERVE_GATEWAY_URL` | unset | Gateway the generated KServe endpoint is reachable on; recorded on the endpoint. |
| `FEATURE_STORE_DIR` | `.feature_store` beside the platform datastore | On-disk feature store root. |
| `MLFLOW_SQLITE_DB` | `./mlflow.db` | MLflow's own SQLite file, when it is not on Postgres — the backup sqlite tier looks for it here. |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `minioadmin` | MinIO credentials. Compose passes these to JupyterHub as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. **Change both before exposing the stack.** |
| `DASHBOARD_TOKEN` | unset | Dashboard API token used by CLI/agent callers, stored as a CLI config field. |
| `LOG_FORMAT` | `text` | `json` switches structured logging on. |
| `MODELS` | `JPCP MACK MCBound` | Space-separated model list used by the batch/simulator entrypoints. |
| `RAY_WORKER_ID` | `default` | **Set by Ray**, read by the serving replica to label itself. Not an operator knob. |

---

## Simulator & retry tuning

| Variable | Default | Purpose |
|---|---|---|
| `DRIFT_THRESHOLD` / `DRIFT_WINDOW` / `DRIFT_COOLDOWN` | `0.5` / `50` / `300` | Simulator drift trigger, sample window, and seconds between triggers. `CLIENT_SIM_DRIFT_THRESHOLD`, `CLIENT_SIM_DRIFT_WINDOW` and `CLIENT_SIM_DRIFT_COOLDOWN` are the older names, read only when the short ones are unset. |
| `MLFLOW_HTTP_REQUEST_MAX_RETRIES` | `3` (or `RAY_MLFLOW_MAX_RETRIES`) | Retries on MLflow HTTP calls. Set with `setdefault`, so an explicit value always wins. |
| `MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR` | `1` (or `RAY_MLFLOW_BACKOFF_FACTOR`) | Backoff factor for those retries. |
| `PREFECT_CB_FAIL_MAX` | `5` | Consecutive Prefect failures before the circuit breaker opens. |
| `PREFECT_CB_RESET_TIMEOUT` | `30.0` | Seconds before it half-opens again. |

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
