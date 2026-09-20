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
| `RAY_PREDICT_TIMEOUT` | `30` | Hard ceiling (s) on a single `model.predict()`; exceeding it returns HTTP 504. A caller's shorter `X-ExaMLOps-Budget-Ms` lowers it for that request |
| `RAY_PREDICT_WORKERS` | `4` | Thread-pool size backing the predict timeout |
| `RAY_MAX_ONGOING_REQUESTS` | `100` | Max concurrent requests per Ray Serve replica |
| `EXAMLOPS_GATEWAY_TENANT_RPM` | `600` | [Serving gateway](../guides/serving-gateway.md): requests per tenant per minute, counted across every gateway replica through the shared coordinator. Over it: `429` with `Retry-After`. `0` turns the quota off. |
| `EXAMLOPS_GATEWAY_SCOPE_CACHE_SECONDS` | `30` | With `EXAMLOPS_MULTITENANCY` on, how long the serving gateway caches which project a model belongs to. With nothing cached and the platform store unreachable, a request is refused (`503`), never opened. |
| `EXAMLOPS_GATEWAY_KEY_CACHE_SECONDS` | `60` | How long the serving gateway keeps accepting a virtual key it verified, while the platform datastore is unreachable. A key it has not verified is refused (`503`). |
| `RAY_MAX_QUEUED_REQUESTS` | `-1` | Load shedding for the model server: once this many requests wait at a caller (the HTTP proxy or a handle), the next is answered 503 at once. `-1` = unbounded (Ray's default) — size it per site, see [Ray Serve → Overload](../components/ray-serve.md#overload-deadlines-and-load-shedding) |
| `INFERENCE_MAX_QUEUED_REQUESTS` | `-1` | The same bound for the inference-pipeline ingress (`/infer-pipeline/infer`); a shed request gets 503 with `Retry-After: 1` |
| `INFERENCE_ROUTE_RETRIES` | `2` | Ceiling on retries of the inference-pipeline → MultiModelServer hop (transport errors, 503, and one retry of a request whose replica died under it); each retry must also fit the request's deadline and the retry budget |
| `INFERENCE_PIPELINE_REPLICAS` | `2` | Replicas of each inference-pipeline stage (ingress, feature transformer, router). They hold no state, so more replicas are only redundancy |
| `INFERENCE_DEADLINE_SECONDS` | `30` | Time budget given to an inference request that arrives without an `X-ExaMLOps-Budget-Ms` header; every hop spends from it and the request is answered 504 when it runs out |
| `INFERENCE_DEADLINE_MAX_SECONDS` | `300` | Ceiling on the budget a caller may ask for with `X-ExaMLOps-Budget-Ms` |
| `INFERENCE_RETRY_MAX_TOKENS` / `INFERENCE_RETRY_TOKEN_RATIO` | `100` / `0.1` | Retry budget of the inference router (gRPC `retryThrottling`): a failed attempt costs one token, a success earns the ratio back, and retries are allowed only above half. A burst of about 50 failures (a replica dying with its requests) is retried; a sustained outage earns about one retry per ten successes, so it is not multiplied by retries. Was 10, which failed requests whenever more than five were on a dying replica |
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
`RAY_MEM_LIMIT` (`6g`), `RAY_CPUS` (`2.0`), `MLFLOW_MEM_LIMIT` (`4g`; MLflow 3.16 holds about
0.85 GiB per server worker, and `MLFLOW_WORKERS`, default `2`, sets how many),
`ORCHESTRATOR_MEM_LIMIT` (`2g`), `POSTGRES_MEM_LIMIT` (`1g`),
`CONTROL_PLANE_MEM_LIMIT` (`512m`), `DASHBOARD_MEM_LIMIT` (`1g`),
`JUPYTERHUB_MEM_LIMIT` (`2g`), `BRIDGE_MEM_LIMIT` (`1g`), `DATAPLANE_MEM_LIMIT` (`2g`), and for the `lineage` profile `MARQUEZ_MEM_LIMIT` (`1g`), `MARQUEZ_WEB_MEM_LIMIT` (`256m`), `MARQUEZ_DB_MEM_LIMIT` (`512m`).

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
| `EXAMLOPS_POSTGRES_UNREACHABLE_TTL` | `5.0` | How long an "unreachable" verdict is cached, so one command probes once, and every call during an outage fails in microseconds. It expires, so a long-lived process recovers when the server returns. |
| `EXAMLOPS_POSTGRES_POOL_TIMEOUT` | `2.0` | Seconds a call waits for a free pooled connection before failing (the driver's own default is 30). Short enough to fit inside a readiness probe: with the store gone, a 30 s wait held every worker and the service answered nothing. Raise it only if a legitimately busy pool starts failing. |
| `EXAMLOPS_POSTGRES_TCP_TIMEOUT_MS` | `10000` | `tcp_user_timeout` (milliseconds) on every connection: how long a query may wait for an unacknowledged packet before the kernel gives up. This is what bounds a query whose server vanished without a reset — a killed container, a severed route — where retransmission would otherwise take minutes. A DSN naming `tcp_user_timeout` keeps its own value. |
| `EXAMLOPS_COORDINATOR` | `db` | Cross-process coordination backend: `db` (via the datastore) or `redis` (cross-host HA). |
| `EXAMLOPS_IDEMPOTENCY_TTL` | `86400` | Seconds a stored result is kept for a mutating MCP tool call made with an `idempotency_key` (ADR 0147 d4); a repeat within the window returns the original result. |
| `EXAMLOPS_IDEMPOTENCY_PENDING_TTL` | `300` | Seconds an in-flight `idempotency_key` claim blocks a repeat before it is treated as abandoned (a crashed caller). |
| `EXAMLOPS_EVENT_PUBLISHER` | `log` | Event publisher: `log` (dependency-free, dev/tests), `nats` (NATS JetStream — the backbone, ADR 0124; needs `examlops[events]` and `EXAMLOPS_NATS_URL`) or `redis` (Redis Streams). `kafka` is a fail-loud placeholder. Every event is published as a CloudEvents 1.0 envelope. Drain the outbox with `exa events relay` (the control plane relays its own). |
| `EXAMLOPS_TELEMETRY_VIA_EVENTBUS` | unset (off) | **Serving-plane/control-plane separation (ADR 0123 decision 4).** Truthy makes the Dataplane bus bridge publish per-inference drift/input-embedding telemetry onto the NATS event backbone (`serving.inference_telemetry`, direct JetStream publish — not the durable outbox) instead of writing `drift_snapshots`/`input_snapshots` to `platform.db` directly; `exa drift consume-telemetry` performs those writes wherever it runs, so the bridge no longer needs database connectivity. Needs `EXAMLOPS_NATS_URL` on both the bridge and the consumer. Off by default: the bridge writes directly, as before, and no consumer is needed. A publish failure is counted (`dataplane_bus_telemetry_eventbus_publish_failures_total`) and the sample is dropped, matching the drop-on-full behaviour of the direct-write path it replaces — these are windowed aggregates, so a lost sample is never retried onto a direct write. |
| `EXAMLOPS_NATS_URL` | unset | NATS server(s), e.g. `nats://nats:4222` (comma-separated for a cluster). Required by the `nats` publisher, event consumers and `exa events tail`. |
| `EXAMLOPS_NATS_STREAM` | `EXAMLOPS_EVENTS` | JetStream stream holding platform events and dead letters; created on first use. |
| `EXAMLOPS_NATS_SUBJECT_PREFIX` | `examlops.events` | Subject prefix: topic `retrain.scheduled` publishes to `examlops.events.retrain.scheduled`. Dead letters use `examlops.dlq.<consumer>`. |
| `EXAMLOPS_NATS_MAX_AGE_SECONDS` | `604800` (7 days) | Stream retention; a consumer down longer than this misses events. |
| `EXAMLOPS_NATS_DUPLICATE_WINDOW` | `120` | Seconds JetStream remembers `Nats-Msg-Id` (the outbox id), so a relay retry inside the window is one message, not two. |
| `EXAMLOPS_NATS_REPLICAS` | `1` | Stream replicas; `3` on a clustered deployment. |
| `EXAMLOPS_NATS_TIMEOUT` | `5` | Seconds per NATS operation before the call fails (the outbox keeps the event). |
| `EXAMLOPS_KAFKA_BROKERS` | unset | Reserved comma-separated Kafka brokers. Selecting the Kafka publisher fails loudly until its transport is implemented. |
| `EXAMLOPS_REDIS_PREFIX` | `examlops:coord` | Namespace prefix for Redis coordination keys; use a distinct value per installation sharing a Redis database. |
| `EXAMLOPS_REDIS_EVENT_STREAM` | `examlops.events` | Redis Stream name used by the implemented event publisher. |
| `EXAMLOPS_REDIS_EVENT_MAXLEN` | `100000` | Approximate maximum Redis Stream length; must be a positive integer. |
| `EXAMLOPS_EVENT_MAX_ATTEMPTS` | `5` | Maximum automatic outbox publication attempts, spent only by an event a broker actually refused — an unreachable broker defers the batch and charges nothing. Exhausted rows remain as poison evidence for operator inspection. |
| `EXAMLOPS_CONFIG` | `~/.config/examlops/config.toml` | Overrides the CLI config path so containers and CI can pin a config and tests run hermetically. |
| `EXAMLOPS_PROJECT` | active CLI context or `default` | Explicit project/tenant context for CLI commands and trusted server-side agent scoping. Request callers cannot override an authenticated agent tenant with payload data. |
| `EXAMLOPS_USECASE_DIR` | `usecases/reference` | Selects the use-case pack (ADR 0094). The platform core names no concrete model or dataset; this is how it reaches content. |
| `EXAMLOPS_AGENT_DIR` | derived from the repo | Where the Skipper package lives for explicit `exa agent memory … --local` recovery. Normal memory administration uses `AGENT_URL`. |
| `EXAMLOPS_PROJECTS_BUCKET` | `examlops-projects` | MinIO bucket holding per-project `artifacts/`, `datasets/`, `cache/`. |

### Governance & secrets

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_SECRETS_KEYS` | unset | Secrets KEK keyring, `key_id:fernet_key,…`. |
| `EXAMLOPS_SECRETS_ACTIVE_KEY` | first key | Which key new writes are encrypted with. Rotate online with `exa secrets rewrap`; `DASHBOARD_SECRET_KEY` is a decrypt-only legacy fallback. |
| `EXAMLOPS_ACTOR` | `$USER` | Actor stamped into every audit event. Set it in CI and in scripts, or the log records whichever account the runner happens to use. |
| `EXAMLOPS_PRINCIPAL_KIND` | `human` | Set to `agent` by an agent runtime for the `exa` processes it drives. An agent is never auto-confirmed: with `-o json` or `--yes` a command that asks for confirmation is refused with `plan_required` instead of proceeding. Any other value (or unset) means a human, whose scripts behave as before. |
| `EXAMLOPS_AUDIT_WORM_PATH` | unset | External append-only WORM anchor for audit checkpoints — a local file in dev, an S3 Object-Lock path or Rekor log in production. Verify with `exa audit verify-worm`. |
| `EXAMLOPS_AUDIT_STREAM` | unset (off) | `1` publishes every audit event on the event backbone as `audit.recorded`, in the same transaction as the audit row, with the fields the hash chain covers so a SIEM can verify it (`examlops.data.audit.verify_audit_stream`). Set it on every process that writes audit events. |
| `EXAMLOPS_OIDC_ISSUER` | unset | OIDC issuer for RS256 access-token validation. Unset ⇒ SSO off (single-tenant). See also `EXAMLOPS_OIDC_AUDIENCE` / `_JWKS` / `_TENANT_CLAIM` / `_SUBJECT_CLAIM`. |
| `CONTROL_PLANE_ALLOWED_HOSTS` | `*` | Comma-separated allow-list for the control plane's Host header. `*` is a development default. |
| `EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS` | unset (off) | `1` opts into adding unknown SSH host keys. Off means `RejectPolicy` — an unknown host fails rather than being trusted. Governs the HPC SSH transport and the dataplane's `sftp://` sources alike (see `EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS`). |

### Autopilot

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_AUTOPILOT_ENABLED` | unset (disabled) | Master kill-switch for the self-driving loop. Falls back to the `autopilot_config` table when unset; `exa autopilot enable/disable` sets it persistently. |
| `EXAMLOPS_AUTOPILOT_LEASE_TTL` | `900` | TTL (s) of the distributed cycle lease — only one cycle runs at a time, and a crashed holder's lease expires. |
| `EXAMLOPS_CONTRACTS_FILE` | unset | YAML overlay for the ADR-0113 blast-radius contracts. May only NARROW the built-ins (autonomy toward REVIEW/DISABLED, extra `may_not_change` entries, smaller extent caps); widening is a code change. |
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
| `EXAMLOPS_OIDC_DEFAULT_ROLE` | unset | Legacy single-issuer mode only: the platform role (`viewer`/`operator`/`admin`) a valid token gets when none of its groups map to one. Unset ⇒ such a user is authenticated but not authorized. |

---

## Identity federation & delegated authorization (ADR 0120)

ExaMLOps federates with the identity provider and authorization service the hosting data center
already runs. Guide: [Identity federation](../guides/identity-federation.md).

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_IAM_CONFIG` | unset | Path to the **trust file** (`identity-providers.yaml`): one entry per trusted center — issuer, audience, keys, claim→role mapping, tenant binding, the center's PDP. Unset ⇒ the legacy `EXAMLOPS_OIDC_*` single issuer, or federation off. Re-read when it changes on disk. Must be set on the dashboard **and** the control plane. |
| `EXAMLOPS_IAM_JWKS_TTL` | `3600` | Seconds a center's discovery document and JWKS stay cached. |
| `EXAMLOPS_IAM_JWKS_MIN_REFRESH` | `60` | Minimum seconds between forced JWKS refetches on an unknown `kid` — picks up key rotation without letting random-`kid` tokens hammer the IdP. |
| `EXAMLOPS_IAM_HTTP_TIMEOUT` | `5` | Timeout (s) for calls to an IdP (discovery, JWKS, token, introspection). The PDP has its own `timeout_s` in the trust file. |
| `EXAMLOPS_IAM_STEP_UP` | unset | `enforce` ⇒ local password sessions must have authenticated within `EXAMLOPS_IAM_STEP_UP_MAX_AGE` for step-up actions (model promotion, secret reveal). Federated users are governed by their center's `step_up` entry instead. |
| `EXAMLOPS_IAM_STEP_UP_MAX_AGE` | `900` | Seconds a local password login stays "recent" for step-up actions. |
| `EXAMLOPS_IAM_ACCOUNT_CACHE_TTL` | `10` | Seconds a federated account's status (active / deactivated / deprovisioned, ADR 0132) is cached per process. A deactivation over SCIM or `exa auth deactivate` takes effect at once in the process that received it and within this many seconds everywhere else. |
| `EXAMLOPS_AUTH_ISSUER` | unset | CLI: the IdP `exa auth login` uses when no `--provider`/`--issuer` is given (config key `auth_issuer`, per context). |
| `EXAMLOPS_AUTH_CLIENT_ID` | unset (`exa-cli`) | CLI: the public OAuth client id registered for `exa` at that IdP (config key `auth_client_id`). |
| `DASHBOARD_BACKBONE` | `auto` | With `EXAMLOPS_EVENT_PUBLISHER=nats` (and `EXAMLOPS_NATS_URL` set), each dashboard replica relays the platform's events from NATS onto its live stream (`GET /api/v1/stream`). `off` keeps the stream to this process's own events. |
| `DASHBOARD_LOCAL_LOGIN` | `true` | `false` ⇒ the shared viewer/admin passwords stop working and organisation SSO is the only way into the dashboard. |
| `DASHBOARD_SESSION_COOKIE_SECURE` | `true` | SSO session cookie is `Secure` + `__Host-` prefixed. Browsers accept that over HTTPS and `http://localhost` only; set `false` for a deployment reached over plain `http://<host>`. |
| `DASHBOARD_SSO_SESSION_HOURS` | `8` | Lifetime of a dashboard session created by SSO. |

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
| `AGENT_ALLOW_UNAUTHENTICATED` | unset | Development opt-out only. An agent bound beyond loopback (`AGENT_SERVER_HOST` other than `127.0.0.1`/`localhost`/`::1`, as in compose) with no `AGENT_API_KEY`/`AGENT_API_KEYS_JSON` refuses every request with 503; set this truthy to serve it as the anonymous `local` principal anyway. |
| `EXAMLOPS_UID` / `EXAMLOPS_GID` | `1000` / `1000` | Dev compose only: the user and group the agent, control-plane and dashboard images are built to run as, so it can write the host-owned `/state` mount (`platform.db`). Set them to your host `id -u` / `id -g` if those are not 1000. Image builds without these (Helm, CI) run as `10001`. |
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
| `EXAMLOPS_DATAPLANE_MAX_ROWS` / `EXAMLOPS_DATAPLANE_MAX_BYTES` / `EXAMLOPS_DATAPLANE_MAX_SECONDS` | unset (no cap) | ADR 0130 dataplane — platform-wide caps on a single connector pull (`examlops.dataplane.types.global_limits()`); a source-level `Limits` is capped against these via `Limits.capped()`. Unset ⇒ no platform-wide bound. |
| `EXAMLOPS_DATAPLANE_CONTRACT_MAX_ROWS` | `200000` | ADR 0130 dataplane — the most rows of ONE table a data-contract check reads (`examlops.dataplane.pull.contract_max_rows()`): both a source's ingestion contract (`--contract`, checked before a pull commits) and the training gate on a pinned snapshot stream the table's Parquet parts and stop at this many rows, so a large snapshot never has to fit in memory. Tables are checked one at a time, never concatenated. When a table is cut short the check ran on a sample: the pull's result and its `dataplane_pull_succeeded` audit record, and the gate's report, say `sampled` with `rows_checked` of `rows`; `min_rows` still judges the table's real row count. A non-positive or non-integer value falls back to the default (with a warning). |
| `EXAMLOPS_DATAPLANE_ALLOWED_HOSTS` | unset (none allowed) | ADR 0130 dataplane egress guard (`examlops.dataplane.safety`) — comma-separated allow-list of hostnames and/or CIDRs a connector may reach even though they resolve to a platform-internal name (e.g. `mlflow`, `minio`) or a non-public address. A hostname entry trusts that name's DNS answer for *every* address it returns; a CIDR entry only admits a resolved address that falls inside it. Checked once per connection and pinned to the resolved IP (DNS-rebinding-proof); unset ⇒ only public addresses on non-internal names are allowed. |
| `EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES` | unset (`false`) | ADR 0130 dataplane — enables a local-filesystem source/sink for connectors that support one (`examlops.dataplane.safety.local_files_allowed()`). Truthy (`1`/`true`/`yes`/`on`) opts in; off by default because a local path escapes the egress guard entirely. |
| `EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS` | unset | ADR 0130 dataplane `files` connector — path of an OpenSSH `known_hosts` file whose keys an `sftp://` source's host is verified against, in addition to the service user's `~/.ssh/known_hosts` (`examlops.dataplane.connectors.files._guarded_sftp_class`). An unknown host key is rejected (`RejectPolicy`) unless `EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS` is truthy; a set path that cannot be read fails the pull rather than falling back. Auto-added keys are never written back to this file. |
| `EXAMLOPS_DATAPLANE_STORE_URL` | unset | ADR 0130 §7 snapshot store (`examlops.dataplane.store.store_from_env()`) — fsspec URL of the object store holding committed snapshots. Unset ⇒ `s3://<EXAMLOPS_DATA_BUCKET>/dataplane` on the dedicated dataset store (`EXAMLOPS_DATA_S3_*`), falling back to the platform MinIO (`MLFLOW_S3_ENDPOINT_URL`/`AWS_*`). Set to any fsspec URL (e.g. `file://...`) to point at a different store, such as in tests. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | unset ⇒ `us-east-1` | ADR 0130 dataplane S3 (`examlops.dataplane.s3.s3_filesystem()`, pyarrow's native S3 filesystem — no s3fs) — the region S3 requests are signed for, for the snapshot store and for `files` sources alike; the first one set wins. MinIO and most S3-compatible endpoints ignore it; set it for an AWS bucket outside `us-east-1`. Always resolved to a value, so the AWS SDK never asks the EC2 metadata endpoint. |
| `EXAMLOPS_DATAPLANE_CACHE_DIR` | `$EXAMLOPS_DATA_DIR/cache/dataplane`, else `~/.cache/examlops/dataplane` | ADR 0130 §8 training cache (`pipelines.datasets.dataplane.cache_root()`) — local directory a training run materializes its pinned snapshot into (`<cache>/<source>/<revision>/`). A materialized revision re-hashes to its pinned id, so the directory is safe to share between runs and to delete at any time. Point it at node-local scratch on an HPC node. |
| `EXAMLOPS_DATAPLANE_URL` | `http://localhost:18010` | ADR 0130 — dataplane service URL (`exa dataplane pull --remote`, `examlops.cli._config.Config.dataplane_url`); the CLI posts a remote pull request here instead of running it in-process. The dashboard backend also reads this (`settings.dataplane_url`) to probe `/health`. |
| `PUBLIC_DATAPLANE_URL` | `http://localhost:18010` | Browser-facing dataplane URL shown by the dashboard's `/api/health` (`settings.public_dataplane_url`), rewritten to the request's host the same way the other service links are. |
| `DATAPLANE_BIND` | `127.0.0.1` | Host address the Compose `dataplane` service publishes port `18010` on. Loopback by default, because with no `DATAPLANE_TOKEN` and no identity federation its reads are open. Set `0.0.0.0` only together with a real `DATAPLANE_TOKEN` (or federation), e.g. so remote browsers can follow the dashboard's dataplane link. |
| `EXAMLOPS_DATAPLANE_TOKEN` | unset | ADR 0130 — bearer token for the dataplane service's `--remote` routes (`examlops.cli._config.Config.dataplane_token`). Unset ⇒ requests carry no `Authorization` header. |
| `DATAPLANE_TOKEN` | unset | ADR 0130 dataplane **service** — the static bearer it accepts (`examlops.dataplane.service.auth`); grants read, write and ingest. Must be a real secret of at least 16 characters (surrounding whitespace, such as a trailing newline from a file, is ignored). Unset or blank and no identity federation ⇒ reads are open (loopback-only deployments) and every write returns 503 — the only open state. Set to a placeholder (`changeme`, `…-change-me-…`) or to anything shorter ⇒ **not** treated as unset: with no working federation every route but `/health`, `/ready` and `/metrics` returns 503 ("set but is a placeholder or too short"), reads included; with federation the bad token is ignored and only IdP tokens are accepted. A usable token ⇒ every route but those three needs `Authorization: Bearer` (401 missing, 403 wrong). `/health` reports the effective mode as `auth` (`open`, `static`, `federated`, `static+federated`, `token-invalid`, `trust-file-invalid`, …), and the service logs it once at startup. Tokens from a trusted data-center IdP (ADR 0120, `EXAMLOPS_IAM_CONFIG`) are accepted alongside it: any mapped role reads, `operator` and up write, and the center's PDP may veto. With `EXAMLOPS_MULTITENANCY` on, an IdP caller is also scoped to projects — `viewer` (read) or `editor` (write) on the source's `project:<p>`, a global source written only from `operator` up — while this static token keeps full access. The client side is `EXAMLOPS_DATAPLANE_TOKEN`. |
| `DATAPLANE_PORT` | `8010` | ADR 0130 dataplane service — container port `platform/services/dataplane/main.py` listens on (published on the host as `18010`). |
| `EXAMLOPS_DATAPLANE_SCHEDULER_INTERVAL` | `30` | ADR 0130 dataplane service — seconds between scheduler ticks (`examlops.dataplane.service.scheduler.Scheduler`); each tick queues every enabled source whose `schedule` has elapsed since its last pull. Must be > 0. |
| `EXAMLOPS_DATAPLANE_WORKERS` | `2` | ADR 0130 dataplane service — pulls that run at once (the scheduler's worker pool, shared by scheduled and API-requested pulls). Further due pulls wait queued; a source never has two pulls queued or running. Must be ≥ 1. |
| `EXAMLOPS_DATAPLANE_ROLE` | `all` | ADR 0131 dataplane service role (`examlops.dataplane.service.app.dataplane_role()`): `all` runs everything; `api` mounts the source, pull and stream routes (push included) and runs the pull scheduler, but no stream supervisor; `streams` runs only the stream supervisor (the long-lived connectors) and serves just `/health`, `/ready` and `/metrics`. Any other value fails at startup. A single host runs `all`; a larger site runs push on `api` replicas and the connectors on a `streams` process. `/metrics` is unauthenticated in every role and its `dataplane_stream_*` series name every stream (`project`, `stream`, `connector`, `model`) — as Plan 1's source gauges name every source — so keep the port loopback or in-cluster (`DATAPLANE_BIND`). |
| `DATAPLANE_INGEST_TOKEN` | unset | ADR 0131 dataplane service — an optional second static bearer with the `ingest` scope only: it may push to `POST /streams/{name}/messages` and nothing else (every read and write route answers 403 on the missing scope). Same rules as `DATAPLANE_TOKEN`: at least 16 characters, never a placeholder, compared in constant time. `DATAPLANE_TOKEN` itself holds read, write and ingest. Ingest is never open: with neither token nor identity federation configured, push answers 503. Setting only this token means the service is no longer in the open loopback state, so reads then need `DATAPLANE_TOKEN` or federation too. A federated caller may push with `operator` or `editor` on the stream's project; the center's PDP decides `dataplane.ingest`. |
| `EXAMLOPS_DATAPLANE_PUSH_READ_TIMEOUT` | `10` | ADR 0131 dataplane service — seconds a push may take to send its whole body; a body still arriving after that is answered 408 (a problem document), so a slow or stalled sender cannot hold a connection and an in-flight slot open. Must be > 0. |
| `EXAMLOPS_DATAPLANE_PUSH_MAX_BYTES` | `1048576` | ADR 0131 dataplane service — the largest body `POST /streams/{name}/messages` accepts (a stream's own `limits.max_bytes` may lower it). Checked on `Content-Length` first and then on the bytes actually read, so a chunked body without a length is cut off at the cap too; either answers 413. Must be ≥ 1. |
| `EXAMLOPS_DATAPLANE_DRAIN_SECONDS` | `20` | ADR 0131 dataplane service shutdown drain — ONE budget with one deadline, `t0 + budget`, where `t0` is SIGTERM or the lifespan exit, whichever comes first. From `t0`: `/ready` and push answer 503, the stream supervisor stops its connectors (at SIGTERM itself), in-flight pushes finish (uvicorn's `timeout_graceful_shutdown` is set to the same budget), drift, telemetry spool and inference client close, the stream leader leases are released, the pull scheduler stops. Every step uses only the time left; past the deadline only the two flushes get 0.5 s each, so shutdown ends within the budget plus about a second. **Keep it below the orchestrator's stop grace period** (Compose sets `stop_grace_period: 30s` for `dataplane`; Kubernetes' default `terminationGracePeriodSeconds` is 30), or a SIGKILL lands mid-flush. Must be ≥ 0. |
| `EXAMLOPS_DATAPLANE_INFERENCE_URL` | `http://ray-serving:8001` | ADR 0131 stream ingress — base URL of the inference pipeline the dataplane's streams call (`examlops.dataplane.streams.client.RayPipelineClient`, `POST /infer-pipeline/infer`). |
| `EXAMLOPS_DATAPLANE_INFERENCE_MAX_CONNECTIONS` | `100` | ADR 0131 stream ingress — connection-pool ceiling of the one shared inference client per dataplane process. A non-integer falls back to the default with a warning. |
| `EXAMLOPS_DATAPLANE_TELEMETRY_QUEUE_MAX` | `1000` | ADR 0131 stream telemetry — capacity of the bounded, drop-on-full spool between a stream's reply and the telemetry write (`examlops.dataplane.streams.telemetry.TelemetrySpool`). A full spool drops the record and counts it in `dataplane_stream_telemetry_dropped_total`. Unset, non-integer or non-positive falls back to the default. |
| `EXAMLOPS_DATAPLANE_DRIFT_WINDOW` / `EXAMLOPS_DATAPLANE_DRIFT_THRESHOLD` / `EXAMLOPS_DATAPLANE_DRIFT_COOLDOWN_SECONDS` | `50` / `0.5` / `300` | ADR 0131 stream drift (`examlops.dataplane.streams.drift.DriftAggregator`) — the rolling window of model outcomes per model, the model-failure rate over a full window that trips a retrain, and the cooldown between retrains of one model (a model's own `drift_auto_retrain.cooldown_s` wins). Only `model` outcomes count as failures. The threshold is clamped into (0, 1] and the window to at least 1. |
| `EXAMLOPS_DATAPLANE_DRIFT_DATASET` | unset | ADR 0131 stream drift — the dataset a drift retrain uses for a model with no `drift_auto_retrain` row. Unset ⇒ such a trip is suppressed (`retrain_suppressed`, reason `no_config`). A use-case name, so it is set by the deployment, never hard-coded (ADR 0094). |
| `EXAMLOPS_DATAPLANE_DRIFT_BACKEND` | `dataplane` | ADR 0131 stream drift — the dataset backend passed to the retrain trigger. |
| `EXAMLOPS_DATAPLANE_LEGACY_DATAPLANE_BUS_UUID` | unset (off) | ADR 0131 stream catalog — truthy (`1`/`true`/`yes`/`on`) makes the pack sync also derive one legacy `dataplane-bus` stream per model from the model YAML's `dataplane_bus_uuid` key (`examlops.dataplane.streams.bindings`). Off by default: only `inference.streams` entries become streams. For the Dataplane bus cutover window. |
| `EXAMLOPS_DATAPLANE_LAG_REFRESH_SECONDS` | `10` | ADR 0131 stream consumer lag — how often a Kafka stream's poll loop asks the broker for a **real** high watermark per assigned partition, instead of librdkafka's cached one. The cache is refreshed only by fetch responses, so a partition that is paused (by an operator, or parked in a retry backoff) freezes it and `dataplane_stream_consumer_lag` reads 0 exactly while a backlog builds. Between refreshes the cached value still serves, and the two are combined with `max`. Lower it for a tighter lag signal at the cost of one metadata request per partition per interval; a negative or unparseable value keeps the default. However low you set it, one refresh pass is bounded to ~2 s of real time and resumes at the next partition on its following turn, so a slow or unreachable broker can never keep the consumer out of `poll()` (its group heartbeat) for one timeout per partition. That bound is a fixed constant on purpose — it protects group membership, which is correctness, not a metrics setting. |
| `EXAMLOPS_DATAPLANE_DLQ_RETENTION_DAYS` | `7` | ADR 0131 stream dead letters (`examlops.dataplane.streams.dlq.prune`) — days a row of `dataplane_stream_dead_letters` is kept. The dataplane service's pull scheduler deletes older rows at most once an hour and writes one `dataplane_stream_dlq_pruned` audit event per run that deleted any. A non-integer or a value below 1 falls back to the default (with a warning). A stored payload exists only on a stream whose `options.dlq_store_payload` is true, is at most 256 KiB and is redacted (secrets and personal data) before it is written. |
| `EXAMLOPS_DATA_CONTRACT_GATE` | `enforce` | **A5** training-gate mode (ADR 0005 clause 2): `enforce` fails the run closed on an error-severity contract violation, `warn` records it and continues, `off` skips. An unrecognised value falls back to `enforce` — a typo must not quietly disable a gate that fails closed by design. Dummy runs and datasets with no contract are skipped with a recorded reason. |
| `EXAMLOPS_CONTRACT_ENGINE` | `auto` | **A5** data-contract engine (ADR 0005 clause 1): `auto` validates with Pandera when it is importable — a row-level failure then names the rows that failed — and with the pandas-only engine otherwise; `python` forces the pandas-only engine. The two reach the same verdict on every check (`tests/unit/test_data_contract_engines.py`), so there is deliberately no value that requires Pandera. An unrecognised value warns and means `auto`. Every verdict records its engine (`exa data validate` output, `--json` `engine`, `data_quality_checks.engine`). |
| `EXAMLOPS_ASSET_ORCHESTRATOR` | `local` | **A4** which engine materializes an asset (ADR 0036): `local` runs the production function in-process, `scheduler` submits it through the phase-23 HPC seam, `prefect` runs it as a Prefect flow run (falls back to local, recorded, when no Prefect API is configured or reachable). An unrecognised value falls back to `local` — a typo must leave the asset built, not route it to an engine nobody configured. Override per run with `exa assets materialize --orchestrator`. |
| `EXAMLOPS_ASSET_PREFECT_RETRIES` | `0` | **A4** Prefect task retries for a failed asset production function under `--orchestrator prefect` (ADR 0036). Opt-in: a build that failed halfway is not known to be safe to repeat. |
| `EXAMLOPS_ASSET_PREFECT_RETRY_DELAY` | `10` | **A4** seconds between those retries. |
| `EXAMLOPS_JOB_SCRIPT_DIR` | `$XDG_CACHE_HOME/examlops/jobs` | Where generated scheduler job scripts are kept (mode 0700): asset builds (`exa assets materialize --orchestrator scheduler`, ADR 0036) and reindex jobs (`exa embedding reindex --scheduler`, ADR 0043). They are the record of what each job ran. Never the adapter's working directory, which for the mock is inside the repository. The job's interpreter, repo and work dir come from `EXAMLOPS_HPC_REMOTE_PYTHON` / `_REPO` / `_WORKDIR`; with none set it uses the submitting interpreter. |
| `EXAMLOPS_ASSET_JOB_DIR` | unset | **Deprecated** alias of `EXAMLOPS_JOB_SCRIPT_DIR` (its v0.53.0 name, when asset builds were the only scheduler jobs). Still honoured; `EXAMLOPS_JOB_SCRIPT_DIR` wins when both are set. |
| `EXAMLOPS_ENCODER_REGISTRY` | `local` | **A6** where encoders are recorded (ADR 0043 clause 1). `local` keeps them in `platform.db`. `mlflow` makes the MLflow encoder registry the record: one run per encoder with an `encoder.json` card artifact, published before `platform.db` indexes it. An unknown value is an error. Publish existing encoders with `exa embedding migrate`. |
| `EXAMLOPS_ENCODER_EXPERIMENT` | `examlops-encoders` | **A6** the MLflow experiment that holds the encoder registry. |
| `EXAMLOPS_ENCODER_MLFLOW_URI` | `MLFLOW_TRACKING_URI` | **A6** a separate MLflow for the encoder registry; unset uses the platform's tracking server. |
| `EXAMLOPS_REINDEX_ORCHESTRATOR` | `inline` | **A6** where a blue-green reindex runs (ADR 0043 clause 4): `inline` in the calling process; `scheduler` runs it as a job on the phase-23 HPC scheduler (the case the clause names, large corpora). That job continues the same reindex row and applies `--recall` / `--recall-floor` there. An unrecognised value falls back to `inline`; an unreachable scheduler, or a library caller's `recall_fn`, runs it inline and records `inline-fallback`. Override per run with `exa embedding reindex --scheduler` / `--inline`. |
| `EXAMLOPS_INFERENCE_EMBEDDING_DIM` | unset | **A5** inference-gate embedding width. When set, a request whose embedding is the wrong length is refused with 422. Never defaulted — an embedding width is a fact about a use case, not a platform constant. |
| `EXAMLOPS_OPENLINEAGE_URL` | unset (no-op) | **A2** OpenLineage receiver (ADR 0004): every lineage emit path POSTs its run event to `<url>/api/v1/lineage`; unset ⇒ no HTTP, and `platform_db` is still written. With the Compose `lineage` profile (Marquez) use `http://marquez:5000` for containers — set it in `platform/infra/docker-compose/.env`, which the control plane loads and the dataplane and dashboard receive — and `http://localhost:15050` for host processes (`exa`, the flows `exa pipeline deploy` serves). Fail-open: an unreachable receiver is logged, never raised. |
| `MARQUEZ_DB_PASSWORD` | `marquez` | Compose `lineage` profile: the password of Marquez's own Postgres (`marquez-db`), which publishes no port and shares a network only with Marquez. Set it before the first `up`; the database keeps the password it was created with. |
| `OTEL_SDK_DISABLED` | `true` | **C1** master tracing switch; `false` enables OTLP export of GenAI spans. |
| `EXAMLOPS_GENAI_CAPTURE_CONTENT` | unset (off) | **C1** capture prompt/completion content on spans (redactor-gated, D8). |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | unset | **C1** OpenTelemetry's comma-separated convention opt-in. Listing `gen_ai_latest_experimental` switches GenAI spans wholesale to the current conventions: `gen_ai.provider.name` instead of `gen_ai.system`, the registry's operation names (`text_completion`, `chat`, `invoke_agent`, `execute_tool`, …), span names `"<operation> <model>"`, and structured `gen_ai.input.messages`/`gen_ai.output.messages` for captured content. Spans then report `examlops.semconv.version = genai@<commit>`, the conventions revision the names were checked against. Absent it, the pinned 1.27.0 attributes keep being emitted unchanged. |
| `EXAMLOPS_VAULT_ADDR` / `EXAMLOPS_SECRETS_KEY` | unset | **D7** secrets — OpenBao address; else Fernet-local store keyed by `EXAMLOPS_SECRETS_KEY` (or `DASHBOARD_SECRET_KEY`). |
| `EXAMLOPS_VAULT_STRICT` | unset (fall back) | When truthy, a configured-but-unreachable OpenBao/Vault **fails the read** instead of silently downgrading to the local store or an environment variable. Set it wherever Vault is the system of record. |
| `EXAMLOPS_SECRET_TENANTS` | unset | **D7** per-tenant secret path-prefix scoping. |
| `EXAMLOPS_MULTITENANCY` | unset (off) | **D6** RBAC — off ⇒ every `authz.check` allows (single-tenant compat); truthy ⇒ default-deny enforcement. |
| `RAY_SHADOW_WORKERS` | `2` | Threads for **shadow mirroring** (ADR 0024 clause 1). A pool of its own — never the prediction pool — so a shadow slower than the champion cannot take threads from the traffic it is shadowing. |
| `RAY_SHADOW_MAX_INFLIGHT` | `16` | Cap on concurrent shadow requests. Excess is **dropped and counted** (`examlops_shadow_total{status="dropped"}`), never queued: an unbounded queue would turn a slow shadow into unbounded memory growth on a serving replica. |
| `RAY_SHADOW_CONFIG_TTL` | `30` | Seconds the `shadow_config` lookup is cached. It is consulted per request, so a SQLite read per prediction would put the shadow's cost on the production path. |
| `EXAMLOPS_GUARDRAIL_MODE` | `monitor` | **D8** guardrails at the **gateway** boundary (ADR 0026 clause 3) — every `GatewayClient.chat` is scanned on the way in and on the way out. `monitor` (default) records findings to `guardrail_events` and changes nothing a caller can observe; `enforce` blocks injection/toxicity and redacts PII and secrets, failing closed on a scanner error; `off` skips the scan at no cost. An unrecognised value falls back to `monitor` rather than off, so a typo cannot silently disable the boundary. |
| `EXAMLOPS_GUARDRAIL_PII_NER` | unset (off) | **D8** PII detection (ADR 0026 clause 1) — truthy enables Presidio as an **additive** NER supplement to the regex PII detectors (adds `PERSON`/`LOCATION`/`NRP`, entity types a regex cannot find; the regex detectors keep email/phone/SSN/credit-card/IBAN/IP regardless). Needs `examlops[guardrails-presidio]` and a separately-installed spaCy model; unavailable or misconfigured degrades to regex-only, logged once, never raised. |
| `EXAMLOPS_GUARDRAIL_PRESIDIO_MODEL` | `en_core_web_lg` | The spaCy model Presidio NER loads when `EXAMLOPS_GUARDRAIL_PII_NER` is on. The small model (`en_core_web_sm`) works and is what the opt-in test suite verifies against, but is measurably less accurate — verified mistagging a person's name and a street address as `ORGANIZATION` in the same sentence a `PERSON`/`LOCATION` pair it got right moments earlier. |
| `EXAMLOPS_SIGNING_KEY` | unset | **D3** supply-chain — the **legacy** HMAC model-signing key (else D7 secret `model-signing/key`). Used to sign only when no Ed25519 key is configured, and to verify versions signed that way. Every verifier holding it could also forge, so prefer Ed25519. |
| `EXAMLOPS_SIGNING_PRIVATE_KEY_FILE` | unset | Ed25519 private key (PEM, `openssl genpkey -algorithm ed25519 -out signing.pem`) used by `exa models sign` and by the training pipeline at registration; else the D7 secret `model-signing/ed25519-private`. Only the signer holds it: never set it on Ray Serve. |
| `EXAMLOPS_SIGNING_PUBLIC_KEYS` | unset | The serving plane's trust bundle: comma-separated base64 raw Ed25519 public keys (`exa models sign` prints the signer's). A signature verifies only under one of these. Keep a retiring key listed until everything it signed is re-signed. |
| `EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE` | unset | The same trust bundle as a file of one or more PEM public keys (`openssl pkey -in signing.pem -pubout`). Both sources are read. |
| `EXAMLOPS_SIGN_AT_REGISTRATION` | `auto` | Whether the training pipeline signs each version it registers: `auto` signs when a signing key is configured, `required` fails the run when it cannot sign (so an unsigned version never reaches a serving plane that enforces), `off` never signs. |
| `EXAMLOPS_SERVING_BACKEND` | `ray-compose` | **E1** serving backend — `ray-compose` (default) or `kserve-k8s`. |
| `EXAMLOPS_VECTOR_BACKEND` | `sqlite` | **B5** vector store — `sqlite` (persistent fallback) or `pgvector`. |
| `EXAMLOPS_PGVECTOR_DSN` | unset | **B5** Postgres+pgvector DSN (required for the `pgvector` backend). |
| `EXAMLOPS_PGVECTOR_SCHEMA` | unset (`public`) | **B5** schema for the pgvector registry and collection tables; a plain identifier. Lets two instances share one server. |
| `EXAMLOPS_PGVECTOR_STATEMENT_TIMEOUT_MS` | `10000` | **B5** per-search statement timeout on pgvector, so a runaway scan cannot hold a pooled connection. |
| `EXAMLOPS_PGVECTOR_CONNECT_TIMEOUT` | `5` | **B5** seconds to wait for the pgvector server before failing. |
| `EXAMLOPS_PGVECTOR_POOL_MAX` | `10` | **B5** pgvector connection-pool size per process (needs `psycopg-pool`; unpooled without it). |
| `EXAMLOPS_PGVECTOR_TEST_DSN` | unset | Opt-in DSN for the live pgvector test suite (`tests/unit/test_pgvector_store.py -m live`). |
| `EXAMLOPS_CARBON_POLICY_MARGIN_PP` | `5.0` | **ADR 0112 R-ec** — percentage points of carbon-agnostic emissions a carbon-weighing placement policy must beat the best simple baseline by before it may place on carbon. |
| `EXAMLOPS_CARBON_POLICY_RETEST_DAYS` | `90` | **R-ed** — an evaluation older than this no longer licenses a carbon policy (re-test overdue). |
| `EXAMLOPS_CARBON_POLICY_RETIRE_BELOW_PCT` | `2.0` | **R-ed** — a shipped carbon policy saving less than this share of emissions is retired: carbon leaves placement. |
| `EXAMLOPS_CARBON_POLICY_GATE` | `enforce` | `enforce` applies the R-ec/R-ed gate; `warn` lets the requested policy run and records what `enforce` would have done. |
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
| `EXAMLOPS_HPC_WORKDIR` | see the note | Where an adapter keeps its **own** job files (logs, per-job folders), as `<it>/<kind>`. Set it on a filesystem the compute nodes can see; CI wants a temporary directory. Unset, the **mock** scheduler uses the cache directory — its historical default was a folder inside the installed package, so a mock run wrote generated scripts and pickles holding absolute local paths into the checkout. A real **slurm**/**flux** adapter keeps its relative default (`slurm_jobs` / `flux_jobs`): those paths reach `sbatch --output` and must resolve on the cluster, so the submit directory is the operator's to choose. Distinct from `EXAMLOPS_JOB_SCRIPT_DIR`, which holds the generated *scripts*. |
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
| `MLFLOW_TRACKING_USERNAME` / `MLFLOW_TRACKING_PASSWORD` | unset | The credential every MLflow client sends when the server requires authentication (plan P3.6): MLflow's own SDK reads them, and the platform's raw-HTTP callers (control plane, dashboard, agent, CLI) send the same Basic header through `examlops.service_auth`. Put them in `.env`; every service that loads it picks them up. |
| `MLFLOW_TRACKING_TOKEN` | unset | Alternative to the pair above: a bearer token (for an MLflow behind a token-checking proxy). Takes precedence. |
| `MLFLOW_AUTH` | unset | `basic` starts MLflow with its basic-auth app (Compose). Needs `MLFLOW_ADMIN_PASSWORD`; the server refuses to start without one rather than falling back to MLflow's public default `admin`/`password1234`. |
| `MLFLOW_ADMIN_USERNAME` / `MLFLOW_ADMIN_PASSWORD` | `admin` / unset | The basic-auth app's admin account, written into its config at start. Its user store lives on the `mlflow_auth` volume. |
| `MLFLOW_DEFAULT_PERMISSION` | `READ` | What an authenticated MLflow user may do with resources they were not granted explicitly (`READ`, `EDIT`, `MANAGE`, `NO_PERMISSIONS`). |
| `MLFLOW_FLASK_SERVER_SECRET_KEY` | unset | Secret for the basic-auth app's session and CSRF protection; set a random value when `MLFLOW_AUTH=basic`. |
| `MLFLOW_S3_ENDPOINT_URL` | `http://localhost:19000` | MinIO S3-compatible endpoint for MLflow artifact storage |
| `AWS_ACCESS_KEY_ID` | `minioadmin` | MinIO access key |
| `AWS_SECRET_ACCESS_KEY` | `minioadmin` | MinIO secret key |
| `PREFECT_API_URL` | `http://localhost:14200/api` | Prefect server API endpoint |
| `PREFECT_API_AUTH_STRING` | unset | `user:password` for a self-hosted Prefect that requires authentication (plan P3.6). In Compose, setting it in `.env` turns authentication **on** at the server and every client sends it; unset, Prefect is open as before. The server-side variable is exported only from a non-empty value, because Prefect treats `PREFECT_SERVER_API_AUTH_STRING=""` as "require an empty password" and would refuse every client. Also set it where the Prefect worker runs. |
| `PREFECT_API_KEY` | unset | A Prefect Cloud API key; sent as a bearer when no auth string is set. |
| `PREFECT_DEPLOYMENT_NAME` | `training_flow/examlops-dispatch` | Prefect deployment every control-plane retrain is dispatched to (`POST /retrain`, the approval gate, ModelZoo auto-retrain). `exa pipeline deploy` registers it and serves its runs; its parameters are exactly `model_name`, `dataset_cls_name`, `is_dummy`, `backend_name`. `GET /health` reports whether it exists (`dispatch`). |

---

## Dataset Backends (Phase 1)

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_DATA_BUCKET` | `examlops-data` | S3/MinIO bucket consulted by `MinIOBackend` |
| `EXAMLOPS_DATA_S3_ENDPOINT` | unset | Dedicated endpoint for the **dataset** object store (e.g. a Day-0 store on S3), separate from the platform MinIO holding MLflow artifacts/models. Unset ⇒ the minio dataset backend falls back to `MLFLOW_S3_ENDPOINT_URL` (single shared instance, legacy behaviour). |
| `EXAMLOPS_DATA_S3_ACCESS_KEY` / `EXAMLOPS_DATA_S3_SECRET_KEY` | unset | Credentials for the dedicated dataset store. Unset ⇒ fall back to `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. |

Selection is per-pipeline-run via `--backend` CLI flag or `backend_name` Prefect flow parameter. Omitting falls back to the default Zenodo flow.

---

## Ray Serve (Phase 3)

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_STAGE` | `Production` | Default MLflow alias when no per-request alias or version is specified |
| `RAY_NUM_REPLICAS` | `2` | Replicas of the model server; the floor when autoscaling is on |
| `RAY_AUTOSCALE_MAX_REPLICAS` | unset | Set it and Ray autoscales the model server between `RAY_NUM_REPLICAS` and this ceiling (a lower value is raised to the floor). Every replica loads the whole hot set, so size it by memory too |
| `RAY_AUTOSCALE_TARGET_ONGOING` | `5` | In-flight requests per model-server replica that the autoscaler aims for |
| `RAY_AUTOSCALE_UPSCALE_DELAY_S` | `30` | Seconds demand must stay above target before a replica is added |
| `RAY_AUTOSCALE_DOWNSCALE_DELAY_S` | `300` | Seconds demand must stay below target before a replica is removed |
| `RAY_SERVE_PORT` | `8001` | Ray Serve internal HTTP port (host-exposed as `18001`) |
| `RAY_SERVE_GRPC_PORT` | `8081` | Port of the model server's [Open Inference Protocol v2 gRPC](../components/ray-serve.md#open-inference-protocol-v2-over-grpc) service (host `18081` in Compose); `0` turns it off. It binds `RAY_SERVE_HOST`. |
| `RAY_SERVE_GRPC_MAX_MESSAGE_MB` | `8` | Largest gRPC request or answer the model server accepts; larger ones get `RESOURCE_EXHAUSTED`. |
| `RAY_SERVE_HOST` | `0.0.0.0` | Address the HTTP port binds. The [workload-identity overlay](../guides/workload-identity.md#every-hop-to-the-model-server-mutual-tls) sets `127.0.0.1`, so the mutual-TLS sidecar in the model server's own network namespace is the one way in and `18001` is not published. |
| `RAY_PRELOAD_ALIASES` | `Production,Canary,Staging` | Comma-separated MLflow aliases pre-loaded into the hot set at startup and on reload |
| `RAY_VERSION_CACHE_SIZE` | `8` | LRU cache size for raw-version (`/predict` with `version=`) lookups |
| `RAY_RELOAD_POLL_SECONDS` | `60` | Background MLflow alias-poll interval in seconds; `0` disables polling. With a serving snapshot published (`RAY_SNAPSHOT_MODE=auto`), replicas do not poll MLflow at all; this interval applies only while no snapshot exists. |
| `RAY_SNAPSHOT_MODE` | `auto` | `auto`: replicas serve the serving snapshot the control plane publishes (ADR 0127), and the inference router takes traffic splits from it; both fall back to their own MLflow scan or table read only while no snapshot exists. `off`: the legacy paths only. |
| `RAY_SNAPSHOT_POLL_SECONDS` | `2` | How often a replica looks for a newer serving-snapshot generation (minimum 0.5). |
| `RAY_ARTIFACT_CACHE` | unset (off; Compose: a volume) | Directory of content-addressed local copies of the model versions this replica serves. Each version is fetched once, by version, and its files' SHA-256 digests are re-checked on every use (a corrupted copy is refetched). With the serving snapshot, this is what lets a replica restart and serve while MLflow is down. Signature verification (`EXAMLOPS_SERVING_VERIFY`) runs against the cached bytes. |
| `RAY_ARTIFACT_CACHE_MAX_GB` | `20` | Size bound for `RAY_ARTIFACT_CACHE`; least-recently-used versions are evicted beyond it (`0` = unbounded). |
| `RAY_SNAPSHOT_CACHE` | `<tmp>/examlops-serving-snapshot.json` (Compose: a volume) | The replica's last-known-good snapshot, served when NATS, the database and the control plane are all unreachable at start. Used only when no source answers; a reachable database that has no snapshot is never overridden by an old file. |
| `RAY_SERVE_ADMIN_TOKEN` | unset (admin routes closed) | Bearer required by Ray Serve's admin routes: `POST /reload`, `/reload/{model}` and `/infer-pipeline/traffic-rules/{model}`. Unset or a placeholder makes them answer 503; inference is unaffected, and alias moves still reload within `RAY_RELOAD_POLL_SECONDS`. Callers send it from the same variable: `exa serve reload` / `exa serve traffic` (config key `ray_serve_admin_token`), the agent, and the pipeline's promotion webhook. |
| `EXAMLOPS_SERVING_VERIFY` | `warn` | Verify-before-load for Ray Serve: `off`, `warn` (check every load and audit a failure as `model_verify_failed`, never refuse) or `enforce` (refuse an unsigned, tampered, untrusted or unverifiable artifact and keep serving the last-known-good version). Serving loads exactly the bytes it verified, against the signature record the serving snapshot carries. `enforce` needs every served version signed (at registration, or `exa models sign`) and the signer's public key in `EXAMLOPS_SIGNING_PUBLIC_KEYS`. The default was `off` until registration signed models (plan P4.10). |
| `MINIO_SERVING_ACCESS_KEY` / `MINIO_SERVING_SECRET_KEY` | unset | Read-only MinIO credential for Ray Serve on `mlflow-artifacts`, created by `minio-init` when both are set. Serving only downloads models; unset falls back to the root credential. |
| `MINIO_MLFLOW_ACCESS_KEY` / `MINIO_MLFLOW_SECRET_KEY` | unset | MLflow server's credential: read-write on `mlflow-artifacts`, nothing else (no bucket creation, no admin). Created by `minio-init`; unset falls back to root. |
| `MLFLOW_DB_USER` / `MLFLOW_DB_PASSWORD` | `mlops` / `POSTGRES_PASSWORD` | MLflow's own Postgres role (plan P3.4). Set both and `postgres-init` creates the role, makes it the owner of the `mlflow` database (existing tables included), and shuts every other role out. Change the password and re-run `up` to rotate it. |
| `PREFECT_DB_USER` / `PREFECT_DB_PASSWORD` | `mlops` / `POSTGRES_PASSWORD` | Prefect's own Postgres role on the `prefect` database, the same way. |
| `DASHBOARD_DB_USER` / `DASHBOARD_DB_PASSWORD` / `DASHBOARD_DB_NAME` | `mlops` / `POSTGRES_PASSWORD` / `mlflow` | The dashboard's own Postgres role and database. A dashboard role needs its own database (`DASHBOARD_DB_NAME=dashboard`); `postgres-init` refuses one on `mlflow`. Moving existing tables: docs/guides/production-hardening.md. The backup sidecar follows `DASHBOARD_DB_NAME`. |
| `MINIO_DASHBOARD_ACCESS_KEY` / `MINIO_DASHBOARD_SECRET_KEY` | unset | Dashboard's credential: read-write on `dashboard-model-docs` and the project-storage bucket. |
| `MINIO_BACKUP_ACCESS_KEY` / `MINIO_BACKUP_SECRET_KEY` | unset | Backup sidecar's credential: **read-only** on `mlflow-artifacts`, `dashboard-model-docs` and project storage. Restoring objects is an operator's action with the root credential. |
| `MINIO_NOTEBOOK_ACCESS_KEY` / `MINIO_NOTEBOOK_SECRET_KEY` | unset | What JupyterHub hands every spawned notebook: read-write on `mlflow-artifacts` and project storage. A notebook runs arbitrary user code, so it never gets root once this is set. |
| `MINIO_DATAPLANE_ACCESS_KEY` / `MINIO_DATAPLANE_SECRET_KEY` | unset | ADR 0130 dataplane service's credential: read-write on `EXAMLOPS_DATA_BUCKET` only (`EXAMLOPS_DATA_S3_ACCESS_KEY`/`_SECRET_KEY` in the container), created by `minio-init`; unset falls back to root, same fallback shape as the dashboard's. |
| `MLFLOW_ALLOWED_HOSTS` | `mlflow`, `localhost`, `127.0.0.1`, `$PUBLIC_HOST` (each with any port) | `Host` headers the MLflow server accepts (was `*`). Add every name clients use to reach MLflow. |
| `MLFLOW_CORS_ALLOWED_ORIGINS` | the dashboard's origins on `localhost`, `127.0.0.1` and `$PUBLIC_HOST` | Browser origins allowed to call MLflow cross-origin (was `*`). |
| `TRAFFIC_RULES_TTL_SECONDS` | `30` | TTL of the inference-pipeline router's per-replica traffic-split cache. Split changes written by the ingress or `exa serve traffic` (other processes) apply within this window; negative results are cached too. |
| `RAY_SERVE_RELOAD_URL` | unset | Ray Serve URL for the Prefect promotion webhook (`POST /reload/{model_id}`); unset disables the webhook |
| `RAY_METRICS_EXPORT_PORT` | `8080` | Prometheus metrics export port used by Ray |
| `RAY_GAUGE_REFRESH_SECONDS` | `5` | How often each model-server replica republishes its gauges (`models_loaded`, the applied snapshot generation, the served version per alias). Ray 2.55 clears a gauge once it is collected, so a gauge set only at load time disappears from Prometheus. `0` disables; keep it under Ray's 10 s report interval |
| `EXAMLOPS_LOADTEST_TOKEN` | unset | Bearer credential `exa serve loadtest` sends, e.g. a serving-gateway virtual key. Read from the environment so it never appears on the command line or in `ps`. Unset, the command sends `EXAMLOPS_SERVING_TOKEN` instead, but only when its target is the configured `ray_serve` URL. |
| `EXAMLOPS_SERVING_TOKEN` | unset | `exa`'s serving credential (config key `serving_token`): a [serving-gateway](../guides/serving-gateway.md#credentials) virtual key or an IdP access token. It is sent only on requests under the configured `ray_serve` URL, and never over a credential a request already carries. Point `ray_serve` at the gateway to use it. |

---

## Control Plane (Phase 4 + 11 + 12)

| Variable | Default | Purpose |
|---|---|---|
| `CONTROL_PLANE_PORT` | `8002` | HTTP port for the retrain API (host-exposed as `18002`) |
| `CONTROL_PLANE_TOKEN` | unset | Legacy operator bearer credential. It maps to principal `legacy`, tenant `default`, with `read` and `write` scopes; optional when the structured credential map is configured. |
| `CONTROL_PLANE_CREDENTIALS_JSON` | unset | JSON object keyed by bearer secret. Each value requires `principal`, `tenant`, and non-empty `scopes` drawn from `read`, `write` (every mutation), and the narrow action scopes `retrain`, `approve`, `changes` and `admin` (plan P3.2). Malformed input, an unknown scope, or a secret duplicated from `CONTROL_PLANE_TOKEN` fails all bearer authentication closed. |
| `CONTROL_PLANE_LEGACY_TOKEN` | `on` | Whether the shared, all-scopes `CONTROL_PLANE_TOKEN` is accepted: `on`; `warn` (accepted, each use counted in `control_plane_legacy_token_uses_total` and the caller logged at most once a minute); `off` (refused with 403). Any other value is `off` and fails the startup check. See [Retiring the shared legacy token](../components/control-plane.md#retiring-the-shared-legacy-token). |
| `CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON` | unset | SPIFFE ID → `principal`, `tenant`, `scopes` (the same scopes as `CONTROL_PLANE_CREDENTIALS_JSON`). A workload presenting a valid JWT-SVID for one of these IDs acts with those scopes. See [Workload identities](../components/control-plane.md#workload-identities-spiffe). |
| `CONTROL_PLANE_SPIFFE_AUDIENCE` | `control-plane` | The audience a JWT-SVID must name to be accepted by the control plane. |
| `CONTROL_PLANE_TOKEN_FILE` | unset | A file holding the bearer credential a platform service sends the control plane, read on every call: a JWT-SVID that spiffe-helper keeps fresh (ADR 0125), or a rotated Secret mounted as a file. Wins over `CONTROL_PLANE_TOKEN`; missing or empty falls back to it. Read by the CLI and autopilot follower, the bus bridge, the dashboard and the agent. |
| `EXAMLOPS_SPIFFE_TRUST_DOMAIN` | unset (off) | The SPIFFE trust domain workload identities belong to, e.g. `examlops.internal`. A JWT-SVID for another trust domain is refused. |
| `EXAMLOPS_SPIFFE_BUNDLE` | unset | The trust bundle's JWT keys: a JWKS or SPIFFE bundle file (`spire-server bundle show -format spiffe`), or the bundle set spiffe-helper writes (`jwt_bundle_file_name`, only this trust domain's keys are used), re-read when it changes;, the JSON itself, or an `https://` URL of SPIRE's OIDC discovery keys. |
| `SPIFFE_WORKLOADS` | `control-plane dashboard skipper autopilot dataplane-bus-bridge` | Compose identity overlay: the workloads `spire-register` registers, each `spiffe://<trust domain>/<name>` selected by the container label `examlops.spiffe=<name>`. See [Workload identity](../guides/workload-identity.md). |
| `SPIFFE_JWT_SVID_TTL` | `300` | Compose identity overlay: JWT-SVID lifetime in seconds for newly registered workloads. |
| `SPIRE_SERVER_MEM_LIMIT` / `SPIRE_AGENT_MEM_LIMIT` | `256m` / `128m` | Compose identity overlay: memory ceilings of the SPIRE server and agent. |
| `DATAPLANE_BUS_BRIDGE_CONTROL_PLANE_TOKEN` / `AUTOPILOT_CONTROL_PLANE_TOKEN` / `AGENT_CONTROL_PLANE_TOKEN` / `DASHBOARD_CONTROL_PLANE_TOKEN` | unset (→ `CONTROL_PLANE_TOKEN`) | Compose: the control-plane bearer each of those services sends. Issue each its own credential in `CONTROL_PLANE_CREDENTIALS_JSON` with only the scopes it needs (the bridge: `retrain`), so the audit trail names the service and a compromised one cannot act beyond its job. |
| `CONTROL_PLANE_URL` | `http://control-plane:8002` | Control plane URL used by the dataplane simulator and CI notify script |
| `CONTROL_PLANE_DB` | `PLATFORM_DB`, else `/data/approvals.db` | SQLite path for commands, approvals, admission, outbox, and ModelZoo state. Inside the service this becomes the shared `PLATFORM_DB`, keeping transactions and the relay on one file. |
| `CONTROL_PLANE_COMMAND_LEASE_SECONDS` | `60` | How long a dispatch claim lasts. When the replica holding it stops (a crash, a killed pod), another replica takes the command over after this and dispatches it again with the same Prefect idempotency key, so no second run is created. Keep it above `CONTROL_PLANE_DISPATCH_BUDGET_SECONDS` (8). Was 300. |
| `CONTROL_PLANE_RETRAIN_LOCK_SECONDS` | command lease (minimum `60`) | Coordinator lease for one tenant/model/dataset retrain dispatch. |
| `CONTROL_PLANE_POLLER_LEASE_SECONDS` | `30` (minimum `3`) | Coordinator lease used to elect the singleton ModelZoo poller. |
| `CONTROL_PLANE_EVENT_RELAY_SECONDS` | `1` | In-process outbox relay interval; `0` disables it. |
| `CONTROL_PLANE_EVENT_RELAY_BATCH_SIZE` | `100` | Maximum outbox rows claimed per relay pass. |
| `CONTROL_PLANE_SNAPSHOT_SECONDS` | `60` | The serving-snapshot projector's full recompile interval, which catches changes made directly in MLflow. It also recompiles within a tick of any `serving.*` or `model.alias_changed` event. `0` disables the projector; replicas then keep polling MLflow themselves. |
| `CONTROL_PLANE_DRIFT_EVAL_SECONDS` | `60` | How often the control plane scores every model's prediction drift and announces a status change as `drift.status_changed` (and `alert.drift` for WARNING/CRITICAL). `0` disables the evaluator; `exa drift status` is unaffected. |
| `CONTROL_PLANE_SNAPSHOT_TICK_SECONDS` | `1` | How often the projector checks the outbox for a serving-relevant change (minimum 0.2). |
| `EXAMLOPS_SNAPSHOT_MLFLOW_TIMEOUT` | `10` | Seconds per MLflow REST call while compiling the serving snapshot. A failed compile keeps the previous generation in force. |
| `CONTROL_PLANE_STARTUP_RECHECK_SECONDS` | `10` | While a startup check is failing, `/health` and `/readyz` re-run the checks at most this often (minimum 1), so a dependency that was briefly down at boot does not keep the replica unready. Passing checks are never re-run. |
| `CONTROL_PLANE_EVENT_BACKBONE_STATS_SECONDS` | `30` | How often the relay reads consumer lag and dead-letter depth from JetStream for `/metrics` (minimum 5; only with `EXAMLOPS_EVENT_PUBLISHER=nats`). |
| `CONTROL_PLANE_ADMISSION_RETRY_AFTER` | `30` (minimum `1`) | `Retry-After` seconds on the HTTP 429 returned when a retrain or approval dispatch is refused because the tenant's admission capacity (`EXAMLOPS_ADMISSION_PER_TENANT` / `_MAX_RUNNING`) is full. Retry with the same `Idempotency-Key`. |
| `CONTROL_PLANE_SEPARATION_OF_DUTIES` | `true` | The principal that filed an approval (`POST /api/changes`) cannot approve it (403); another principal must. The shared `CONTROL_PLANE_TOKEN` is exempt — every holder is principal `legacy`, so requester and approver are indistinguishable — and `GET /health` → `runtime.separation_of_duties` says `enforced-except-legacy-token`. Use structured or federated credentials for an enforced gate; `false` disables the rule. |
| `CONTROL_PLANE_WEBHOOK_MAX_BYTES` | `5242880` (5 MiB, minimum 1024) | Largest ModelZoo webhook body accepted (413 above it), checked against the declared length and the bytes actually received. |
| `CONTROL_PLANE_DISPATCH_BUDGET_SECONDS` | `8` | Total time one dispatch may spend on Prefect (deployment lookup + flow-run creation, including retries). Kept below the 10 s client timeout so the server answers before its callers give up; exhausted → 502. |
| `CONTROL_PLANE_PREFECT_CONNECT_TIMEOUT` / `CONTROL_PLANE_PREFECT_READ_TIMEOUT` | `3` / `5` | Per-attempt Prefect timeouts, each further capped by what is left of the dispatch budget. |
| `CONTROL_PLANE_PREFECT_ATTEMPTS` | `3` | Attempts per Prefect call on a 5xx or transport error, with full-jitter backoff that never sleeps past the budget. A 4xx is never retried; a POST is retried only when it carries an idempotency key. |
| `CONTROL_PLANE_COMMAND_WORKERS` | `1` | Worker threads that dispatch asynchronous commands (`POST /v1/retrain`). `0` disables them: commands are accepted and wait. Every platform retrain (CLI, autopilot, drift trigger, MCP, agent, bus bridge) goes through these commands, so with `0` no retrain is dispatched anywhere. Safe on several replicas — claims are atomic in the datastore. |
| `EXAMLOPS_RETRAIN_WAIT_SECONDS` | `30` | How long `exa retrain`, `exa pipeline hpo start`, `exa production deploy` and the MCP/agent retrain tools wait for a submitted retrain to be dispatched. A retrain still queued when it runs out is reported as accepted with its `command_id` (follow it with `exa commands show`), never as an error. Automated loops (drift trigger, autopilot, bus bridge) wait at most 5 s. |
| `CONTROL_PLANE_COMMAND_POLL_SECONDS` | `1` (minimum `0.1`) | How often each worker looks for due commands. |
| `CONTROL_PLANE_SETTINGS_TTL_SECONDS` | `5` | How long a control-plane replica acts on its cached copy of the shared runtime settings (`PUT /v1/modelzoo/config`) before re-reading them; the bound on how long replicas can disagree after a change. |
| `CONTROL_PLANE_COMMAND_MAX_ATTEMPTS` | `5` | Dispatch attempts before an asynchronous command is buried as `dead`. |
| `CONTROL_PLANE_COMMAND_BACKOFF_SECONDS` | `5` | Base of the exponential backoff between attempts (capped at 300 s). |
| `CONTROL_PLANE_RECONCILE_SECONDS` | `30` (minimum `1`) | How often the command workers poll each dispatched flow run until it reaches a terminal state (recorded as `run_state`, published once as `retrain.run_<state>`). |
| `CONTROL_PLANE_ORPHAN_RECHECK_SECONDS` | `60` | How often a replica asks again about the same buried command. It has to ask more than once: the run it is looking for appears *after* the burial, so a single early "no run" would be the wrong answer. |
| `CONTROL_PLANE_ORPHAN_CHECK_WINDOW_SECONDS` | `900` | How far back the reconcile sweep looks for commands it gave up on, to ask Prefect whether one of them left a training run behind ([ControlPlaneDeadCommandLeftARun](../runbooks/control-plane.md#controlplanedeadcommandleftarun)). `0` turns the check off; a replica asks about each command once, and the burying replica is often the one that cannot reach Prefect, which is why any replica may do it. |
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
| `DASHBOARD_JWT_SECRET` | **required** | HS256 signing secret for JWT tokens (minimum 32 characters). A key derived from it also signs the short-lived (1 h) URLs the dashboard issues for bundled model README images. |
| `CONTROL_PLANE_TOKEN` (dashboard) | unset | Fallback bearer the dashboard sends on control-plane reads (`/models*`, bundled images) when the Config page's encrypted `control_plane_token` secret is unset. The stored secret wins when both exist; it is never sent to the browser. |
| `DASHBOARD_SECRET_KEY` | **required** | Fernet key (base64-encoded, 44 characters) for secrets-at-rest encryption |
| `DASHBOARD_JWT_TTL_HOURS` | `12` | JWT token expiry in hours |
| `DASHBOARD_TRUSTED_PROXY` | unset | When truthy, the login/BFF rate limiter keys on the leftmost `X-Forwarded-For` address instead of the socket peer. Set ONLY behind a trusted reverse proxy — the header is spoofable when clients connect directly. |
| `DASHBOARD_GITLAB_INSECURE_TLS` | unset | Opt-out of TLS verification on the dashboard→GitLab modelzoo calls (private-CA escape hatch). Default verifies; prefer shipping the CA bundle. |
| `DASHBOARD_PORT` | `8099` | Dashboard HTTP port (host-exposed as `18099`) |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | `minioadmin` | The credential the dashboard itself uses for model-doc images and project storage. Compose sets these from `MINIO_DASHBOARD_ACCESS_KEY`/`_SECRET_KEY`, which is the least-privilege pair (read-write on its own buckets only); the defaults are dev-only and must not survive into a deployment. |
| `DASHBOARD_MINIO_BUCKET` | `dashboard-model-docs` | Bucket holding uploaded model documentation and README images. |
| `DASHBOARD_MAX_IMAGE_BYTES` | `5242880` (5 MiB) | Largest image the dashboard will accept for a model README. Raising it raises what an authenticated editor can store. |
| `DASHBOARD_IMAGE_URL_TTL_SECONDS` | `300` | Lifetime of the signed URL the browser gets for a bundled README image. Short on purpose: the URL is the capability. |
| `EXAMLOPS_REPO_URL` / `EXAMLOPS_REPO_BRANCH` | unset / `main` | Repository the *view source* links point at. Unset means no source links rather than broken ones. |
| `ENV_EXPORT_PATH` | `/app/.env.dashboard` | Where `exa config export` writes the dashboard's effective configuration inside the container. |
| `DATAPLANE_URL` | `http://localhost:18010` | Dataplane the dashboard queries. **Note the name**: the dashboard's own variable has no `EXAMLOPS_` prefix, and Compose feeds it from `EXAMLOPS_DATAPLANE_URL`. Setting only the prefixed one works through Compose; setting the dashboard's environment directly needs this name. |
| `SLURM_MODE` | `mock` | Scheduler mode the dashboard reports (`mock` / `slurm` / `flux`). Same naming subtlety: Compose supplies it from `EXAMLOPS_SLURM_MODE`. |
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
| `DATAPLANE_BUS_BRIDGE_STATUS_URL` | dashboard `http://localhost:8003` · `exa` `http://localhost:18003` | Where the bridge's `/health` and `/stats` are. The two defaults differ because the two readers do: the dashboard is also run bare-metal beside a bare-metal bridge, which serves on `8003` with no port mapping, while `exa` runs on the host, where the container publishes `18003`. In Docker the dashboard is set to `http://dataplane-bus-bridge:8003` (the docker-compose default). Set this variable to point both at one bridge — `exa dataplane-bus status` and `exa production verify` honour it (config key `dataplane_bus_bridge` under `[urls]`), as does the dashboard (whose DB config key `dataplane_bus_bridge_status_url` overrides it). |
| `XDG_STATE_HOME` | `~/.local/state` | Standard XDG base directory; the dashboard's default CLI workspace is `$XDG_STATE_HOME/examlops/cli-workspace` when `EXAMLOPS_DASHBOARD_CLI_WORKSPACE` is unset. |
| `EXAMLOPS_DASHBOARD_CLI_WORKSPACE` | `$XDG_STATE_HOME/examlops/cli-workspace` (`~/.local/state/…`) | CLI Console (ADR 0119): the one directory `exa` path arguments may name from the dashboard. Uploads land here; files a command writes appear here to download. Compose sets it to `/var/lib/examlops-dashboard/cli-workspace` on the `dashboard_cli_data` named volume, so it survives rebuilds and stays out of the repo. |
| `EXAMLOPS_DASHBOARD_CLI_CWD` | `REPO_ROOT`, else the repo `examlops` is loaded from | CLI Console: directory commands run from — the repo root, as an operator runs `exa`, so commands that read the use-case pack or compose files by relative path work. Path *arguments* still resolve inside the workspace (they are passed as absolute paths). |
| `EXAMLOPS_DASHBOARD_CLI_TIMEOUT` | `300` | CLI Console: seconds a run may take before its whole process group is stopped (SIGTERM, then SIGKILL). |
| `EXAMLOPS_DASHBOARD_CLI_MAX_CONCURRENT` | `4` | CLI Console: runs in flight across all users; beyond it a new run is refused with 429 rather than queued. |
| `EXAMLOPS_DASHBOARD_CLI_PER_USER` | `2` | CLI Console: runs in flight per signed-in session. |
| `EXAMLOPS_DASHBOARD_CLI_MAX_OUTPUT` | `2000000` | CLI Console: bytes of stdout kept per run (stderr keeps a quarter of it); the rest is drained and dropped, and the run is marked truncated. |
| `EXAMLOPS_DASHBOARD_CLI_MAX_UPLOAD` | `26214400` | CLI Console: largest file (bytes) an admin may upload into the workspace. |
| `EXAMLOPS_DASHBOARD_CLI_HISTORY` | `500` | CLI Console: runs kept in the shared `dashboard_cli_runs` table (newest first); older rows are pruned. The table is what lets every dashboard replica serve every run, and history survive a restart. |
| `EXAMLOPS_DASHBOARD_CLI_STORE_OUTPUT` | `256000` | CLI Console: characters of stdout/stderr stored per run in the shared table (the live run keeps up to `EXAMLOPS_DASHBOARD_CLI_MAX_OUTPUT`); a longer output is stored truncated and flagged. |
| `EXAMLOPS_DASHBOARD_CLI_CATALOG_TTL` | `300` | CLI Console: seconds the command catalog (built from the live CLI tree) is cached before it is rebuilt, so a `git pull` on the bind-mounted repo shows up without a restart. |

Generate the required secrets:
```bash
# JWT secret
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Fernet key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Dataplane bus Bridge

The bridge (`platform/clients/dataplane_bus_bridge.py`) connects to the real Dataplane bus (from the dataplane-bus repo, TCP :5398) in `reqres` mode. Per-model handler UUIDs are read automatically from `pipelines/models/*.yaml` (`dataplane_bus_uuid` field). No topic UUIDs need to be set.

| Variable | Default | Purpose |
|---|---|---|
| `DATAPLANE_BUS_HOST` | `dataplane-bus-reqgen` | Where the bridge finds Dataplane bus. Container on `dataplane-bus-net` → its container name (default). **Bare-metal on the host** → `host.docker.internal` (needs the bridge's `extra_hosts: host.docker.internal:host-gateway`) or the Docker bridge gateway IP (e.g. `172.19.0.1`). Not `localhost` (that's the container, not the host). For `make stack-up` this is interpolated from the **root** `.env`. See `docs/guides/dataplane-bus-sim.md`. |
| `DATAPLANE_BUS_PORT` | `5398` | Dataplane bus TCP port. Bare-metal Dataplane bus must bind `0.0.0.0:<port>`, not `127.0.0.1`. |
| `DATAPLANE_BUS_MODE` | `reqres` | Bridge mode — always `reqres` with the real Dataplane bus |
| `DATAPLANE_BUS_RETRAIN_UUID` | unset | Optional: req/res UUID for `RetrainReqV1 → RetrainResV1` |
| `DATAPLANE_BUS_VECTOR_UUID` | unset | Optional: req/res UUID for `VectorReqV1 → VectorResV1` |
| `DATAPLANE_BUS_DEFAULT_MODEL` | `JPCP` | Fallback model name when `HpcJobV1.modelName` is empty |
| `DATAPLANE_BUS_DEFAULT_ALIAS` | `Production` | Fallback MLflow alias when `HpcJobV1.alias` is empty |
| `DATAPLANE_BUS_TELEMETRY_QUEUE_MAX` | `1000` | Bound on per-inference drift / input-embedding records waiting to be written. The bridge replies on the bus first and writes these in the background; a full spool drops the record (`dataplane_bus_telemetry_dropped_total`), and a failed write is counted (`dataplane_bus_telemetry_persist_failures_total`) without failing the inference. |
| `DATAPLANE_BUS_PUBLISH_RESULTS` | `true` | Publish inference results back onto the bus. `false` makes the bridge consume-only. |
| `DATAPLANE_BUS_INFERENCE_UUID` | unset | **Legacy** global req/res UUID, used only when no per-model `dataplane_bus_uuid` is present in any model YAML. Prefer `exa dataplane-bus init-uuids`. |
| `DATAPLANE_BUS_JOB_TOPIC_UUID` / `DATAPLANE_BUS_RESULT_TOPIC_UUID` | unset | Pub/sub topic UUIDs for the job and result streams. |
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
| `AGENT_OLLAMA_NUM_CTX` | `16384` | Context window requested from Ollama. Its server default (4096) truncates Skipper's scoped tool-pack prompts (~5k tokens) from the front, dropping the system prompt. `0` leaves the server default. |
| `AGENT_SERVER_PORT` | `18004` | Port for the HTTP/WebSocket chat server (`skipper.server`). |
| `AGENT_API_KEY` | unset | Legacy single credential protecting completions, status, history, and WebSocket tools. It remains the dashboard fallback and maps to the `primary` principal. Prefer distinct caller credentials in `AGENT_API_KEYS_JSON`. |
| `AGENT_API_KEYS_JSON` | unset | JSON object mapping trusted principal names to distinct bearer credentials, for example `{"dashboard":"<dashboard-key>","cli-operator":"<cli-key>"}`. Names become server-derived conversation and memory owners; callers cannot choose them. Use distinct credentials wherever memory isolation matters. Entries are validated individually: a non-string or empty value, or an empty principal name, is refused for that principal only — logged once at `WARNING` and counted on the agent's `GET /healthz` as `credential_config_problems`. A map that does not parse at all leaves authentication on, never off. |
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

### Versioned system prompt (ADR 0009)

Skipper resolves its system prompt from the prompt registry as `skipper-system@<label>` instead of
reading a Python constant, so a prompt change is a label move rather than a code deploy, and can be
rolled back. The literal in `skipper/prompts.py` remains the seed and the fail-safe: an absent,
unreachable or empty registry falls back to it silently, so the agent always starts.

Seed it once with `python -c "from skipper.prompts import seed_system_prompt; seed_system_prompt()"`
(idempotent, no behaviour change), then `exa prompt label skipper-system prod --version N` to move
it and `exa prompt rollback skipper-system prod` to go back.

| Variable | Default | Purpose |
|---|---|---|
| `SKIPPER_PROMPT_REGISTRY` | `1` | Resolve the system prompt from the registry. `0`/`false`/`no`/`off` ⇒ always use the literal in `skipper/prompts.py`. |
| `EXAMLOPS_PROMPT_BACKEND` | `platform_db` | **B1** where prompts live: `platform_db` or `mlflow` (the MLflow Prompt Registry, ADR 0009 clause 1). Unknown values are an error. |
| `EXAMLOPS_PROMPT_MLFLOW_URI` | unset (`MLFLOW_TRACKING_URI`) | **B1** MLflow URI for the `mlflow` prompt backend, when prompts should live in a different MLflow than the tracking server. |
| `EXAMLOPS_PROMPT_GATE_LABELS` | `prod` | **B1** comma-separated prompt labels whose moves are gated by the C3 eval regression check (ADR 0009 clause 4). Gating `dev`/`staging` too would deadlock the registry — the gate reads its baseline from a labelled version. |
| `SKIPPER_PROMPT_LABEL` | `prod` | Which label to resolve (`dev`/`staging`/`prod`), so a staging agent can run an unpromoted prompt. |

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
| `AGENT_WATCH_EVENTS` | `true` | With `EXAMLOPS_NATS_URL` set, the `--daemon` also consumes `retrain.*` from the event backbone and raises `alert.retrain` for a run that ends FAILED, CRASHED or MISSING. `false` keeps it to the polled drift and cost signals. |
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
| `AGENT_KNOWLEDGE_K` | `10` | How many chunks `search_knowledge` retrieves per question. Measured 2026-08-28: for *"confirm the Ray Serve deployment has its models loaded and is returning inference responses"* the chunk naming `exa serve check` is retrieved at ranks 7, 8, 10, 13 and 18 — the previous hard-coded `5` cut it off and the agent answered with the plausible commands ranked above it. Raise for recall, lower to spend less context. |
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
| `MINIO_URL` | `http://localhost:19000` | Internal MinIO S3 API the dashboard uses for model docs |
| `MINIO_CONSOLE_URL` | `http://localhost:19001` | Internal MinIO console URL |
| `LOKI_URL` | `http://localhost:13100` | Internal Loki URL the dashboard queries for logs |
| `JUPYTERHUB_URL` | `http://localhost:18888` | Internal JupyterHub URL (workbench status) |

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
| `PUBLIC_RAY_SERVE_URL` | `http://localhost:18001` | Clickable Ray Serve API URL (the model-detail *Try the API* link) |
| `PUBLIC_CONTROL_PLANE_URL` | `http://localhost:18002` | Clickable control-plane URL |
| `PUBLIC_MINIO_CONSOLE_URL` | `http://localhost:19001` | Clickable MinIO console URL |
| `PUBLIC_LOKI_URL` | `http://localhost:13100` | Clickable Loki URL |
| `PUBLIC_JUPYTERHUB_URL` | `http://localhost:18888` | Clickable JupyterHub URL |
| `PUBLIC_DASHBOARD_URL` | `http://localhost:18099` | The dashboard's own browser-facing base, used in links it emits |
| `GRAFANA_LOKI_EXPLORE_URL` | `http://localhost:13000/explore` | Grafana *Explore* deep link the log views point at |

Every one of these reaches the dashboard through its **pydantic-settings** class
(`platform/services/dashboard/backend/settings.py`), where the field `public_ray_serve_url` *is* the
variable `PUBLIC_RAY_SERVE_URL`. That matters for more than curiosity: a variable read this way never
appears as a string literal anywhere, so it is invisible to a text search — which is exactly how
nineteen of them went undocumented until `tests/unit/test_env_vars_are_documented.py` learned to read
the settings class as well as the code.

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
| `EXAMLOPS_MODELZOO_DIR` | `<repo>/modelzoo` | Where to find the `seanergys_modelzoo` checkout. Every runtime path-resolution site (pipeline engine, Ray Serve, control plane, use-case pack, `exa data`/`exa data synth`) honours it. |

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
| `RAY_MODELS_DIR` | unset | Phase 14 — directory of per-model YAML files (e.g., `usecases/reference/models`); takes precedence over `RAY_REGISTRY_PATH` for per-model alias control |
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

## Instance data, upgrades & site modules (ADR 0128)

The platform is three layers — the **core** (the code a release replaces), the **deployment**
(Compose, Helm, a bare host) and the **instance data** users create. These variables are the
seams between them. All are optional: with none set, an install behaves exactly as before.
Guides: [Core · deployment · instance data](../guides/three-layer-architecture.md),
[Upgrades & compatibility](../guides/upgrade-and-compatibility.md),
[Site feature profiles](../guides/site-feature-profiles.md).

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_DATA_DIR` | unset (legacy per-store locations) | The **instance-data root**. When set, the defaults of `PLATFORM_DB` (`<root>/platform.db`), the site profile (`<root>/site.toml`), the site configuration directory (`<root>/config`), the HPC cluster registry (`<root>/config/clusters.yaml`), the upgrade backup directory (`<root>/backups`), the agent's SQLite files (`<root>/agent/`, when `AGENT_DB`/`AGENT_MEMORY_DB`/`AGENT_MEMORY_REVIEW_DB` are unset) and the site's use-case pack (`<root>/usecase`, used when it has a `pack.toml`) derive from it. An explicitly set per-store variable still wins. Compose sets it to `/state` on every service that mounts the state directory. Create one with `exa instance init`. |
| `EXAMLOPS_FEATURES` | unset (every module) | Site feature-profile overlay, read by the CLI, the dashboard and the services: `preset:<name>`, `+module` (or a bare `module`), `-module`, comma- or space-separated — e.g. `preset:standard,+hpc,-finops`. Layers **over** the site profile file. The Helm chart sets it from `site.features`. `exa modules render` prints the value for a profile. |
| `EXAMLOPS_SITE_PROFILE` | `<EXAMLOPS_DATA_DIR>/site.toml`, else `<config dir>/site.toml` | Path of the site profile file that `exa modules enable|disable|preset` edits. |
| `EXAMLOPS_DEPLOYMENT` | detected | How this process is run, as reported by `exa instance info`: `kubernetes` (set by the Helm chart), `compose`, `container`, `host`. Unset ⇒ detected from `KUBERNETES_SERVICE_HOST` and the container marker files. |
| `EXAMLOPS_ALLOW_INCOMPATIBLE_DATA` | off | Open a datastore whose data format this release must not read (written by a newer release after a breaking migration). Off ⇒ the platform refuses it with `IncompatibleDataError`. Only for a deliberate, understood downgrade — restoring the pre-upgrade backup is the safe way back. |
| `EXAMLOPS_IMAGE_TAG` | `latest` | Image tag Compose runs; `exa instance info` reports it as part of the deployment layer. |
| `KUBERNETES_SERVICE_HOST` | set by Kubernetes | **Set by the kubelet** in every pod; read only to detect that the process runs on Kubernetes. Not an operator knob. |
| `POD_NAMESPACE` / `COMPOSE_PROJECT_NAME` | unset | Reported by `exa instance info` when the deployment provides them. |
| `XDG_CACHE_HOME` | `~/.cache` | Standard XDG base directory; generated scheduler job scripts go to `$XDG_CACHE_HOME/examlops/jobs` when `EXAMLOPS_JOB_SCRIPT_DIR` is unset. |
| `XDG_DATA_HOME` | `~/.local/share` | Where an **installed wheel** keeps `examlops/platform.db` when neither `PLATFORM_DB` nor `EXAMLOPS_DATA_DIR` is set (a source checkout keeps `<repo>/platform.db`). |

---

## Feature gates

Every gate here is **off unless set**, and a gate that is off gates nothing — none of them will
appear in a log or a CI summary until you switch it on.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_SLO_GATE_ENABLED` | off | Make `exa slo` failures block a promotion instead of reporting. |
| `EXAMLOPS_SLO_PROBE_TIMEOUT` | `5` | Seconds the `availability` SLI probe (`exa slo ingest`, ADR 0023) waits for the model's Open Inference Protocol readiness answer (`GET /v2/models/{model}/ready` on `RAY_SERVE_URL`) before counting it a bad sample. |
| `EXAMLOPS_FAIRNESS_GATE_ENABLED` | off | Make subgroup-fairness failures block. |
| `EXAMLOPS_SYNTHETIC_ONLY_GATE` | off | Refuse to train on anything but synthetic data — for a use case that may not touch real records yet. |

Accepted truthy values are `1`, `true`, `yes`, `on` (case-insensitive); anything else is off.

---

## Carbon intensity signal

Unset ⇒ the carbon provider uses its static coefficient rather than a live grid signal.

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_GRID_INTENSITY_URL` | unset | Endpoint returning current grid carbon intensity. |
| `EXAMLOPS_GRID_INTENSITY_ZONE` | unset | Zone/region appended to that URL. |
| `EXAMLOPS_GRID_INTENSITY_TOKEN` | unset | Bearer token for the signal provider. |
| `EXAMLOPS_GRID_INTENSITY_METHOD` | `average_grid_mix` | What the endpoint measures (ADR 0112). Accounting methods (`average_grid_mix`, `residual_mix`) may be reported; decision methods (`locational_marginal`, `marginal_emissions`, `short_run_marginal`) may drive placement. The default is the safe one — set a decision method **only** if the feed really is marginal. |

---

## Paths, identity & misc

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_CONFIG_DIR` | `<EXAMLOPS_DATA_DIR>/config`, else `~/.config/examlops` | Site configuration directory: `providers.yaml`, `finops.yaml`, `policy.yaml`. The operator's own `config.toml` is located by `EXAMLOPS_CONFIG` instead. |
| `EXAMLOPS_REPO_ROOT` | auto-detected | Repository root, when the platform runs from somewhere the walk-up cannot find it. |
| `EXAMLOPS_TENANT` | `default` | Tenant recorded on writes when multi-tenancy is on. |
| `EXAMLOPS_VAULT_TOKEN` | unset | Token for the OpenBao/Vault secrets backend. Unset ⇒ the backend degrades to Fernet, then to env. |
| `EXAMLOPS_POLICY_ENGINE` | built-in | `opa` uses Rego via a local OPA binary, **if it is available** — otherwise the built-in engine stays in use, silently. |
| `EXAMLOPS_POLICY_BUNDLE_DIR` | `~/.config/examlops/bundle` | Where Rego bundles are read from. |
| `EXAMLOPS_LLM_LAUNCHER` | `external` | Default launcher for `exa serve llm` — one of `external`, `compose`, `slurm`, `flux`, `kserve` (`slurm` and `flux` are the same HPC launcher under two scheduler names). `--launcher` overrides it. |
| `EXAMLOPS_LLM_COST_PROVIDER` | from `finops.yaml` | Provider for LLM token cost. |
| `EXAMLOPS_KSERVE_GATEWAY_URL` | unset | Gateway the generated KServe endpoint is reachable on; recorded on the endpoint. |
| `EXAMLOPS_KSERVE_NAMESPACE` | `examlops` | The KServe substrate's real, live apply (ADR 0142 d6, `examlops.serving.substrates.kubectl_client`) — the namespace every `InferenceService`/`LLMInferenceService` is applied to, read from and deleted from. Explicit rather than the kubeconfig context's ambient default, since the rendered manifests carry no `metadata.namespace` on purpose (render stays pure). Per-project namespace isolation is a separate, not-yet-decided design question; every project shares this one namespace for now. |
| `EXAMLOPS_KSERVE_FIELD_MANAGER` | `examlops` | The `--field-manager` Server-Side Apply uses for KServe objects (ADR 0142 d6) — the identity that shows up in `metadata.managedFields`, distinguishing exa's own applies from anything else (a human `kubectl apply`, another controller) that later touches the same object. |
| `EXAMLOPS_MLFLOW_ARTIFACTS_DESTINATION` | unset | The MLflow tracking server's `--artifacts-destination` (e.g. `s3://mlflow-artifacts`). A KServe manifest must point at storage a pod can read, so a version whose artifacts are reported as `mlflow-artifacts:/…` is mapped onto this location; unset ⇒ such a version is refused and you pass `--artifact-uri` instead. |
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
