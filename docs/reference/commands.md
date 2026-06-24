# ExaMLOps Command Reference

Use `exa` for day-to-day ML production operations: training, retraining, deployment, ModelZoo checks, approvals, serving, and SeanerBUS status. Keep `make` for bootstrap, Docker Compose infrastructure, monitoring, notebooks, CI, and low-level developer shortcuts.

## Stack Management

| Command | Description |
|---|---|
| `make bootstrap` | One-shot setup: start stack + install all deps |
| `make full-up` | Start everything: core stack + monitoring + SeanerBUS reqgen + bridge (guards: requires `seanerbus-net` network and seanerbus repo at `../seanerbus`) |
| `make stack-up` | Start full stack (Postgres, MLflow, Prefect, Ray, MinIO, Dashboard, Control Plane) |
| `make stack-down` | Stop containers (volumes preserved) |
| `make stack-wipe` | **DESTRUCTIVE** — remove all containers, volumes, images |
| `make stack-restart` | Restart containers without rebuild |
| `make stack-logs` | Tail docker-compose logs |
| `make stack-shell SERVICE=mlflow` | Shell into a running container |
| `exa status` | Show running containers and endpoint URLs |

## Monitoring Stack

| Command | Description |
|---|---|
| `make monitoring-up` | Start Prometheus (:19090) + Alertmanager (:19093) + Tempo (:13200) + Grafana (:13000) + Loki (:13100) + Promtail |
| `make monitoring-down` | Stop monitoring stack (all five services) |
| `make alerts-check` | Validate `alert_rules.yml` with `promtool` — also runs automatically in `make ci-infra` |
| `exa stack monitoring-up` | CLI shortcut for `make monitoring-up` (same compose profile) |
| `exa stack monitoring-down` | CLI shortcut for `make monitoring-down` |
| `exa stack monitoring-status` | Show container status for the monitoring profile |

Grafana auto-provisions seven dashboards from `platform/infra/docker-compose/grafana/provisioning/dashboards/`:

| Dashboard | UID | Contents |
|---|---|---|
| `examlops_overview.json` | `examlops-overview` | 8-service health, platform KPIs, retrain pipeline, approval/inference time series, annotation overlays |
| `examlops_online_metrics.json` | `examlops-online-metrics` | SLO compliance, error budget, burn-rate charts, request rate, latency percentiles, per-model table |
| `examlops_control_plane.json` | `examlops-control-plane` | Retrain success/error/dedup stats, duration percentiles, circuit breaker timeline, Prefect retry rate, approval funnel |
| `examlops_drift.json` | `examlops-drift` | Prediction drift (median/p95/p05), input embedding norm/mean/std vs baseline, auto-retrain history |
| `examlops_approvals.json` | `examlops-approvals` | Approval queue depth, funnel gauge, SLA risk, event rates, auto-expiry trend, event log |
| `examlops_seanerbus.json` | `examlops-seanerbus` | Bridge health, per-model error %, latency p50/p95/p99, retrain trigger rate |
| `examlops_logs.json` | `examlops-logs` | Loki log explorer with service filter |

The dashboard SeanerBUS page embeds the inference rate, error rate, and latency panels as iframes. This requires Grafana to be running with anonymous read-only access (enabled by default via `GF_AUTH_ANONYMOUS_ENABLED=true`). On the remote server, set `PUBLIC_HOST=137.204.56.169` in `.env` so iframe URLs resolve correctly in the browser.

## Real SeanerBUS (seanerbus repo)

The real SeanerBUS (Rust server + `examlops-reqgen`) lives in the companion `seanerbus` repository. It replaces the old `seanerbus_sim.py` mock bus. Start it from the seanerbus repo root:

```bash
# one-time setup (creates shared Docker network)
docker network create seanerbus-net

# start real SeanerBUS + JPCP inference request generator
cd ../seanerbus && docker compose up -d
```

Once running, start the bridge from ai-productions:

```bash
make seanerbus-up
make seanerbus-bridge-logs
```

Or start everything at once:

```bash
make full-up   # starts reqgen + bridge automatically
```

## SeanerBUS UUID Management

Each model has a stable UUID in its `pipelines/models/<name>.yaml` (`seanerbus_uuid` field). The bridge registers one req/res handler per model at startup.

| Command | Description |
|---|---|
| `exa seanerbus list` | Show all models and their UUIDs |
| `exa seanerbus init-uuids` | Assign UUIDs to models that don't have one (idempotent) |
| `exa seanerbus regen-uuid JPCP` | Regenerate UUID for one model (notify HPC teams) |

UUIDs are also visible in the dashboard at **SeanerBUS → Model UUIDs**.

## SeanerBUS Bridge

| Command | Description |
|---|---|
| `make seanerbus-up` | Start SeanerBUS bridge container |
| `make seanerbus-down` | Stop bridge |
| `make seanerbus-bridge-logs` | Tail bridge logs |
| `make seanerbus-reqgen-logs` | Tail reqgen logs (`inference_requests.log` + `seanerbus.log`) |
| `exa seanerbus status` | Probe bridge /health and /stats endpoints |
| `make seanerbus-bridge-up` | Start bridge bare-metal (reqres mode) |

**Test script** (real SeanerBUS must be running):

```bash
make seanerbus-test-req   # send one JPCP req/res inference request, print response
# or directly:
python platform/clients/seanerbus_test_req.py
```

**Prometheus metrics** — the bridge exposes `GET /metrics` on :18003 (same server as `/health` and `/stats`). Prometheus scrapes it automatically via the `seanerbus_bridge` job when the bridge container is running. Five metrics are exposed:

| Metric | Labels | Description |
|---|---|---|
| `seanerbus_bridge_up` | — | 1.0 while the process is running |
| `seanerbus_inferences_total` | `model` | Completed inference calls |
| `seanerbus_inference_errors_total` | `model` | Failed inference calls |
| `seanerbus_inference_latency_seconds` | `model` | End-to-end POST latency histogram |
| `seanerbus_retrain_triggers_total` | — | Drift-triggered retrains |

Live charts are visible in the Grafana **SeanerBUS Bridge** dashboard (`http://localhost:13000/d/examlops-seanerbus`) and embedded directly in the dashboard SeanerBUS page (requires `make monitoring-up`).

## Pipeline

| Command | Description |
|---|---|
| `exa pipeline list` | List all auto-discovered models and datasets |
| `exa pipeline run --dummy` | Run all pipelines with dummy data (dev-safe) |
| `exa pipeline run --env prod` | Run all pipelines with full Zenodo data (production) |
| `exa pipeline run --model JPCP` | Run one pipeline (real data) |
| `exa pipeline run --model JPCP --dataset PM100Dataset --dummy` | Run one pipeline (dummy) |
| `exa pipeline run --model JPCP --dataset PM100Dataset --backend minio` | MinIO backend |
| `exa pipeline deploy` | Deploy nightly schedule to Prefect (2am UTC) |
| `exa pipeline deploy --no-schedule` | Deploy to Prefect (manual trigger only) |
| `exa pipeline validate` | Validate all `pipelines/models/*.yaml` files against Python shims |
| `exa pipeline run --env prod` | Run all pipelines using YAML registry with env overlay (`dev`/`staging`/`prod`) |
| `exa pipeline run --env dev --dummy` | Run with dev overlay (loose thresholds, dummy data) |
| `exa pipeline deploy --env prod` | Deploy one Prefect flow per model in the YAML registry, with env overlay |
| `exa pipeline promote jpcp --if-rmse-lt 5.0` | Promote Staging → Production if RMSE < 5.0 (metric-gated) |
| `exa pipeline promote --list` | List saved promotion rules |
| `exa pipeline validate-model JPCP` | Latency smoke-test Staging alias (exit 1 on failure — CI gate) |
| `exa pipeline validate-model JPCP --max-latency 0.5 --n 5` | Strict 500ms SLA, 5 requests |
| `exa pipeline add-model MyModel` | Register an existing ModelZoo class as a pipeline (no re-scaffolding) |
| `exa pipeline add-model MyModel --task anomaly_detection --type classification` | Register with task/type override |

## Inference (Ray Serve)

| Command | Description |
|---|---|
| `exa stack up --service ray-serving` | Start Ray Serve locally on :18001 |
| `exa serve reload` | Hot-reload Production models from MLflow |
| `exa serve check` | Smoke test: health + model list + one prediction per model |
| `exa serve benchmark` | Benchmark: 200 requests, report latency stats |
| `exa serve infer-check` | Smoke test: POST one synthetic job to the inference pipeline, print JSON |
| `exa serve traffic JPCP` | Show current alias traffic split for JPCP |
| `exa serve traffic JPCP --production 90 --canary 10` | Set 90% Production / 10% Canary split |
| `exa serve models` | List models currently hot-loaded in Ray Serve |
| `exa serve models --detail` | Show full detail per model (alias, version, status) |
| `exa serve traffic-list` | Show traffic split configuration for all models |

## Control Plane

| Command | Description |
|---|---|
| `make control-plane-up` | Start retrain API on :18002 |
| `make control-plane-down` | Stop control plane |
| `make control-plane-logs` | Tail logs |
| `exa retrain JPCP --dataset PM100Dataset --dummy` | POST /retrain via curl |

## ModelZoo Integration

Smoke-test the ModelZoo freshness tracking integration (webhooks + polling + CLI). Requires the control plane to be running.

| Command | Description |
|---|---|
| `exa modelzoo status` / `exa modelzoo events` | Check ModelZoo freshness and recent push events |

## Approval Gate

Sysadmin commands for reviewing model changes detected by CI before training starts.

| Command | Description |
|---|---|
| `exa approvals list` | List all pending model change approvals |
| `exa approvals list --status approved` | List approvals by status (`pending`/`approved`/`rejected`) |
| `exa approvals approve JPCP` | Approve a pending change — fires Prefect training run immediately |
| `exa approvals reject JPCP` | Reject a pending change (no training) |
| `exa approvals reject JPCP --reason "needs data review"` | Reject with a reason recorded in the audit log |

**How it works:**

1. A developer pushes to the modelzoo repo on GitHub.
2. CI detects changed model files, maps them to `model_id`s, and calls `POST /api/changes` on the Control Plane.
3. The Control Plane creates a pending approval entry in its SQLite store — no training runs yet.
4. The dashboard shows a badge with the pending count (admin-only Approvals page).
5. A sysadmin calls `exa approvals approve <name>` (or clicks Approve in the dashboard) to trigger training.
6. The existing 7-step Prefect pipeline runs → MLflow registration → Ray Serve auto-reload.

**Environment variables required:**

```bash
export CONTROL_PLANE_TOKEN=<shared bearer token>
export CONTROL_PLANE_URL=http://localhost:18002   # used by CI notify script
```

**Control plane reliability env vars (all optional, have defaults):**

| Variable | Default | Purpose |
|---|---|---|
| `LOG_FORMAT` | `text` | `text` for human-readable logs; `json` for structured JSON (production) |
| `APPROVAL_EXPIRY_HOURS` | `72` | Pending approvals older than this are auto-expired |
| `PREFECT_CB_FAIL_MAX` | `5` | Prefect circuit breaker opens after this many consecutive failures |
| `PREFECT_CB_RESET_TIMEOUT` | `30.0` | Seconds before circuit breaker attempts HALF-OPEN recovery |
| `IDEMPOTENCY_TTL_SECONDS` | `300` | How long a `X-Idempotency-Key` response is cached |

**Control plane endpoints:**

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | — | Full health: DB, token, Prefect, startup checks, CB state |
| `GET /ready` | — | Liveness probe — always 200, independent of startup state |
| `GET /metrics` | — | Prometheus scrape target |
| `POST /retrain` | Bearer token | Trigger a training run |
| `POST /admin/reload` | Bearer token | Hot-reload YAML registry + re-run startup checks |

## `exa` CLI

The `exa` command is the primary operator interface, installed via `pip install -e .` or `uv pip install -e ".[dev]"`.

**Global flags** (apply to every subcommand):
- `--json` / `-j` — machine-readable JSON output (skips spinners and confirmations)
- `--yes` / `-y` — skip all confirmation prompts (CI-safe, non-interactive)
- `--version` / `-V` — print installed version and exit

Options with a fixed set of values (e.g. `--backend`, `--alias`, `--env`, `--task`, `--type`, `--service`) display their choices in `--help` as `[a|b|c]` and tab-complete automatically after running `exa --install-completion`. Every command shows 2–3 copy-paste examples at the bottom of its help text.

```bash
exa --help                                    # full command reference
exa --version, -V                             # print installed version and exit
exa --yes, -y <subcommand>                    # skip all confirmation prompts (for CI / non-interactive scripts)
exa --install-completion                      # add shell tab-completion (bash/zsh/fish) — also completes enum values

exa doctor                                    # diagnose: config, API token, all services, platform DB, Python, Docker
exa --json doctor                             # machine-readable health report

exa status                                    # platform snapshot: services + pending approvals + production models
exa production verify                         # non-mutating production health + freshness verification
exa production deploy                         # dry-run deploy/retrain/reload/verify plan for stale models
exa production deploy --execute               # execute the production workflow (side effects) and append history
exa production deploy rollback <deploy-id>    # dry-run rollback to that deploy's previous successful snapshot
exa production deploy rollback <deploy-id> --execute  # restore Production aliases, reload, verify, and append rollback history
exa production deploy history                 # list recent production deploy records
exa production deploy history --status failed # filter history by status
exa production deploy history --model MACK    # filter history by model ID
exa production deploy history --operation rollback  # filter history by operation
exa production deploy status <deploy-id>      # inspect one deploy record
exa --json production verify                  # machine-readable production verification report

exa approvals list                            # pending model change approvals
exa approvals approve JPCP                    # approve → fires Prefect training run
exa approvals reject JPCP --reason "needs review"
exa approvals delete <uuid>                   # retract a stale or duplicate pending approval by UUID
exa --yes approvals delete <uuid>             # bypass confirmation prompt (CI-safe)

exa modelzoo status                           # ModelZoo freshness for every registered model (CURRENT / STALE)
exa modelzoo events                           # recent ModelZoo push event log
exa modelzoo events --limit 20               # show last 20 push events
exa modelzoo sync                             # manually trigger a GitLab poll cycle
exa modelzoo config                           # show runtime config (auto_retrain, poll_interval_seconds, watch_branch)
exa modelzoo config-set auto_retrain true     # set a config key on the control plane
exa modelzoo config-set poll_interval_seconds 120

exa models list                               # all registered models
exa models info JPCP                          # versions, aliases, metrics
exa models diff jpcp 17 18                    # compare metrics/params between two versions
exa models lineage jpcp                       # pipeline → dataset → model version chain
exa models lineage jpcp 18                    # lineage for a specific version
exa models cost jpcp                          # show HPC GPU-hour and cost history for a model
exa models cost jpcp --record                 # fetch Slurm data, store to DB, tag MLflow versions
exa models cost-list                          # cross-model HPC cost summary (all models, latest cost per model)

exa retrain JPCP --dataset PM100Dataset --dummy
exa predict JPCP --features "$(python3 -c 'import json; print(json.dumps({"embedding":[0.1]*384,"num_nodes": 4,"user_id": "smoke"}))')"

exa serve reload                              # hot-reload all Production models
exa serve reload --model JPCP                 # reload one model
exa serve check                               # smoke test: health + loaded models
exa serve infer-check                         # POST one valid synthetic HPC job to /infer-pipeline/infer
exa serve benchmark --requests 200            # latency benchmark through dummy client
exa serve traffic JPCP                        # show current alias traffic split
exa serve traffic JPCP --production 90 --canary 10  # set 90/10 traffic split
exa serve models                              # list models hot-loaded in Ray Serve
exa serve models --detail                     # full detail per model
exa serve traffic-list                        # show traffic split config for all models

exa pipeline list                             # auto-discovered models (runs from repo root)
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
exa pipeline run --model JPCP --dataset PM100Dataset --backend minio
exa pipeline validate                         # validate pipelines/models/*.yaml against Python shims
exa pipeline deploy                           # register Prefect deployments for all models (nightly schedule)
exa pipeline deploy --no-schedule             # register without schedule (manual trigger only)
exa pipeline deploy --model JPCP              # register deployment for one model only
exa pipeline deploy --registry pipelines/model_registry.yaml --env prod  # with registry + env overlay
exa pipeline export-registry                  # export auto-discovered model state to model_registry.yaml
exa pipeline promote jpcp --if-rmse-lt 5.0   # promote Staging→Production if RMSE < 5.0
exa pipeline promote jpcp --if-rmse-lt 5.0 --dry-run  # show outcome without promoting
exa pipeline promote --list                   # list saved promotion rules

exa drift status                              # prediction drift status for all models
exa drift status JPCP                         # drift status for one model
exa drift baseline JPCP                       # store current stats as baseline
exa drift reset JPCP                          # clear all snapshots for a model
exa --json drift status                       # machine-readable drift report
exa drift auto-retrain enable JPCP --dataset PM100Dataset  # enable closed-loop auto-retrain
exa drift auto-retrain enable JPCP --min-z 2.5 --cooldown 1800  # custom threshold + cooldown
exa drift auto-retrain disable JPCP           # disable auto-retrain
exa drift auto-retrain status                 # show auto-retrain config for all models
exa drift trigger                             # fire retrains for CRITICAL models
exa drift trigger --dry-run                   # preview without firing
exa drift input status                        # embedding distribution drift (norm/mean/std vs baseline)
exa drift input baseline JPCP                 # store embedding stats as input baseline
exa drift input reset JPCP                    # clear input embedding snapshots for a model
exa drift snapshots JPCP                      # inspect raw prediction drift snapshots
exa drift snapshots JPCP --last 50            # show last 50 snapshots
exa --json drift snapshots JPCP --raw         # full raw rows as JSON

exa pipeline validate-model JPCP              # latency smoke-test against Staging alias
exa pipeline validate-model JPCP --max-latency 0.5  # strict SLA check
exa pipeline validate-model JPCP --alias Production --n 5
exa pipeline promote-delete JPCP              # delete saved promotion rule for a model
exa pipeline promote-delete --all             # delete all promotion rules (confirmation required)

exa audit                                     # platform audit log (last 30 days)
exa audit --last 7d --model JPCP              # filter by model and time window
exa audit --action model_approved             # filter by action type
exa --json audit                              # machine-readable audit events

exa seanerbus list                            # list per-model SeanerBUS UUIDs
exa seanerbus init-uuids                      # assign missing UUIDs in pipelines/models/*.yaml
exa seanerbus status                          # probe bridge /health and /stats

exa scaffold DemoAD                           # scaffold a new model (PascalCase name required)
exa scaffold DemoAD --task anomaly_detection --type classification
exa scaffold DemoAD --force                   # overwrite existing files

exa pipeline add-model MyModel                # register existing ModelZoo class as pipeline (YAML + shim only)
exa pipeline add-model MyModel --task anomaly_detection --type classification
exa pipeline add-model MyModel --metric f1_score --threshold 0.85 --direction higher_is_better

# Docker Compose infrastructure stays in make:
make stack-up
make stack-restart
make stack-logs
make stack-ps
```

Docker Compose service names used by `make stack-shell SERVICE=<name>`:
`postgres` · `minio` · `mlflow` · `orchestrator` · `ray-serving` · `control-plane` · `prometheus` · `alertmanager` · `tempo` · `grafana` · `loki` · `promtail` · `dashboard` · `jupyterhub` · `seanerbus-bridge`

```bash
exa config show                               # print resolved config
exa config init                               # interactive setup wizard
exa config set control_plane_token mytoken
```

All commands support `--json` for machine-readable output:

```bash
exa --json models list | jq '.[].Name'
exa --json approvals list | jq '.[] | select(.status == "pending")'
```

## Dashboard

| Command | Description |
|---|---|
| `make dashboard-up` | Build + start dashboard container (:18099) |
| `make dashboard-logs` | Tail dashboard logs |
| `make dashboard-check` | Run backend pytest + frontend npm test |

## Agent (Management CLI + Web Chat)

| Command | Description |
|---|---|
| `make agent` | Start the ExaMLOps management agent REPL (terminal) — reads `ANTHROPIC_API_KEY` / `AGENT_OLLAMA_URL` / `AGENT_MODEL` from `.env` |
| `make agent-server` | Start the web chat UI server on port 18004 — open `http://localhost:18004` in a browser |
| `make agent-test` | Run the agent's unit test suite (`platform/services/agent/tests/`) |

The agent is a packaged LangGraph ReAct agent (`exa_agent/`) exposing ~40 tools across 10 groups for natural-language operations and Q&A.

### LLM backend (dual)

The agent prefers **Claude API** when `ANTHROPIC_API_KEY` is set; falls back to **Ollama** otherwise.

| Backend | Env var | Default model | Notes |
|---|---|---|---|
| Claude (Anthropic) | `ANTHROPIC_API_KEY` | `claude-opus-4-8` (override: `ANTHROPIC_MODEL`) | Adaptive thinking enabled; streaming; best results |
| Ollama | `AGENT_OLLAMA_URL` | `llama3.1:8b` (override: `AGENT_MODEL`) | Local inference; requires `ollama-tunnel` or local `ollama serve` |

```bash
# Claude API (recommended)
export ANTHROPIC_API_KEY=sk-ant-...
make agent               # or: make agent-server

# Ollama (local)
ollama-tunnel start
make agent               # uses AGENT_OLLAMA_URL + AGENT_MODEL from .env
```

### Web chat UI (primary interface)

```bash
make agent-server        # starts FastAPI at http://localhost:18004
```

Features: real-time token streaming (WebSocket), dark-theme GitHub palette, thread sidebar, Markdown rendering (marked.js + highlight.js), write-confirm modal, auto-reconnect, copy-to-clipboard, per-message token count. All conversations are persisted and resumable.

REST endpoints: `GET /` (chat UI), `GET /api/info` (backend + model), `GET /api/threads` (thread list), `GET /api/threads/{id}/history`.

### Terminal REPL (secondary)

```bash
make agent               # streaming REPL with ANSI colors and token cost display
```

**Slash-commands:** `/help` `/tools` `/new` `/resume <id>` `/threads` `/model <name>` `/report` `/exit`

**Extended commands:** `/history [n]` (replay last N messages), `/export [file]` (save thread to JSON), `/grep <pattern>` (search message history), `/watch <secs> <query>` (auto-refresh loop)

### Tools (~40 across 10 groups)

- **registry** — `list_models`, `describe_model`, `list_datasets`
- **inference** — `predict`, `predict_pipeline`, `list_loaded_models`, `reload_models`
- **metrics/health** — `get_metrics`, `platform_health`, `generate_report`
- **training** — `list_pipeline_models`, `trigger_retrain`, `get_retrain_status`
- **approvals** — `list_pending_approvals`, `approve_model`, `reject_model`
- **modelzoo** — `modelzoo_status`, `modelzoo_events`, `modelzoo_get_config`, `modelzoo_sync`, `modelzoo_set_config`
- **services** — `list_services`, `service_logs`, `start_service`, `stop_service`, `restart_service`
- **pipelines** — `list_deployments`, `list_runs`, `scaffold_preview`, `scaffold_create`
- **docs/knowledge** — `search_docs`, `read_doc`, `list_docs`, `get_howto`
- **platform_ops** — `compare_model_versions`, `get_model_lineage`, `get_drift_status`, `get_input_drift_status`, `query_audit_log`, `get_platform_summary`, `diagnose_platform`, `set_traffic_split` [WRITE], `promote_model` [WRITE], `trigger_auto_retrain` [WRITE], `validate_model_serving`

**Write confirmation:** 15 mutating tools pause with a `Proceed? [y/N]` prompt (REPL) or a confirmation modal (web UI) before acting. Pass `--yes` / `-y` globally to skip in CI.

### Ollama model options (Omega server)

```bash
ollama-tunnel start                         # port 11436 (Omega)
ollama-tunnel start kapa                    # port 11437 (Kapa, 16 models)

AGENT_MODEL=hermes3:70b make agent          # best tool calling
AGENT_MODEL=llama3.1:70b make agent         # best Llama quality
AGENT_MODEL=qwen3-coder:30b make agent      # strongest reasoning
```

## JupyterHub (Multi-User Notebooks)

| Command | Description |
|---|---|
| `make jupyter-up` | Build images + start JupyterHub on port 18888 |
| `make jupyter-down` | Stop JupyterHub (user volumes preserved) |
| `make jupyter-logs` | Tail JupyterHub logs |
| `make jupyter-add-user USER=alice HUB_TOKEN=<token>` | Create a user via the Hub REST API |

**VS Code connectivity:** open VS Code → Jupyter extension → *Specify Jupyter Server* → `http://<host>:18888/user/<username>/?token=<token>`. The token is shown in the JupyterHub UI under the user's **Token** tab.

## Model Scaffolding

```bash
exa scaffold DemoAD --task anomaly_detection --type classification
```

Supported `TASK` values: `performance_prediction`, `power_consumption_prediction`, `anomaly_detection`
Supported `TASK_TYPE` values: `classification`, `regression`

`--task` and `--type` are enum-constrained: `--help` shows valid choices and shell tab-completion fills them in automatically.

**Alternative — Dashboard wizard:**

Log in as admin, go to **Models → New Model** to use the 3-step ScaffoldWizard (configure → preview files → create).

## Code Quality

| Command | Description |
|---|---|
| `make lint` | Ruff linter across src/, tests/, pipelines/, services/ |
| `make lint-fix` | Ruff auto-fix safe issues |
| `make typecheck` | mypy on pipelines/ and services/ |
| `make test` | Full test suite |
| `make test-unit` | Unit tests only |
| `make test-integration` | Integration tests only |
| `make test-cov` | Tests with HTML coverage → htmlcov/index.html |
| `make agent-test` | Management-agent unit tests (`platform/services/agent/tests/`) |
| `make check` | lint + typecheck + test + dashboard-check |
| `make modelzoo-test` | ModelZoo smoke + unit tests (poetry env) |
| `make ci` | All three CI job groups (mirrors GitHub/GitLab Actions) |

## Direct Python

```bash
source .venv/bin/activate                                        # activate root env

# Run a single test file
.venv/bin/pytest tests/unit/test_pipeline.py -v

# Run a dashboard backend test
cd platform/services/dashboard/backend && pytest tests/test_auth_unit.py -v

# Pipeline generator
.venv/bin/python pipelines/pipeline_generator.py --list
.venv/bin/python pipelines/pipeline_generator.py --dummy
.venv/bin/python pipelines/pipeline_generator.py --model JPCP --dummy

# Per-model YAML validation
exa pipeline validate

# YAML registry overlay (env overlays still work)
exa pipeline run --env prod
exa pipeline deploy --env prod
```

## Service URLs

| Service | URL | Auth |
|---|---|---|
| Dashboard | http://localhost:18099 | viewer/admin password |
| JupyterHub | http://localhost:18888 | username/password |
| MLflow UI | http://localhost:15000 | — |
| Prefect UI | http://localhost:14200 | — |
| Ray Serve API | http://localhost:18001 | — |
| Ray Dashboard | http://localhost:18265 | — |
| Control Plane | http://localhost:18002 | `Authorization: Bearer <CONTROL_PLANE_TOKEN>` |
| SeanerBUS Bridge Status | http://localhost:18003 | — (GET /health /stats /metrics) |
| MinIO Console | http://localhost:19001 | minioadmin / minioadmin |
| Prometheus | http://localhost:19090 | — |
| Alertmanager | http://localhost:19093 | — |
| Tempo | http://localhost:13200 | — (traces via Grafana Explore) |
| Grafana | http://localhost:13000 | admin / admin |
| Loki | http://localhost:13100 | — |

Remote server `lxp-cpu01`: replace `localhost` with `23.109.46.77`, or use `ssh lxp` to forward all ports to localhost.
