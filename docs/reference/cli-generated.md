# `exa`

ExaMLOps platform CLI — manage models, training, inference, and services.

- `--output, -o` — Output format: table (human) | json | yaml | csv (for scripting/agents)
- `--json` — Shorthand for --output json (kept for compatibility)
- `--context, -c` — Use a named config context for this invocation
- `--yes, -y` — Skip all confirmation prompts
- `--quiet, -q` — Suppress non-essential output (hints, info, progress detail)
- `--verbose, -v` — Show extra diagnostic detail
- `--version, -V` — Print version and exit

## `exa approvals`

Sysadmin approval gate

### `exa approvals approve`

Approve a pending model change — fires Prefect training immediately.

- `--dry-run` — Show what would be approved without firing training

### `exa approvals delete`

Delete a pending approval by its UUID (retract a stale or duplicate entry).

### `exa approvals list`

List model change approvals.

- `--all` — Show all statuses, not just pending

### `exa approvals reject`

Reject a pending model change — no training will run.

- `--reason, -r` — Rejection reason
- `--dry-run` — Show what would be rejected without changing anything

## `exa ask`

Ask the Skipper agent a question in natural language.

- `--session, -s` — Session id to preserve conversational context

## `exa audit`

Show platform audit log — who did what and when.

- `--last` — Time window (e.g. 7d, 30d)
- `--model, -m` — Filter by target model
- `--action, -a` — Filter by action type
- `--source, -s` — Filter by source (cli/agent/bridge)
- `--limit, -n` — Max events to show

## `exa config`

CLI configuration

### `exa config contexts`

List configured contexts (environments) and show the active one.

### `exa config init`

Interactive wizard — write ~/.config/examlops/config.toml.

### `exa config set`

Set a single config key in ~/.config/examlops/config.toml.

- `--context, -c` — Write into a named context instead of the default

### `exa config show`

Print the current resolved config (env vars + TOML file).

### `exa config use`

Switch the active context (environment).

## `exa docs`

Generate the full command reference from the live CLI tree.

- `--out` — Write Markdown to this file instead of stdout

## `exa doctor`

Diagnose your ExaMLOps setup: config, connectivity, and DB health.

## `exa drift`

Prediction drift detection

### `exa drift auto-retrain`

#### `exa drift auto-retrain disable`

Disable drift-triggered auto-retrain for a model.

#### `exa drift auto-retrain enable`

Enable drift-triggered auto-retrain for a model.

- `--dataset, -d` — Dataset class name
- `--min-z` — Z-score threshold to trigger retrain
- `--cooldown` — Seconds between triggers

#### `exa drift auto-retrain status`

Show auto-retrain config for all models.

### `exa drift baseline`

Store current rolling stats as the drift baseline for a model.

- `--dry-run` — Show the baseline that would be set without writing it

### `exa drift input`

#### `exa drift input baseline`

Store current rolling embedding statistics as the input drift baseline.

- `--dry-run` — Show the input baseline that would be set without writing it

#### `exa drift input reset`

Clear all input embedding snapshots for a model (keeps baseline).

- `--dry-run` — Show how many snapshots would be cleared without deleting them

#### `exa drift input status`

Show input embedding distribution drift for all models (or one model).

### `exa drift reset`

Clear all drift snapshots for a model (keeps baseline).

- `--dry-run` — Show how many snapshots would be cleared without deleting them

### `exa drift snapshots`

Show raw prediction drift snapshots for a model.

- `--last, -n` — Number of recent snapshots
- `--raw` — Show all columns including job_id

### `exa drift status`

Show prediction drift status for all models (or one model).

- `--watch, -w` — Live auto-refreshing view (Ctrl-C to exit)
- `--interval` — Refresh interval in seconds for --watch

### `exa drift trigger`

Check drift z-scores and fire POST /retrain for models above threshold.

- `--dry-run` — Show what would be triggered without firing

## `exa env`

Show the effective configuration and the source of every value.

## `exa eval`

Continuous evaluation and feedback

### `exa eval feedback`

Ground-truth feedback loop

#### `exa eval feedback accuracy`

Compute live accuracy (RMSE/MAE) from labelled predictions — real quality, not drift proxy.

- `--alias, -a` — Restrict to one MLflow alias
- `--record` — Persist computed metrics to the live_metrics table

#### `exa eval feedback ingest`

Ingest delayed ground-truth label(s), keyed by prediction request_hash.

- `--request-hash, -r` — Join key of the prediction to label
- `--label, -l` — Observed ground-truth value
- `--source, -s` — Provenance of the label
- `--from-csv` — Bulk-ingest a CSV with columns request_hash,label[,source]

#### `exa eval feedback join`

Show prediction/label pairs joined on request_hash (delayed-label join).

- `--alias, -a` — Filter by MLflow alias

## `exa explain`

Explain what a command does, in plain language, with examples.

## `exa features`

Feature store — versioned training features

### `exa features list`

List feature versions in the store.

### `exa features pull`

Pull a feature file from the store.

- `--name, -n` — Feature set name
- `--version, -v` — Specific version (default: latest)
- `--output, -o` — Copy file to this path

### `exa features push`

Push a feature file into the versioned feature store.

- `--name, -n` — Feature set name

## `exa finops`

FinOps + Green-AI budgets and carbon accounting

### `exa finops budget`

Per-project GPU-hour / cost budgets (project = namespace).

#### `exa finops budget set`

Set (or replace) a project's GPU-hour / cost budget.

- `--gpu-hours` — GPU-hour budget
- `--cost` — Cost budget (USD)
- `--period` — Budget period label

#### `exa finops budget status`

Show budget vs recorded consumption (GPU-hours + cost) per project.

### `exa finops carbon`

Energy (kWh) and CO2e accounting for training runs.

#### `exa finops carbon estimate`

Estimate energy (kWh) and CO2e (g) for a number of GPU-hours (no DB write).

The formula is provided by the active carbon *provider* — a built-in, an entry-point plugin, or
a declarative YAML formula. Defaults reproduce the platform's original methodology exactly.

- `--gpu-hours` — GPU-hours to estimate
- `--grid-intensity` — gCO2e per kWh
- `--provider` — Carbon provider (default: green-ai-default). See: carbon providers
- `--pue` — Override datacentre PUE
- `--gpu-tdp` — Override GPU TDP (watts)

#### `exa finops carbon providers`

List the available carbon providers (built-ins + entry-point plugins) and their status.

#### `exa finops carbon record`

Estimate (via the active provider) and persist a carbon record for a training run.

- `--gpu-hours` — GPU-hours consumed by the run
- `--run-id` — MLflow run id
- `--grid-intensity` — gCO2e per kWh
- `--provider` — Carbon provider (default: green-ai-default). See: carbon providers
- `--pue` — Override datacentre PUE
- `--gpu-tdp` — Override GPU TDP (watts)

#### `exa finops carbon report`

Aggregate recorded energy and carbon (optionally for one model).

- `--model, -m` — Filter to one model

### `exa finops cost`

HPC cost providers (pluggable rate cards). Estimation runs via 'exa models cost'.

#### `exa finops cost providers`

List the available cost providers (rate cards) — built-ins + entry-point plugins.

## `exa hpc`

HPC fleet — discover schedulers, nodes, and GPUs

### `exa hpc approve`

Sysadmin: approve a cluster so exaMLOps may schedule jobs on it.

### `exa hpc capacity`

Per-cluster GPU capacity, utilization, GPU-hours used and cost (ACTIVE clusters).

### `exa hpc clusters`

List registered clusters and their approval state.

### `exa hpc connect`

Probe a host and register it as a PENDING cluster (requires approval to use).

- `--name, -n` — Cluster name (default: host)
- `--user, -u` — SSH user
- `--key, -k` — SSH private-key path
- `--port, -p` — SSH port
- `--scheduler, -s` — Force scheduler; default auto

### `exa hpc detect`

Auto-detect the scheduler on a host and suggest a configuration (read-only).

- `--user, -u` — SSH user
- `--key, -k` — SSH private-key path
- `--port, -p` — SSH port

### `exa hpc gpus`

List GPU devices — model, memory, utilization, online status (read-only).

- `--host, -H` — Host to probe (omit = local)
- `--user, -u` — SSH user
- `--key, -k` — SSH private-key path
- `--port, -p` — SSH port
- `--scheduler, -s` — Force a probe; default auto

### `exa hpc jobs`

List tracked HPC submissions from platform.db (hpc_jobs).

- `--model, -m` — Filter by model
- `--limit, -n` — Max rows

### `exa hpc nodes`

List compute nodes with CPUs/memory/GPUs and normalized state (read-only).

- `--host, -H` — Login-node host (omit = local)
- `--user, -u` — SSH user
- `--key, -k` — SSH private-key path
- `--port, -p` — SSH port
- `--scheduler, -s` — Force a probe (flux|slurm|nvidia-smi); default auto
- `--save` — Persist the inventory snapshot to platform.db
- `--cluster, -c` — Cluster name for --save

### `exa hpc place`

Show which ACTIVE cluster placement would choose for a resource ask.

- `--gpus, -g` — GPUs the job needs
- `--cpus` — CPUs the job needs
- `--nodes, -N` — Nodes the job needs
- `--placement-provider` — Placement scoring provider (default: least-loaded)

### `exa hpc preflight`

Fail-fast pre-submit checks against a cluster (exit 1 on any failure).

- `--gpus, -g` — GPUs the job will request
- `--nodes, -N` — Nodes the job will request

### `exa hpc queue`

Show the live scheduler queue for an ACTIVE cluster (read-only).

- `--cluster, -c` — ACTIVE cluster to query

### `exa hpc reject`

Sysadmin: reject a cluster (blocks scheduling; auditable).

- `--reason, -r` — Why the cluster is rejected

## `exa mcp`

MCP server + Agent-to-Agent (A2A) surface

### `exa mcp agent-card`

Print the A2A Agent Card describing this platform's agent skills.

- `--url` — Public base URL where this agent is reachable
- `--all` — Advertise mutating tools in the card

### `exa mcp prompts`

List the MCP prompts (reusable agent workflows) ExaMLOps ships.

### `exa mcp resources`

List the MCP resources (readable context) ExaMLOps exposes to agents.

### `exa mcp serve`

Run the MCP server so agents can drive ExaMLOps.

- `--transport` — Transport for the MCP server
- `--host` — Bind host (http transport)
- `--port` — Bind port (http transport)
- `--allow-writes` — Register mutating tools (retrain). Off by default.

### `exa mcp tools`

List the tools ExaMLOps exposes to agents over MCP.

- `--all` — Include mutating (write) tools even if writes are disabled

## `exa models`

MLflow model registry

### `exa models card`

Generate model cards

#### `exa models card generate`

Generate a standardised model card document.

- `--output, -o` — Write card to this file path instead of stdout

#### `exa models card history`

Show model card generation history.

### `exa models cost`

Show HPC cost history for a model.  Use --record to ingest new data.

- `--record` — Fetch latest scheduler data (Slurm/Flux), record to DB and tag MLflow

### `exa models cost-list`

Show HPC cost summary across all models.

### `exa models diff`

Compare metrics and params between two model versions.

### `exa models info`

Show detail for one model: all versions, aliases, metrics.

### `exa models lineage`

Show the pipeline → dataset → model version lineage chain.

### `exa models list`

List all registered models with their production alias and latest version.

### `exa models rollback`

Roll back a model alias to a previous version

#### `exa models rollback history`

Show rollback history for a model (last 20 events).

#### `exa models rollback run`

Roll back a model alias (default: Production) to a specified or selected version.

- `--version, -v` — Target version number to roll back to
- `--alias, -a` — Alias to reassign (default: Production)
- `--reason, -r` — Optional reason for the rollback
- `--dry-run, -n` — Preview without applying the change

## `exa modelzoo`

ModelZoo repository freshness and events

### `exa modelzoo config`

Show ModelZoo integration configuration.

### `exa modelzoo config-set`

Update ModelZoo integration config on the Control Plane.

### `exa modelzoo events`

Show recent ModelZoo push events.

- `--limit, -n` — Number of events to show

### `exa modelzoo status`

Show ModelZoo freshness for every registered model.

### `exa modelzoo sync`

Manually trigger one ModelZoo poll cycle.

## `exa namespace`

Project namespace isolation

### `exa namespace assign`

Assign a model to a namespace.

- `--namespace, -n` — Target namespace name

### `exa namespace create`

Create a new namespace.

- `--description, -d` — Optional description

### `exa namespace info`

Show namespace details and the models assigned to it.

### `exa namespace list`

List all namespaces with model counts.

## `exa pipeline`

Prefect training pipeline

### `exa pipeline add-model`

Register an existing modelzoo model into the training pipeline.

Unlike exa scaffold, this command does NOT create a new model class.
It only generates the pipeline YAML and config shim for a model class that
already lives in modelzoo/modelzoo/models/tasks/.

Use this when you have written a model class by hand or imported one from
the modelzoo and want to wire it into ExaMLOps training and inference.

- `--task, -t` — Task type [performance_prediction | power_consumption_prediction | anomaly_detection]
- `--type, -T` — ML type [regression | classification]
- `--metric` — Promotion metric
- `--threshold` — Production promotion threshold
- `--direction` — [higher_is_better | lower_is_better]
- `--force` — Overwrite existing YAML/config files

### `exa pipeline deploy`

Register Prefect deployments for all models (or one model).

- `--no-schedule` — Deploy without a cron schedule
- `--model, -m` — Deploy for a single model only
- `--registry` — Path to model_registry.yaml
- `--env, -e` — Registry env overlay

### `exa pipeline export-registry`

Export auto-discovered model state to pipelines/model_registry.yaml.

### `exa pipeline hpo`

Hyperparameter optimisation

#### `exa pipeline hpo record`

Record an HPO trial result.

- `--trial` — Trial number
- `--params` — Trial parameters as JSON string
- `--value` — Metric value for this trial

#### `exa pipeline hpo start`

Trigger an HPO study via the Control Plane.

- `--trials, -t` — Number of HPO trials
- `--metric, -m` — Metric to optimise
- `--dataset, -d` — Dataset class name

#### `exa pipeline hpo status`

Show HPO study status.

### `exa pipeline list`

List all auto-discovered models and their supported datasets.

### `exa pipeline promote`

Promote a model alias when a metric threshold passes (rule-based gate).

Specify metric threshold with --if-<metric>-<op> <value>, e.g. --if-rmse-lt 5.0

- `--from` — Source alias
- `--to` — Target alias
- `--dry-run` — Show outcome without promoting
- `--save` — Save rule to DB for future reference
- `--list` — List saved promotion rules

### `exa pipeline promote-delete`

Delete saved metric-gated promotion rules.

- `--all` — Delete ALL promotion rules

### `exa pipeline quality`

Data quality validation gates

#### `exa pipeline quality check`

Run data quality checks for a model/dataset pair and record results.

#### `exa pipeline quality history`

Show data quality check history for a model (last 20 runs).

### `exa pipeline run`

Run training pipeline(s) locally via Prefect.

- `--model, -m` — Run for a single model only
- `--dataset, -d` — Run for a single dataset class only
- `--dummy` — Use dummy data (dev-safe)
- `--backend, -b` — Dataset storage backend
- `--env` — YAML registry env overlay
- `--registry` — Path to model_registry.yaml
- `--cluster, -C` — Target an ACTIVE HPC cluster by name, or 'auto' to let placement choose
- `--gpus, -g` — GPUs to request (for --cluster auto placement)

### `exa pipeline validate`

Validate pipelines/models/*.yaml against Python model shims.

### `exa pipeline validate-model`

Smoke-test a model alias on Ray Serve: check it responds and meets latency SLA.

Returns exit code 0 on PASS, 1 on FAIL. Safe to use as a gate before promotion.

- `--alias` — Alias to validate
- `--max-latency` — Max acceptable latency in seconds
- `--n` — Number of smoke-test requests

## `exa plugins`

List installed exa CLI plugins and whether each loaded successfully.

## `exa policy`

Policy-as-code — declarative governance for mutations

### `exa policy list`

List the policy rules currently loaded from policy.yaml.

### `exa policy test`

Evaluate the policy decision for an action + context (not audited).

- `--set, -s` — Context key=value (repeatable), e.g. --set env=dev

## `exa predict`

Send an inference request to the Ray Serve inference pipeline.

- `--features, -f` — JSON feature dict
- `--alias` — MLflow alias
- `--version` — Explicit model version

## `exa production`

Production deployment and verification

### `exa production deploy`

Plan/execute production deploys, or inspect deploy history/status.

- `--execute` — Actually deploy/retrain/reload. Default is a side-effect-free dry run.
- `--models` — Model selector: 'stale', 'all', or comma-separated IDs such as JPCP,MACK.
- `--dataset` — Dataset for retraining selected models
- `--env, -e` — Registry env overlay
- `--registry` — Path to model_registry.yaml
- `--no-schedule` — Deploy Prefect flows without schedules
- `--limit` — Number of deploy history records to show.
- `--status` — Filter deploy history by status.
- `--model` — Filter deploy history by model ID.
- `--operation` — Filter deploy history by operation.

### `exa production verify`

Verify production service health without changing state.

## `exa providers`

Pluggable calculation providers (all domains)

### `exa providers list`

List calculation providers across every domain (built-ins + entry-point plugins + config).

- `--domain, -d` — Only this domain (default: all known domains)

## `exa retrain`

Trigger a Prefect training run via the Control Plane.

- `--dataset, -d` — Dataset class name
- `--dummy` — Use dummy data (dev-safe, no downloads)
- `--backend` — Storage backend
- `--dry-run` — Show what would be scheduled without triggering it

## `exa scaffold`

Scaffold a new model: model class, config, unit test, and YAML.

- `--task, -t` — Task type
- `--type, -T` — ML task type
- `--force` — Overwrite existing files

## `exa dataplane`

DataPlane bridge UUID management

### `exa dataplane init-uuids`

Assign a DataPlane UUID to every model that doesn't have one. Idempotent.

### `exa dataplane list`

Show all models and their DataPlane UUIDs.

### `exa dataplane regen-uuid`

Regenerate the DataPlane UUID for one model. Notify HPC teams of the change.

### `exa dataplane status`

Probe the DataPlane bridge health and runtime stats endpoints.

## `exa serve`

Ray Serve operations

### `exa serve ab`

A/B testing experiments

#### `exa serve ab analyze`

Run a statistical test on the recorded observations of a model's active A/B test.

Uses Welch's t-test (unequal variance) and reports whether the difference between the
two variants is significant, plus the winner given the metric's optimisation direction.

- `--lower-is-better` — Metric where smaller wins (e.g. RMSE, latency); default higher-is-better
- `--alpha` — Significance level
- `--min-sample` — Minimum observations per variant before calling a winner

#### `exa serve ab record`

Record a metric observation for the active A/B test of a model.

#### `exa serve ab start`

Start a new A/B test comparing two model variants.

- `--variant-a, -a` — First variant alias
- `--variant-b, -b` — Second variant alias
- `--split, -s` — % of traffic routed to variant_a (rest goes to variant_b)
- `--name, -n` — Optional experiment name

#### `exa serve ab status`

Show A/B tests (most recent 20).

#### `exa serve ab stop`

Stop the running A/B test for a model.

### `exa serve batch`

Batch inference jobs

#### `exa serve batch list`

List recent batch inference jobs.

- `--model, -m` — Filter by model name

#### `exa serve batch submit`

Run synchronous batch inference from a JSON/JSONL input file.

- `--alias, -a` — MLflow alias to target
- `--output, -o` — Write predictions JSON here

### `exa serve benchmark`

Benchmark Ray Serve using the dummy client and report latency stats.

- `--requests, -n` — Number of benchmark requests

### `exa serve check`

Smoke test Ray Serve: health check + one prediction per model.

### `exa serve explain`

Feature importance explanations (XAI)

#### `exa serve explain explain`

Request feature-importance scores from the Ray Serve explain endpoint.

- `--input-json, -i` — JSON-encoded input dict (default: empty dict)
- `--top-n, -n` — Number of top features to show
- `--alias, -a` — MLflow alias to explain

#### `exa serve explain history`

Show recent explain requests for a model.

### `exa serve infer-check`

Smoke-test the Ray Serve inference pipeline with a valid synthetic HPC job.

### `exa serve models`

List models currently hot-loaded in Ray Serve.

- `--detail, -d` — Show full model detail

### `exa serve reload`

Hot-reload Production models from MLflow into Ray Serve.

- `--model, -m` — Reload one model (default: all)

### `exa serve shadow`

Shadow deployment traffic mirroring

#### `exa serve shadow disable`

Disable shadow deployment for a model.

#### `exa serve shadow enable`

Enable shadow deployment for a model.

- `--shadow-alias, -a` — MLflow alias to mirror traffic to

#### `exa serve shadow log`

Show last 20 shadow inference comparison results for a model.

#### `exa serve shadow status`

Show shadow deployment configuration.

### `exa serve traffic`

Show or set traffic split across model aliases (must sum to 100).

- `--production` — % traffic to Production alias
- `--canary` — % traffic to Canary alias
- `--staging` — % traffic to Staging alias
- `--dry-run` — Show the split that would be applied without changing routing

### `exa serve traffic-list`

Show traffic split configuration for all models.

- `--watch, -w` — Live auto-refreshing view (Ctrl-C to exit)
- `--interval` — Refresh interval in seconds for --watch

## `exa stack`

Docker Compose stack

### `exa stack down`

Stop the stack (or a single service).

- `--service, -s` — Stop only one service

### `exa stack logs`

Tail docker compose logs.

- `--service, -s` — Show logs for one service
- `--tail, -n` — Number of lines to tail
- `--follow, -f` — Follow log output

### `exa stack monitoring-down`

Stop monitoring stack.

### `exa stack monitoring-status`

Show monitoring stack container status.

### `exa stack monitoring-up`

Start monitoring stack: Prometheus, Grafana, Loki, Promtail, Alertmanager, Tempo.

### `exa stack restart`

Restart the stack or a single service.

- `--service, -s` — Restart only one service

### `exa stack status`

Show running containers and their ports.

### `exa stack up`

Start the ExaMLOps stack (or a single service).

- `--service, -s` — Start only one service

## `exa status`

Platform snapshot: service health, pending approvals, production models.

- `--watch, -w` — Live auto-refreshing view (Ctrl-C to exit)
- `--interval` — Refresh interval in seconds for --watch
