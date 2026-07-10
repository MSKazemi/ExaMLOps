# ExaMLOps Command Reference

Use `exa` for day-to-day ML production operations: training, retraining, deployment, ModelZoo checks, approvals, serving, and DataPlane status. Keep `make` for bootstrap, Docker Compose infrastructure, monitoring, notebooks, CI, and low-level developer shortcuts.

## Stack Management

| Command | Description |
|---|---|
| `make bootstrap` | One-shot setup: start stack + install all deps |
| `make full-up` | Start everything: core stack + monitoring + DataPlane reqgen + bridge (guards: requires `dataplane-net` network and dataplane repo at `../dataplane`) |
| `make stack-up` | Start full stack (Postgres, MLflow, Prefect, Ray, MinIO, Dashboard, Control Plane) |
| `make stack-down` | Stop containers (volumes preserved) |
| `make stack-wipe` | **DESTRUCTIVE** — remove all containers, volumes, images |
| `make stack-restart` | Restart containers without rebuild |
| `make stack-logs` | Tail docker-compose logs |
| `make stack-shell SERVICE=mlflow` | Shell into a running container |
| `exa status` | Show running containers and endpoint URLs |
| `exa status --watch` | Live auto-refreshing platform view (`--interval N` seconds; Ctrl-C to exit) |

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
| `examlops_dataplane.json` | `examlops-dataplane` | Bridge health, per-model error %, latency p50/p95/p99, retrain trigger rate |
| `examlops_logs.json` | `examlops-logs` | Loki log explorer with service filter |

The dashboard DataPlane page embeds the inference rate, error rate, and latency panels as iframes. This requires Grafana to be running with anonymous read-only access (enabled by default via `GF_AUTH_ANONYMOUS_ENABLED=true`). On the remote server, set `PUBLIC_HOST=<CONTROL_PLANE_HOST>` in `.env` so iframe URLs resolve correctly in the browser.

## Real DataPlane (dataplane repo)

The real DataPlane (Rust server + `examlops-reqgen`) lives in the companion `dataplane` repository. It replaces the old `dataplane_sim.py` mock bus. Start it from the dataplane repo root:

```bash
# one-time setup (creates shared Docker network)
docker network create dataplane-net

# start real DataPlane + JPCP inference request generator
cd ../dataplane && docker compose up -d
```

Once running, start the bridge from ai-productions:

```bash
make dataplane-up
make dataplane-bridge-logs
```

Or start everything at once:

```bash
make full-up   # starts reqgen + bridge automatically
```

## DataPlane UUID Management

Each model has a stable UUID in its `pipelines/models/<name>.yaml` (`dataplane_uuid` field). The bridge registers one req/res handler per model at startup.

| Command | Description |
|---|---|
| `exa dataplane list` | Show all models and their UUIDs |
| `exa dataplane init-uuids` | Assign UUIDs to models that don't have one (idempotent) |
| `exa dataplane regen-uuid JPCP` | Regenerate UUID for one model (notify HPC teams) |

UUIDs are also visible in the dashboard at **DataPlane → Model UUIDs**.

## DataPlane Bridge

| Command | Description |
|---|---|
| `make dataplane-up` | Start DataPlane bridge container |
| `make dataplane-down` | Stop bridge |
| `make dataplane-bridge-logs` | Tail bridge logs |
| `make dataplane-reqgen-logs` | Tail reqgen logs (`inference_requests.log` + `dataplane.log`) |
| `exa dataplane status` | Probe bridge /health and /stats endpoints |
| `make dataplane-bridge-up` | Start bridge bare-metal (reqres mode) |

**Test script** (real DataPlane must be running):

```bash
make dataplane-test-req   # send one JPCP req/res inference request, print response
# or directly:
python platform/clients/dataplane_test_req.py
```

**Prometheus metrics** — the bridge exposes `GET /metrics` on :18003 (same server as `/health` and `/stats`). Prometheus scrapes it automatically via the `dataplane_bridge` job when the bridge container is running. Five metrics are exposed:

| Metric | Labels | Description |
|---|---|---|
| `dataplane_bridge_up` | — | 1.0 while the process is running |
| `dataplane_inferences_total` | `model` | Completed inference calls |
| `dataplane_inference_errors_total` | `model` | Failed inference calls |
| `dataplane_inference_latency_seconds` | `model` | End-to-end POST latency histogram |
| `dataplane_retrain_triggers_total` | — | Drift-triggered retrains |

Live charts are visible in the Grafana **DataPlane Bridge** dashboard (`http://localhost:13000/d/examlops-dataplane`) and embedded directly in the dashboard DataPlane page (requires `make monitoring-up`).

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
| `exa serve traffic JPCP --production 90 --canary 10` | Set 90% Production / 10% Canary split (confirms; audited) |
| `exa serve traffic JPCP --production 90 --canary 10 --dry-run` | Preview the split without changing routing |
| `exa serve models` | List models currently hot-loaded in Ray Serve |
| `exa serve models --detail` | Show full detail per model (alias, version, status) |
| `exa serve traffic-list` | Show traffic split configuration for all models |
| `exa serve traffic-list --watch` | Live auto-refreshing traffic view (`--interval N` seconds; Ctrl-C to exit) |
| `exa serve ab analyze JPCP` | Statistical A/B verdict (Welch's t-test) over recorded observations |
| `exa serve ab analyze JPCP --lower-is-better` | Interpret smaller metric as the winner (RMSE, latency) |
| `exa serve ab analyze JPCP --alpha 0.01 --min-sample 100` | Tune significance level and minimum sample gate |

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
| `exa approvals approve JPCP` | Approve a pending change — fires Prefect training run immediately (confirms first; audited) |
| `exa approvals approve JPCP --dry-run` | Preview the approval without firing training |
| `exa --yes approvals approve JPCP` | Approve without the confirmation prompt (CI/non-interactive) |
| `exa approvals reject JPCP` | Reject a pending change (no training; confirms first) |
| `exa approvals reject JPCP --dry-run` | Preview the rejection without changing anything |
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

## Evaluation & Feedback

Closes the loop between predictions and delayed real-world labels to measure **live model
quality** (RMSE/MAE against observed outcomes), rather than the drift proxy alone.

| Command | Description |
|---|---|
| `exa eval feedback ingest --request-hash <h> --label 88.5` | Record one observed ground-truth label, keyed by prediction `request_hash` |
| `exa eval feedback ingest --from-csv labels.csv` | Bulk-ingest labels from a CSV with `request_hash,label[,source]` columns |
| `exa eval feedback ingest ... --source hpc-sacct` | Tag the label provenance (default `manual`) |
| `exa eval feedback join JPCP` | Show prediction/label pairs joined on `request_hash` |
| `exa eval feedback join JPCP --alias Production` | Restrict the join to one MLflow alias |
| `exa eval feedback accuracy JPCP` | Compute live RMSE/MAE over labelled predictions |
| `exa eval feedback accuracy JPCP --alias Production --record` | Compute and persist metrics to the `live_metrics` table |

**How it works:** each served prediction is stored with a `request_hash`. When the true
outcome becomes known (often hours/days later), `ingest` writes it to the `ground_truth`
table; `join`/`accuracy` then match labels to predictions to report real model quality.

## FinOps & Green-AI

Per-project (= namespace) GPU-hour / cost **budgets** enforced against real recorded training
spend, plus **energy (kWh) and CO₂e accounting** for training runs — relevant to EU-research
sustainability reporting.

| Command | Description |
|---|---|
| `exa finops budget set eu-hpc --gpu-hours 1000 --cost 5000` | Set a project's GPU-hour / cost budget |
| `exa finops budget set eu-hpc --gpu-hours 500 --period weekly` | Budget with a period label |
| `exa finops budget status` | Show every project's budget vs consumed GPU-hours / cost (flags `OVER`) |
| `exa finops budget status eu-hpc` | Budget status for one project |
| `exa finops carbon estimate --gpu-hours 12` | Estimate kWh + gCO₂e for a GPU-hour figure (no DB write) |
| `exa finops carbon estimate --gpu-hours 12 --grid-intensity 232` | Override grid carbon intensity (gCO₂e/kWh) |
| `exa finops carbon record JPCP --gpu-hours 12 --run-id abc` | Estimate and persist a run's energy/carbon to `carbon_records` |
| `exa finops carbon report` | Aggregate recorded energy and carbon across all runs |
| `exa finops carbon report --model JPCP` | Aggregate for one model |

**How it works:** budgets map to namespaces; consumption is summed from the `model_costs`
table (the same GPU-hours `exa models cost` records) joined through `namespace_models`. Carbon
is estimated as `kWh = gpu_hours × (TDP/1000) × PUE` and `gCO₂e = kWh × grid_intensity`, with
documented, overridable defaults (400 W, PUE 1.5, 300 gCO₂e/kWh).

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

exa drift status                              # prediction drift status for all models (with Trend sparkline)
exa drift status JPCP                         # drift status for one model
exa drift status --watch --interval 10        # live auto-refreshing drift view (Ctrl-C to exit)
exa drift baseline JPCP                       # store current stats as baseline (confirms before overwrite)
exa drift baseline JPCP --dry-run             # preview the baseline without writing it
exa drift reset JPCP                          # clear all snapshots (destructive — confirms first; audited)
exa drift reset JPCP --dry-run                # show how many snapshots would be cleared
exa --yes drift reset JPCP                    # skip the confirmation prompt (CI/non-interactive)
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

exa dataplane list                            # list per-model DataPlane UUIDs
exa dataplane init-uuids                      # assign missing UUIDs in pipelines/models/*.yaml
exa dataplane status                          # probe bridge /health and /stats

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
`postgres` · `minio` · `mlflow` · `orchestrator` · `ray-serving` · `control-plane` · `prometheus` · `alertmanager` · `tempo` · `grafana` · `loki` · `promtail` · `dashboard` · `jupyterhub` · `dataplane-bridge`

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
| `make skipper` | Start Skipper, the ExaMLOps management agent REPL (terminal) — reads `ANTHROPIC_API_KEY` / `AGENT_OLLAMA_URL` / `AGENT_MODEL` from `.env` |
| `make skipper-server` | Start Skipper's web chat UI + OpenAI/kube-q bridge on port 18004 — open `http://localhost:18004` in a browser |
| `make skipper-chat` | Chat with Skipper via the kube-q (`kq`) terminal client (needs `skipper-server` running; installs `kube-q` if missing) |
| `make skipper-test` | Run Skipper's unit test suite (`platform/services/agent/tests/`) |
| `make skipper-memory ARGS=stats` | Admin Skipper's long-term memory: `stats` \| `list <kind>` \| `export` \| `delete <kind> [--scope S]` (≡ `python -m skipper.memory_admin`) |
| `make agent` / `agent-server` / `agent-chat` / `agent-test` | Backward-compatible aliases for the `skipper*` targets above |

The agent is a packaged LangGraph ReAct agent (`skipper/`) exposing **45 tools across 10 groups** for natural-language operations and Q&A, plus **3 long-term memory tools** (`recall_memory`, `remember_preference`, `record_procedure`) when the memory store is enabled (Phase 25). Memory admin lives in the agent package (`python -m skipper.memory_admin`), not the `exa` CLI, to keep the platform CLI free of a langgraph dependency; memory mutations are visible via `exa audit --source agent-memory`. See `docs/guides/agent.md` (Long-Term Memory) and `docs/tutorials/skipper-memory.md`.

### LLM backend (triple)

The agent selects a backend by which keys are set, in order: **Azure Foundry → Claude → Ollama**.

| Backend | Trigger env var(s) | Default model | Notes |
|---|---|---|---|
| Azure OpenAI / AI Foundry | `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` | `gpt-5.4-mini` (override: `AZURE_OPENAI_DEPLOYMENT`) | Preferred when configured; OpenAI-compatible Foundry v1 endpoint |
| Claude (Anthropic) | `ANTHROPIC_API_KEY` | `claude-opus-4-8` (override: `ANTHROPIC_MODEL`) | Adaptive thinking enabled; streaming; best results |
| Ollama | `AGENT_OLLAMA_URL` | `llama3.1:8b` (override: `AGENT_MODEL`) | Local inference; requires `ollama-tunnel` or local `ollama serve` |

```bash
# Claude API (recommended)
export ANTHROPIC_API_KEY=sk-ant-...
make skipper               # or: make skipper-server

# Ollama (local)
ollama-tunnel start
make skipper               # uses AGENT_OLLAMA_URL + AGENT_MODEL from .env
```

### Web chat UI (primary interface)

```bash
make skipper-server        # starts FastAPI at http://localhost:18004
```

Features: real-time token streaming (WebSocket), dark-theme GitHub palette, thread sidebar, Markdown rendering (marked.js + highlight.js), write-confirm modal, auto-reconnect, copy-to-clipboard, per-message token count. All conversations are persisted and resumable.

REST endpoints: `GET /` (chat UI), `GET /api/info` (backend + model), `GET /api/threads` (thread list), `GET /api/threads/{id}/history`.

### Terminal REPL (secondary)

```bash
make skipper               # streaming REPL with ANSI colors and token cost display
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

### Ollama model options (<OLLAMA_HOST> server)

```bash
ollama-tunnel start                         # port 11436 (<OLLAMA_HOST>)
ollama-tunnel start <ollama-host>                    # port 11437 (<OLLAMA_HOST>, 16 models)

AGENT_MODEL=hermes3:70b make skipper          # best tool calling
AGENT_MODEL=llama3.1:70b make skipper         # best Llama quality
AGENT_MODEL=qwen3-coder:30b make skipper      # strongest reasoning
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
| `make skipper-test` | Management-agent unit tests (`platform/services/agent/tests/`) |
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

## Self-documenting

`exa docs` walks the live command tree and prints the full reference as Markdown — it can never drift from the implementation. Use `exa docs --out <file>` to write it, or `exa --json docs` for the raw command tree (useful for tooling or feeding an LLM an accurate capability map).

## Plugins (extensibility)

Third-party packages can add their own `exa` subcommands by exposing a `typer.Typer` app under the `examlops.cli_plugins` entry-point group:

```toml
# in a plugin package's pyproject.toml
[project.entry-points."examlops.cli_plugins"]
myteam = "my_pkg.cli:app"
```

Once the package is installed, its commands appear under `exa myteam …`. List discovered plugins and their load status with `exa plugins` (`--json` for machine output). Plugin loading is resilient — a broken plugin is reported but never crashes the CLI.

## Config contexts (multi-environment)

Switch the CLI between environments (e.g. local vs remote `remote-cpu01`) with named contexts:

| Command | Description |
|---|---|
| `exa config set control_plane http://<DATAPLANE_HOST>:18002 --context remote` | Write a key into the `remote` context |
| `exa config use remote` | Make `remote` the active context |
| `exa config contexts` | List contexts and show the active one |
| `exa env` | Show effective config and where each value comes from (env / context / file / default) |
| `exa -c remote status` | Use the `remote` context for a single command (`--context`/`-c` global) |

Resolution order (highest wins): **environment variable** → **active context** → legacy top-level file settings → built-in default. `exa env` makes this explicit and redacts secrets. `EXAMLOPS_CONTEXT` selects a context without persisting it.

## Output formats & shell completion

Every command accepts a global `--output` / `-o` flag that controls how structured data is rendered:

| Flag | Format | Use |
|---|---|---|
| `-o table` (default) | Rich human table | Interactive use |
| `-o json` (or `--json`) | JSON | Scripting, agents, `jq` |
| `-o yaml` | YAML | Config-style, readable diffs |
| `-o csv` | CSV | Spreadsheets, `cut`/`awk` pipelines |

```bash
exa -o json models list | jq '.[].name'
exa -o yaml mcp agent-card
exa -o csv models cost jpcp > cost.csv
```

`--json` remains as a shorthand for `-o json`. Shell completion is built in: run `exa --install-completion` (bash/zsh/fish/PowerShell) once, or `exa --show-completion` to inspect the script.

## Conversational front door & discoverability

| Command | Description |
|---|---|
| `exa ask "which models are drifting?"` | Ask the Skipper agent a question in plain English (routes to its OpenAI-compatible bridge) |
| `exa ask "retrain the worst one" --session mywork` | Preserve conversational context across turns with a session id |
| `exa --json ask "list production models"` | Machine-readable answer for scripting |
| `exa explain` | List every top-level command with a one-line description |
| `exa explain drift` | Show a command group's subcommands |
| `exa explain serve reload` | Plain-language description of a command plus its copy-paste examples |

`exa ask` needs the Skipper agent running (`make skipper-server`); set its URL with `AGENT_URL` or `exa config set agent <url>` (default `http://localhost:18004`), and `AGENT_API_KEY` if the bridge is token-gated. Unknown commands get fuzzy "did you mean …" suggestions automatically.

## Agent surface — MCP + Agent-to-Agent (A2A)

`exa mcp` exposes the platform's capabilities to LLM agents and MCP clients (Claude Desktop, Claude Code, the in-repo skipper agent, and any Agent-to-Agent peer). The tools reuse the exact same code paths as the CLI, so the agent surface never drifts from the human surface.

| Command | Description |
|---|---|
| `exa mcp tools` | List every tool an agent can call over MCP (read tools only) |
| `exa mcp tools --all` | Include mutating (write) tools in the listing |
| `exa mcp resources` | List MCP resources — readable context (status, model registry, audit log, per-model detail) |
| `exa mcp prompts` | List MCP prompts — reusable agent workflows (diagnose drift, promote safely, triage) |
| `exa mcp serve` | Run the MCP server over **stdio** (for Claude Desktop / Claude Code / MCP clients) |
| `exa mcp serve --transport http --port 8765` | Run the MCP server over HTTP |
| `exa mcp serve --allow-writes` | Also register mutating tools (e.g. `trigger_retrain`) — off by default |
| `exa mcp agent-card` | Print the A2A Agent Card (skills, capabilities, transports) |
| `exa --json mcp agent-card` | Emit the full machine-readable A2A Agent Card (serve at `/.well-known/agent.json`) |

Install the optional MCP dependency once: `uv pip install 'examlops[mcp]'` (adds FastMCP; the core CLI works without it).

**Write safety.** Read-only tools are always available. Mutating tools are registered only when writes are explicitly enabled — via `exa mcp serve --allow-writes` or `EXAMLOPS_MCP_ALLOW_WRITES=1`. Mutating calls that need `CONTROL_PLANE_TOKEN` return a structured error envelope when it is unset, so an agent can reason about the failure.

Example — register ExaMLOps with an MCP client (stdio):

```jsonc
{
  "mcpServers": {
    "examlops": {
      "command": "exa",
      "args": ["mcp", "serve"],
      "env": { "CONTROL_PLANE_URL": "http://localhost:18002" }
    }
  }
}
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
| DataPlane Bridge Status | http://localhost:18003 | — (GET /health /stats /metrics) |
| MinIO Console | http://localhost:19001 | minioadmin / minioadmin |
| Prometheus | http://localhost:19090 | — |
| Alertmanager | http://localhost:19093 | — |
| Tempo | http://localhost:13200 | — (traces via Grafana Explore) |
| Grafana | http://localhost:13000 | admin / admin |
| Loki | http://localhost:13100 | — |

Remote server `remote-cpu01`: replace `localhost` with `<DATAPLANE_HOST>`, or use `ssh remote` to forward all ports to localhost.
