# exa CLI Reference

`exa` is the primary operator interface for ExaMLOps. Install it once from the repo root:

```bash
uv pip install -e ".[dev]"
```

## Global Flags

| Flag | Description |
|---|---|
| `--output`, `-o` | Output format: `table` (human) \| `json` \| `yaml` \| `csv` (for scripting/agents) |
| `--json` | Shorthand for `--output json` (kept for compatibility) |
| `--context`, `-c` | Use a named config context for this invocation (see `exa config contexts`) |
| `--yes`, `-y` | Skip all confirmation prompts (non-interactive / CI) |
| `--quiet`, `-q` | Suppress non-essential output (hints, info, progress detail) |
| `--verbose`, `-v` | Show extra diagnostic detail |
| `--version`, `-V` | Print version and exit |
| `-h`, `--help` | Show help for any command or subcommand |
| `--install-completion` | Install shell tab-completion (bash / zsh / fish) — also completes enum option values |

## Exit-Code Contract

`exa` follows a stable exit-code convention so scripts and CI can branch on results:

| Code | Meaning |
|---|---|
| `0` | Success — the command completed (also used for a declined confirmation, which is a clean no-op). |
| `1` | Runtime error — the operation failed (unreachable service, SLA breach, validation failure). Emitted by `exa`'s error path. |
| `2` | Usage error — bad flag, unknown command, or missing argument (from the Typer/Click parser). |

Mutating commands additionally support `--dry-run` (preview, exit `0`, change nothing) and a
confirmation prompt (auto-confirmed under `--yes`, `--json`, or a non-interactive/CI stdin).

> This table is kept in sync with the code by `tests/unit/test_cli_docs.py`. For the always-current,
> auto-generated full command tree (every command, subcommand, and flag), run `exa docs` or see
> [`cli-generated.md`](./cli-generated.md).

## Shell Completion & Enum Choices

Run once to activate tab-completion in your shell:

```bash
exa --install-completion   # bash / zsh / fish supported
```

Options with a fixed set of valid values display their choices in `--help` as `[value1|value2|...]` and are completed automatically by the shell after installation. These are the constrained options:

| Command | Option | Valid values |
|---|---|---|
| `exa scaffold` | `--task` | `performance_prediction` · `power_consumption_prediction` · `anomaly_detection` |
| `exa scaffold` | `--type` | `regression` · `classification` |
| `exa retrain` | `--backend` | `zenodo` · `minio` · `dataplane` |
| `exa predict` | `--alias` | `Production` · `Canary` · `Staging` |
| `exa pipeline run` | `--env` | `dev` · `staging` · `prod` |
| `exa pipeline deploy` | `--env` | `dev` · `staging` · `prod` |
| `exa stack up/down/restart/logs` | `--service` | compatibility wrapper; prefer `make stack-*` for Docker Compose |

All other options (`--model`, `--dataset`, `--version`, `--name`, `--reason`) remain free-form strings.

Every command also shows 2–3 copy-paste examples at the bottom of its `--help` output.

## Configuration

`exa` resolves endpoint URLs from three sources, in priority order:

1. **Environment variables** — `CONTROL_PLANE_URL`, `RAY_SERVE_URL`, `MLFLOW_TRACKING_URI`, `PREFECT_API_URL`, `DASHBOARD_URL`, `CONTROL_PLANE_TOKEN`, `DASHBOARD_TOKEN`
2. **Config file** — `~/.config/examlops/config.toml`
3. **Built-in defaults** — `http://localhost:18002`, `http://localhost:18001`, etc.

### `exa config show`

Print the resolved config (env vars + TOML file).

```bash
exa config show
```

### `exa config init`

Interactive wizard — write `~/.config/examlops/config.toml`.

```bash
exa config init
```

Prompts for each service URL and the control plane token. Press Enter to keep the current value.

### `exa config set <key> [value]`

Set a single config key without the interactive wizard.

| Config key | Maps to |
|---|---|
| `control_plane` | Control Plane URL |
| `ray_serve` | Ray Serve URL |
| `mlflow` | MLflow URL |
| `prefect` | Prefect URL |
| `dashboard` | Dashboard URL |
| `control_plane_token` | Bearer token for `POST /retrain` |

```bash
exa config set control_plane http://<REMOTE_HOST>:18002
exa config set control_plane_token  # hidden prompt; avoids shell-history exposure
```

---

## exa status

Platform snapshot: service health, pending approvals, and loaded production models.

```bash
exa status
exa --json status | jq '.pending_approvals'
```

Queries Control Plane `/health` and Ray Serve `/models`. Gracefully reports when a service is unreachable.

---

## exa models

Query the MLflow model registry.

### `exa models list`

List all registered models with their Production alias version and all aliases.

```bash
exa models list
exa --json models list | jq '.[].Name'
```

### `exa models info <model>`

Show one model's versions, aliases, and latest version metadata.

```bash
exa models info jpcp
exa --json models info jpcp
```

> **Note:** MLflow registry names are lowercase (`jpcp`); the platform's `MODEL_REGISTRY` uses uppercase (`JPCP`).

### `exa models diff <model> <v1> <v2>`

Compare metrics and parameters between two model versions. Delta values are color-coded: green = improvement, red = regression.

```bash
exa models diff jpcp 17 18
exa --json models diff jpcp 17 18
```

| Argument | Description |
|---|---|
| `MODEL` | Registered model name (e.g. `jpcp`) |
| `V1` | First version number |
| `V2` | Second version number |

### `exa models lineage <model> [version]`

Show the full lineage chain: Prefect pipeline run → dataset version → MLflow run → model version. Defaults to the Production alias if no version is given.

```bash
exa models lineage jpcp
exa models lineage jpcp 18
exa --json models lineage jpcp
```

| Argument | Description |
|---|---|
| `MODEL` | Registered model name (e.g. `jpcp`) |
| `VERSION` | Optional version number (default: Production alias) |

### `exa models cost <model>`

Show HPC training cost history for a model, or fetch and record the latest scheduler cost data (Slurm or Flux).

```bash
exa models cost jpcp                # show cost history from DB
exa models cost jpcp --record       # fetch scheduler data, record to DB, tag MLflow versions
exa --json models cost jpcp
```

| Argument / Option | Description |
|---|---|
| `MODEL` | Registered model name (e.g. `jpcp`) |
| `--record` | Fetch GPU/CPU-hours, write to `platform.db`, tag MLflow versions |

The scheduler is read from the MLflow run's `hpc_scheduler` tag (falling back to
`EXAMLOPS_HPC_SCHEDULER` / `EXAMLOPS_SLURM_MODE`). Cost is computed per backend:

- **Slurm** — `gpu_hours × GPU_COST_PER_HOUR` via `sacct` (`gpu_hours` from `gres/gpu`).
- **Flux** — `gpu_hours × GPU_COST_PER_HOUR + cpu_hours × CPU_COST_PER_HOUR` via
  `flux job info R`/`eventlog`, so CPU-only Flux runs (e.g. lxp, 0 GPUs) still show a cost.
- **Mock** — synthetic GPU-hours, deterministic on model name + version.

Defaults: `GPU_COST_PER_HOUR=$2.50/hr`, `CPU_COST_PER_HOUR=$0.05/hr` (both env-overridable).
The job id is looked up from the `hpc_job_id` MLflow tag (legacy `slurm_job_id` still honored).

MLflow model versions are tagged with `gpu_hours` and `cost_usd` when `--record` is used.

---

## exa drift

Prediction drift detection. The bridge writes a snapshot to `platform.db` after every inference; these commands let you inspect and baseline the distribution.

### `exa drift status [model]`

Show live prediction statistics vs. baseline for all models or one model. z-score thresholds: ≥2.0 = WARNING, ≥3.0 = CRITICAL.

```bash
exa drift status
exa drift status JPCP
exa --json drift status
```

### `exa drift baseline <model>`

Store the current rolling statistics (last 500 snapshots) as the drift baseline.

```bash
exa drift baseline JPCP
```

### `exa drift reset <model>`

Clear all drift snapshots for a model (baseline is preserved).

```bash
exa drift reset JPCP
```

### `exa drift auto-retrain enable <model>`

Configure drift-triggered closed-loop auto-retrain for a model. When `exa drift trigger` runs, models above the configured z-score threshold (and past their cooldown) automatically receive a `POST /retrain`.

```bash
exa drift auto-retrain enable JPCP                                  # defaults: z≥3.0, cooldown 1h, PM100Dataset
exa drift auto-retrain enable JPCP --min-z 2.5 --cooldown 1800     # custom threshold + 30m cooldown
exa drift auto-retrain enable JPCP --dataset PM100Dataset --min-z 2.5
```

| Option | Description |
|---|---|
| `--dataset DATASET`, `-d DATASET` | Dataset class for the retrain call (default: `PM100Dataset`) |
| `--min-z Z` | Minimum z-score to trigger retrain (default: `3.0`) |
| `--cooldown SECONDS` | Minimum seconds between triggers (default: `3600`) |

### `exa drift auto-retrain disable <model>`

Disable drift-triggered auto-retrain for a model (config is preserved).

```bash
exa drift auto-retrain disable JPCP
```

### `exa drift auto-retrain status`

List auto-retrain config for all models.

```bash
exa drift auto-retrain status
exa --json drift auto-retrain status
```

### `exa drift trigger`

Check all models with auto-retrain enabled. For each model above its z-score threshold and past its cooldown period, fire `POST /retrain` at the Control Plane.

```bash
exa drift trigger            # fire retrains for all CRITICAL models
exa drift trigger --dry-run  # preview without firing
```

| Option | Description |
|---|---|
| `--dry-run` | Show what would be triggered without actually firing |

### `exa drift input status [model]`

Show input embedding distribution drift. The bridge records per-inference embedding statistics (norm, mean, std); these are compared to a stored baseline using z-score.

```bash
exa drift input status
exa drift input status JPCP
exa --json drift input status
```

### `exa drift input baseline <model>`

Store current rolling embedding statistics (last 1000 snapshots) as the input drift baseline.

```bash
exa drift input baseline JPCP
```

---

## exa audit

Platform audit log — who did what and when. Events are written by the CLI (`exa approvals approve/reject`, `exa serve traffic`, `exa pipeline promote`), the SeanerBUS bridge (every inference), and the agent.

```bash
exa audit                              # last 30 days, all events
exa audit --last 7d                    # last 7 days
exa audit --model JPCP                 # filter by model
exa audit --action model_approved      # filter by action type
exa audit --source bridge              # filter by source (cli / bridge / agent)
exa --json audit --last 30d | jq '.[].action'
```

| Option | Description |
|---|---|
| `--last WINDOW` | Time window, e.g. `7d`, `30d` (default: `30d`) |
| `--model MODEL`, `-m MODEL` | Filter by target model |
| `--action ACTION`, `-a ACTION` | Filter by action type (e.g. `model_approved`, `retrain_triggered`, `traffic_changed`) |
| `--source SOURCE`, `-s SOURCE` | Filter by source: `cli`, `bridge`, `agent` |
| `--limit N`, `-n N` | Max events to show (default: 100) |

---

## exa serve

Ray Serve operations.

### `exa serve reload`

Hot-reload Production models from MLflow into Ray Serve without restarting.

```bash
exa serve reload               # reload all models
exa serve reload --model JPCP  # reload one model
```

| Option | Description |
|---|---|
| `--model MODEL`, `-m MODEL` | Reload only this model (default: all) |

### `exa serve check`

Smoke test Ray Serve: health check + one prediction per loaded model.

```bash
exa serve check
```

### `exa serve infer-check`

POST one valid synthetic HPC job (384-dim embedding) directly to the inference pipeline and print the JSON response. Useful for verifying the full `InferencePipelineIngress → FeatureTransformer → ModelRouter` path.

```bash
exa serve infer-check
```

### `exa serve benchmark`

Run latency benchmark through the dummy client and print p50/p99/max stats.

```bash
exa serve benchmark                # default: 200 requests
exa serve benchmark --requests 25  # custom count
```

| Option | Description |
|---|---|
| `--requests N`, `-n N` | Number of requests (default: 200) |

### `exa serve traffic <model>`

Show or set probabilistic traffic split across model aliases. Weights must sum to 100. Rules are persisted to `platform.db` and applied to Ray Serve in-memory via `POST /traffic-rules/<model>`.

```bash
exa serve traffic JPCP                              # show current split
exa serve traffic JPCP --production 90 --canary 10  # set split
exa serve traffic JPCP --production 100             # reset to 100% Production
exa --json serve traffic JPCP
```

| Option | Description |
|---|---|
| `--production N` | % traffic to Production alias |
| `--canary N` | % traffic to Canary alias |
| `--staging N` | % traffic to Staging alias |

---

## exa retrain

Trigger a Prefect training run via the Control Plane. Requires `CONTROL_PLANE_TOKEN`.

```bash
exa retrain JPCP
exa retrain JPCP --dataset PM100Dataset --dummy
exa retrain JPCP --dataset PM100Dataset --backend minio
```

| Argument / Option | Description |
|---|---|
| `MODEL` | Model ID (e.g. `JPCP`) |
| `--dataset DATASET`, `-d DATASET` | Dataset class name (default: `PM100Dataset`) |
| `--dummy` | Use dummy data — safe for dev |
| `--backend BACKEND` | Storage backend — choices: `zenodo` \| `minio` \| `dataplane` |

---

## exa predict

Send an inference request to the Ray Serve inference pipeline.

```bash
exa predict JPCP --features "$(python3 -c 'import json; print(json.dumps({"embedding":[0.1]*384,"num_nodes": 4,"user_id": "smoke"}))')"
exa predict JPCP --features "$(python3 -c 'import json; print(json.dumps({"embedding":[0.1]*384,"num_nodes": 4,"user_id": "smoke"}))')" --alias Canary
```

| Argument / Option | Description |
|---|---|
| `MODEL` | Model ID (e.g. `JPCP`) |
| `--features JSON`, `-f JSON` | JSON feature dict — must include `embedding` (384 floats) |
| `--alias ALIAS` | MLflow alias — choices: `Production` \| `Canary` \| `Staging` |
| `--version VERSION` | Explicit model version number |

Calls `POST /infer-pipeline/infer` on Ray Serve.

---

## exa approvals

Sysadmin approval gate. CI detects model changes on push to `main`, creates pending approvals, and waits for sysadmin action before training starts.

### `exa approvals list`

List pending model change approvals.

```bash
exa approvals list           # pending only (default)
exa approvals list --all     # all statuses
exa --json approvals list | jq '.[] | select(.status == "pending")'
```

| Option | Description |
|---|---|
| `--all` | Show all statuses, not just pending |

### `exa approvals approve <model>`

Approve a pending change — fires a Prefect training run immediately.

```bash
exa approvals approve JPCP
```

### `exa approvals reject <model>`

Reject a pending change — no training will run.

```bash
exa approvals reject JPCP
exa approvals reject JPCP --reason "needs data review"
```

| Option | Description |
|---|---|
| `--reason REASON`, `-r REASON` | Optional rejection reason |

---

## exa modelzoo

ModelZoo repository freshness and events. Freshness state is maintained by the Control Plane via GitLab/GitHub webhooks and a background poller.

### `exa modelzoo status`

Show ModelZoo freshness for every registered model (`CURRENT` / `STALE`).

```bash
exa modelzoo status
exa --json modelzoo status
```

### `exa modelzoo events`

Show recent ModelZoo push events (default: last 10).

```bash
exa modelzoo events
exa modelzoo events --limit 20
```

| Option | Description |
|---|---|
| `--limit N`, `-n N` | Number of events to show (default: 10) |

### `exa modelzoo sync`

Manually trigger one GitLab poll cycle.

```bash
exa modelzoo sync
```

### `exa modelzoo config`

Show runtime ModelZoo config: `auto_retrain`, `poll_interval_seconds`, `watch_branch`.

```bash
exa modelzoo config
```

---

## exa pipeline

Prefect training pipeline operations. Run from the repo root (these commands invoke `pipelines/pipeline_generator.py` and `pipelines/deploy.py`).

### `exa pipeline list`

List all auto-discovered models and their supported datasets.

```bash
exa pipeline list
```

### `exa pipeline run`

Run training pipeline(s) locally via Prefect.

```bash
exa pipeline run --dummy                  # fast dev run for every model/dataset
exa pipeline run --model JPCP --dataset PM100Dataset  # one model, real data
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
exa pipeline run --model JPCP --dataset PM100Dataset --backend minio
exa pipeline run --env prod               # with YAML env overlay
exa pipeline run --registry pipelines/model_registry.yaml --env staging
```

| Option | Description |
|---|---|
| `--model MODEL`, `-m MODEL` | Run only this model |
| `--dataset DATASET`, `-d DATASET` | Run only this dataset class |
| `--dummy` | Use dummy data (dev-safe) |
| `--backend BACKEND`, `-b BACKEND` | Dataset backend — choices: `zenodo` \| `minio` \| `dataplane` |
| `--env ENV` | YAML registry env overlay — choices: `dev` \| `staging` \| `prod` |
| `--registry PATH` | Path to `model_registry.yaml` |

### `exa pipeline deploy`

Register Prefect deployments for all models (or one model). Default schedule: 2am UTC nightly.

```bash
exa pipeline deploy                           # all models, nightly schedule
exa pipeline deploy --no-schedule             # register without schedule
exa pipeline deploy --model JPCP              # one model only
exa pipeline deploy --env prod                # with env overlay
exa pipeline deploy --registry pipelines/model_registry.yaml --env prod
```

| Option | Description |
|---|---|
| `--no-schedule` | Deploy without a cron schedule (manual trigger only) |
| `--model MODEL`, `-m MODEL` | Deploy for a single model |
| `--registry PATH` | Path to `model_registry.yaml` |
| `--env ENV`, `-e ENV` | Registry env overlay — choices: `dev` \| `staging` \| `prod` |

### `exa pipeline validate`

Validate all `pipelines/models/*.yaml` files against their Python model shims. Runs the `test_registry_integrity.py` guard via pytest.

```bash
exa pipeline validate
```

### `exa pipeline export-registry`

Export auto-discovered model state to `pipelines/model_registry.yaml`.

```bash
exa pipeline export-registry
```

### `exa pipeline validate-model <model>`

Smoke-test a model alias on Ray Serve: check it responds and meets the latency SLA. Fires N dummy inference requests and reports average/max latency vs the threshold. Returns exit code 1 on failure — safe to use as a CI gate before promotion.

```bash
exa pipeline validate-model JPCP                              # Staging alias, 2.0s threshold, 3 requests
exa pipeline validate-model JPCP --alias Production           # check Production
exa pipeline validate-model JPCP --max-latency 0.5 --n 5     # strict SLA, 5 requests
exa --json pipeline validate-model JPCP                       # machine-readable output
```

| Option | Description |
|---|---|
| `MODEL` | Model name (e.g. `JPCP`) |
| `--alias ALIAS` | Alias to validate (default: `Staging`) |
| `--max-latency SECONDS` | Max acceptable average latency in seconds (default: `2.0`) |
| `--n N` | Number of smoke-test requests (default: `3`) |

### `exa pipeline promote <model>`

Promote a model alias when a metric threshold passes (metric-gated gate). Use `--if-<metric>-<op> <value>` to specify the threshold. Supported operators: `lt`, `gt`, `lte`, `gte`.

Because it writes the live `Production` alias, `promote` prompts for confirmation before the change (auto-confirmed under `--yes`, `--json`, or a non-interactive/CI stdin), writes an audit event, and refuses to promote on a degenerate (`NaN`/`Infinity`) metric value.

```bash
exa pipeline promote jpcp --if-rmse-lt 5.0                     # promote Staging → Production if RMSE < 5.0
exa pipeline promote jpcp --if-rmse-lt 5.0 --dry-run           # show outcome without promoting
exa pipeline promote jpcp --if-r2-gt 0.9 --from Staging --to Production
exa pipeline promote jpcp --if-mae-lt 3.0 --save               # promote and save rule to DB
exa pipeline promote --list                                     # list saved promotion rules
```

| Argument / Option | Description |
|---|---|
| `MODEL` | Model name (e.g. `jpcp`); omit with `--list` |
| `--if-<metric>-<op> VALUE` | Metric threshold, e.g. `--if-rmse-lt 5.0`, `--if-r2-gt 0.9` |
| `--from ALIAS` | Source alias (default: `Staging`) |
| `--to ALIAS` | Target alias (default: `Production`) |
| `--dry-run` | Show what would happen without promoting |
| `--save` | Save rule to DB for future reference |
| `--list` | List all saved promotion rules |

---

## exa scaffold

Scaffold a new model: model class, config shim, unit test, and YAML. Run from the repo root.

```bash
exa scaffold DemoAD
exa scaffold DemoAD --task anomaly_detection --type classification
exa scaffold DemoAD --force          # overwrite existing files
```

| Argument / Option | Description |
|---|---|
| `NAME` | PascalCase model name (e.g. `DemoAD`) |
| `--task TASK`, `-t TASK` | Model task — choices: `performance_prediction` \| `power_consumption_prediction` \| `anomaly_detection` (default: `performance_prediction`) |
| `--type TYPE`, `-T TYPE` | ML task type — choices: `regression` \| `classification` (default: `regression`) |
| `--force` | Overwrite existing files |

After scaffolding: edit `modelzoo/seanergys_modelzoo/models/tasks/<name>.py` and `pipelines/models/<NAME>.yaml`. The CI guard `tests/unit/test_registry_integrity.py` will fail if scaffolding is half-applied.

---

## exa seanerbus

SeanerBUS bridge UUID management. Each model has a stable UUID used as its identity on the SeanerBUS. Run these commands from the repo root.

### `exa seanerbus list`

Show all models and their SeanerBUS UUIDs. Reads directly from `pipelines/models/*.yaml` — no dashboard required.

```bash
exa seanerbus list
exa --json seanerbus list | jq '.[] | select(.uuid != "(not assigned)")'
```

### `exa seanerbus init-uuids`

Assign a UUID to every model that doesn't have one. Idempotent — safe to run multiple times. Commit the resulting YAML changes to git so HPC teams can see the stable UUIDs.

```bash
exa seanerbus init-uuids
git add pipelines/models/
git commit -m "feat: assign SeanerBUS UUIDs"
```

### `exa seanerbus regen-uuid <model>`

Regenerate the UUID for a single model. **HPC teams must be notified** — the old UUID will no longer be registered by the bridge.

```bash
exa seanerbus regen-uuid JPCP
```

### `exa seanerbus status`

Probe the SeanerBUS bridge `/health` and `/stats` endpoints and print the combined response.

```bash
exa seanerbus status
```

---

## exa stack

Docker Compose stack management. The canonical infrastructure interface is the Makefile
(`make stack-up`, `make stack-down`, `make stack-restart`, `make stack-logs`, `make stack-ps`).
The `exa stack` commands remain as thin compatibility wrappers for scripts that already use the CLI.

The `--service` option accepts any of these values (tab-completes after `--install-completion`):

```
postgres  minio  mlflow  orchestrator  ray-serving  control-plane
prometheus  alertmanager  tempo  grafana  loki  promtail
dashboard  jupyterhub  seanerbus-bridge
```

### `exa stack up`

Start the ExaMLOps stack (or a single service).

```bash
exa stack up                        # start full stack
exa stack up --service mlflow       # start only MLflow
```

### `exa stack down`

Stop the stack or a single service.

```bash
exa stack down
exa stack down --service ray-serving
```

### `exa stack restart`

Restart the stack or a single service.

```bash
exa stack restart
exa stack restart --service control-plane
```

### `exa stack logs`

Tail docker compose logs.

```bash
exa stack logs                         # all services, last 50 lines, follow
exa stack logs --service ray-serving
exa stack logs --service mlflow --tail 200
exa stack logs --no-follow             # print and exit
```

| Option | Description |
|---|---|
| `--service SERVICE`, `-s SERVICE` | Tail only this service (see valid service names above) |
| `--tail N`, `-n N` | Number of lines from end (default: 50) |
| `--follow / --no-follow`, `-f / -F` | Follow log output (default: follow) |

### `exa stack status`

Show running containers and their ports.

```bash
exa stack status
```

---

## JSON Output Mode

All commands support `--json` as a global flag. Output is machine-readable and suitable for piping to `jq`.

```bash
exa --json status
exa --json models list | jq '.[].Name'
exa --json approvals list | jq '.[] | select(.status == "pending")'
exa --json modelzoo status | jq '.models[] | select(.status == "stale")'
exa --json retrain JPCP | jq '.flow_run_id'
```

The `--json` flag must come **before** the subcommand:
```bash
exa --json models list    # correct
exa models list --json    # incorrect
```
