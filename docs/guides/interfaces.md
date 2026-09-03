# ExaMLOps Interfaces

A map of every user-facing interface in the platform — browser UIs, REST APIs, and the CLI agent.

## Quick reference

| Interface | URL | Auth |
|---|---|---|
| ExaMLOps Dashboard | http://localhost:18099 | viewer / admin password |
| MLflow UI | http://localhost:15000 | none |
| Prefect UI | http://localhost:14200 | none |
| Ray Serve API | http://localhost:18001 | none |
| Ray Dashboard | http://localhost:18265 | none |
| MinIO Console | http://localhost:19001 | minioadmin / minioadmin |
| Control Plane API | http://localhost:18002 | bearer token |
| JupyterHub | http://localhost:18888 | native username/password |
| Grafana | http://localhost:13000 | monitoring profile |
| Prometheus | http://localhost:19090 | monitoring profile |
| Alertmanager | http://localhost:19093 | monitoring profile |
| Tempo | http://localhost:13200 | monitoring profile (traces via Grafana Explore) |

Start the stack (if not already running): `make stack-up`

Start monitoring: `make monitoring-up`

Check what is running: `exa status`

---

## ExaMLOps Dashboard

**http://localhost:18099**

The primary control surface. A React SPA served by FastAPI at `platform/services/dashboard/`. Log in with the `DASHBOARD_VIEWER_PASSWORD` (read-only) or `DASHBOARD_ADMIN_PASSWORD` (full access). The interactive OpenAPI (Swagger UI) API reference is at `/docs`; the dashboard's own documentation browser is at `/documents`.

### Pages

| Page | Role | What you can do |
|---|---|---|
| **Overview** | viewer | Platform health at a glance — service status badges, quick links |
| **Models** | viewer/admin | Browse all registered models with lifecycle stage badges (Staging / Canary / Production / Archived); admin "New Model" button opens ScaffoldWizard |
| **Model Detail** | viewer/admin | Per-model README with YAML frontmatter, stage badges, try-it-out inference form, MinIO image gallery, admin markdown editor, drift banner |
| **Services** | viewer/admin | Start / stop / restart individual stack services, live status badges, tail Docker logs |
| **Pipelines** | viewer/admin | Prefect deployment status, recent run history, admin trigger button per deployment |
| **Datasets** | viewer | Dataset list pulled live from the configured model zoo (GitLab) |
| **Docs** | viewer | Browse rendered repo documentation (this file tree) |
| **Config** | admin | Set service URLs, Grafana API key, GitLab token, and other encrypted secrets |
| **Approvals** | admin | Pending model change approvals from CI — approve to fire Prefect training or reject with optional reason; shows pending count badge |
| **Audit** | admin | Chronological log of every config write (values never recorded) |

### Try-it-out form (Model Detail)

The **Model Detail** page embeds a small inference form. It POSTs to
`/api/models/{name}/predict`, which proxies through to Ray Serve. Use it for
quick sanity checks without writing curl commands.

---

## MLflow UI

**http://localhost:15000**

The authoritative experiment and model registry store.

- **Experiments** tab — compare runs, view metrics, download artifacts.
- **Models** tab — registered model versions and their aliases
  (`@Staging`, `@Canary`, `@Production`, `@Archived`).

When a training pipeline completes, the promoted version appears here automatically. You can also promote or archive versions manually via the UI or the MLflow SDK.

---

## Prefect UI

**http://localhost:14200**

Pipeline orchestration dashboard.

- **Flow Runs** — live and historical training run logs, task-level state.
- **Deployments** — the `nightly` schedule and the manual trigger.
- **Work Pools** — the default process pool that executes flows.

Trigger an ad-hoc run: Deployments → `examlops_scheduled_training/nightly` → **Run** (or use `exa pipeline run --dummy-one`).

---

## Ray Serve API

**http://localhost:18001**

REST inference API — no browser UI, but interactive Swagger docs are at `/docs`.

```bash
# Health + loaded models
curl http://localhost:18001/health
curl http://localhost:18001/models

# Standard prediction (Production alias)
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2, "feature_1": 0.8}}'

# Pin a lifecycle alias
curl -X POST http://localhost:18001/predict/JPCP \
  -H "Content-Type: application/json" \
  -d '{"features": {"feature_0": 1.2}, "alias": "Canary"}'

# Inference pipeline endpoint (Phase 10)
curl -X POST http://localhost:18001/infer-pipeline/infer \
  -H "Content-Type: application/json" \
  -d '{"embedding": [...384 floats...], "num_nodes": 4}'

# Force a hot-reload from MLflow
curl -X POST http://localhost:18001/reload

# Traffic split rules (Phase 19)
curl http://localhost:18001/traffic-rules                            # GET all rules
curl -X POST http://localhost:18001/traffic-rules/JPCP \            # SET split
  -H "Content-Type: application/json" \
  -d '{"production": 90, "canary": 10, "staging": 0}'
```

Smoke-test all endpoints: `exa serve check`

Benchmark (200 requests, latency stats): `exa serve benchmark`

Pipeline-specific smoke-test: `exa serve infer-check`

---

## Ray Dashboard

**http://localhost:18265**

Cluster health UI bundled with Ray.

- **Overview** — node count, CPU/memory utilisation.
- **Serve** — deployment replicas, request throughput, error rates per deployment.
- **Tasks / Actors** — fine-grained runtime state (useful when debugging batch timeouts).

---

## MinIO Console

**http://localhost:19001** — `minioadmin` / `minioadmin`

S3-compatible object store.

- **`mlflow` bucket** — MLflow run artifacts (models, metrics, plots).
- **`examlops-data` bucket** — training datasets (MinIO backend, Phase 1).
- **`examlops-dashboard` bucket** — model README images uploaded via the dashboard.

---

## Control Plane API

**http://localhost:18002**

FastAPI service that lets clients trigger retraining without holding Prefect credentials. Requires a bearer token (`CONTROL_PLANE_TOKEN`).

```bash
# List supported models
curl http://localhost:18002/models

# Trigger retrain
curl -X POST http://localhost:18002/retrain \
  -H "Authorization: Bearer ${CONTROL_PLANE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model_name": "JPCP", "dataset_name": "PM100Dataset", "is_dummy": true}'

# Poll run state
curl http://localhost:18002/retrain/<flow_run_id>

# Approval gate (Phase 11)
# List pending approvals
curl http://localhost:18002/approvals?status=pending

# Approve a pending model change (fires training)
curl -X POST http://localhost:18002/approve/JPCP \
  -H "Authorization: Bearer ${CONTROL_PLANE_TOKEN}"

# Reject without training
curl -X POST http://localhost:18002/reject/JPCP \
  -H "Authorization: Bearer ${CONTROL_PLANE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"reason": "needs data review"}'
```

Makefile shortcut: `exa retrain JPCP --dataset PM100Dataset --dummy`

Interactive docs: **http://localhost:18002/docs**

See [Control Plane guide](control-plane.md) for the full API.

---

## Monitoring stack

Not running by default — start with `make monitoring-up`.

### Tempo (Distributed Tracing)

**http://localhost:13200** — Grafana Tempo trace backend. No direct browser UI; explore traces through **Grafana → Explore → Tempo** datasource.

Tracing is off by default (`OTEL_SDK_DISABLED=true`). Set `OTEL_SDK_DISABLED=false` in the compose `.env` to enable. Both the `control-plane` and `dashboard` services are already instrumented — all FastAPI routes and httpx calls produce spans automatically via the `opentelemetry-instrument` launcher.

For services that cannot use the launcher, import `examlops.observability`:

```python
from examlops.observability import setup_tracing
setup_tracing("my-service")   # no-op when OTEL_SDK_DISABLED is set/truthy
```

The Tempo Grafana datasource is pre-provisioned with **trace→logs correlation** to Loki: click any span in the trace view to see the matching log lines.

### Alertmanager

**http://localhost:19093**

Prometheus alert routing, deduplication, silences, and inhibitions. Fires when Prometheus evaluates a rule in `platform/infra/docker-compose/alert_rules.yml`.

Six built-in alert rules:

| Alert | Condition |
|---|---|
| `RayServeHighErrorRate` | Prediction error ratio > 5% over 5m, sustained 10m |
| `RayServeHighLatencyP99` | p99 latency > 1s over 5m, sustained 10m |
| `RayServeNoModelsLoaded` | Zero models in the hot set for 5m |
| `RayServeReloadFailures` | Any reload errors in the last 15m |
| `ApprovalsStale` | Oldest pending approval older than 24h for 30m |
| `TargetDown` | Any Prometheus scrape target down for 5m |

The default config routes fired alerts to the UI only. To forward to Slack, edit `alertmanager.yml` and provide a webhook URL.

Validate rules: `make alerts-check`

### Grafana

**http://localhost:13000**

Two pre-built dashboards are provisioned automatically:

**ExaMLOps Online Metrics** — inference request rate and latency per model / alias, model reload events, Ray Serve deployment health.

**ExaMLOps — Approval Gate** (Phase 13) — pending approval count (red when > 0), oldest pending age in minutes (yellow > 30, red > 60), approval events rate by model and action. Use the "Oldest Pending Age" panel to configure a Grafana Alert for approvals waiting longer than one hour.

Set the Grafana API key via the ExaMLOps Dashboard Config page so the dashboard proxy can embed panels.

### Prometheus

**http://localhost:19090**

Raw metrics scrape target. Key metrics:

| Metric | Labels | Source |
|---|---|---|
| `examlops_predict_requests_total` | `model`, `version`, `alias`, `status` | Ray Serve |
| `examlops_predict_latency_seconds` | `model`, `version` | Ray Serve |
| `examlops_models_loaded` | — | Ray Serve |
| `examlops_reload_total` | `scope`, `status` | Ray Serve |
| `examlops_approvals_pending` | — | Control Plane (Phase 13) |
| `examlops_approval_events_total` | `model_id`, `action` | Control Plane (Phase 13) |
| `examlops_approval_age_oldest_seconds` | — | Control Plane (Phase 13) |

---

## JupyterHub

**http://localhost:18888**

Multi-user notebook environment. Start with `make jupyter-up`. Each user gets a dedicated JupyterLab container with access to internal services.

```bash
make jupyter-up                                    # build images + start
make jupyter-add-user USER=alice HUB_TOKEN=<token> # add a user via Hub API
make jupyter-down                                   # stop (user volumes preserved)
```

From a JupyterLab session you can reach MLflow, MinIO, Ray Serve, and the Control Plane by their internal hostnames (e.g., `http://mlflow:5000`). See [JupyterHub guide](jupyter.md) for full setup and user management.

---

## Management Agent (CLI + HTTP)

A conversational interface backed by LangGraph's ReAct loop. The LLM backend is chosen by which keys are set, in order: **Azure Foundry → Claude → Ollama**.

```bash
make skipper                                   # lower-level developer REPL
make skipper-server                            # HTTP/WebSocket service on :18004
exa chat                                       # canonical interactive client
```

The agent groups tools for registry, inference, metrics, training, approvals, ModelZoo, services,
pipelines, documentation, and platform operations. Use natural language to inspect the platform,
request controlled operations, or ask how things work. Mutating tools pause for explicit approval;
named conversations persist across client runs (`exa chat --session <id>` or `/resume <id>`). See the
[agent guide](agent.md) for the current tool surface, backends, client commands, and HTTP/WebSocket API.

Example prompts:
- *"What is the current production version of JPCP?"*
- *"Run inference on JPCP with dummy features."*
- *"Trigger a retrain of MACK using FDataDataset."*

See [Agent guide](agent.md) for configuration and example sessions.

---

## `exa` Platform CLI

The primary operator command-line interface. Install once, use from anywhere — no Makefile or repo checkout required.

```bash
uv pip install -e ".[dev]"       # install from repo root (once)
exa --install-completion         # add shell tab-completion (bash/zsh/fish)
```

| Command | What it does |
|---|---|
| `exa status` | Platform snapshot: services + pending approvals + production models |
| `exa approvals list` | List pending model change approvals |
| `exa approvals approve JPCP` | Approve → fires Prefect training |
| `exa approvals reject JPCP --reason "..."` | Reject without training |
| `exa models list` | All registered models with production alias |
| `exa models info JPCP` | Versions, aliases, metrics |
| `exa retrain JPCP --dataset PM100Dataset --dummy` | POST /retrain |
| `exa predict JPCP --features '{"embedding":[...], "num_nodes":4}'` | POST /infer-pipeline/infer |
| `exa serve reload` | Hot-reload all Production models |
| `exa serve check` | Smoke test per model |
| `exa pipeline list` | Auto-discovered models + datasets |
| `exa pipeline run --model JPCP --dataset PM100Dataset --dummy` | Run training flow |
| `exa pipeline deploy` | Register Prefect deployments for all models (nightly schedule) |
| `exa pipeline deploy --no-schedule` | Register without schedule (manual trigger only) |
| `exa pipeline export-registry` | Export auto-discovered model state to model_registry.yaml |
| `exa scaffold DemoAD` | Scaffold a new model (generates model, config, YAML, test) |
| `exa models diff JPCP 17 18` | Compare metrics and params between two MLflow versions |
| `exa models lineage JPCP` | Show pipeline→dataset→model provenance chain |
| `exa drift status` | Prediction drift z-score for all models (uses platform_db baselines) |
| `exa drift baseline JPCP` | Store current prediction stats as drift baseline |
| `exa drift auto-retrain enable JPCP` | Configure per-model drift-triggered auto-retrain |
| `exa drift auto-retrain disable JPCP` | Disable auto-retrain (config preserved) |
| `exa drift trigger [--dry-run]` | Fire POST /retrain for all CRITICAL models with auto-retrain enabled |
| `exa drift input status [MODEL]` | Embedding distribution drift (norm/mean/std z-score vs baseline) |
| `exa drift input baseline JPCP` | Store current embedding stats as input drift baseline |
| `exa pipeline validate-model JPCP` | Latency smoke-test — exit 1 on error or SLA breach (CI gate) |
| `exa audit --last 7d --model JPCP` | Query platform audit log (action, source, time filters) |
| `exa serve traffic JPCP --production 90 --canary 10` | Set weighted traffic split (persisted + pushed to Ray Serve) |
| `exa pipeline promote JPCP --if-rmse-lt 5.0` | Metric-gated MLflow alias promotion |
| `exa stack up / down / restart / logs / status` | Docker Compose management |
| `exa config show / init / set KEY VALUE` | Config file management |

All commands support `--json` for machine-readable output (pipe to `jq`).

Config is resolved from: per-command flags → environment variables → `~/.config/examlops/config.toml` → hardcoded dev defaults.

See [Command Reference](../reference/cli-generated.md) for the complete command tree with examples.

---

## Remote server

All interfaces are available on the `lxp-cpu01` server at `<REMOTE_HOST>` on
the same ports. Replace `localhost` with the server IP, or use `ssh lxp` to
forward all ports to localhost automatically.
