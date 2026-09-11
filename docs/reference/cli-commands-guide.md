# `exa` CLI — Command Guide (What · Use case · Example)

The `exa` CLI is the primary operator interface for ExaMLOps. This guide documents **every
command** — what it does, *when and why* you'd reach for it, and a copy-paste example —
organized by the twelve MLOps-lifecycle areas that `exa --help` groups commands into.

> **In the terminal, too.** Running a bare group — e.g. `exa serve` — prints its subcommands in
> titled panels plus a **Common tasks** block (copy-paste examples) and a **Learn more** footer
> (`exa <group> <command> -h`, `exa explain <group>`, and a pointer back to this guide). So the
> same "what can I do here / show me" help this document gives is one keystroke away on the CLI.

> **Conventions**
> - Every example is grounded in the live CLI (`exa` v0.48.0 — 365 leaf commands under 62 groups,
>   held to the live tree by `tests/unit/test_cli_guide_coverage.py`). Run any command with `-h`/`--help`
>   for its full options, or `exa explain <cmd>` for plain-language help.
> - **Mutating / outward-facing** commands (training runs, deploys, promotions, approvals, secret
>   writes, sends) are marked *(mutation)*. Where possible the example uses `--dry-run`/`--dummy`;
>   treat these as real actions.
> - Model names: the registry uses uppercase (`JPCP`); MLflow uses lowercase (`jpcp`). Common
>   demo values used below: model `JPCP`/`jpcp`, datasets `PM100Dataset`/`FDataDataset`/`FData`,
>   project `minio-demo`, aliases `Production`/`Canary`/`Staging`.
> - Global flags on every command: `--output/-o table|json|yaml|csv|md|html`, `--json`,
>   `--context/-c`, `--yes/-y`, `--quiet/-q`, `--verbose/-v`.

**Related:** [Dashboard Usage Guide](../dashboard/usage-guide.md) · [Full generated reference](cli-generated.md) · [Verified one-liners](cli-examples.md)

---

## Getting Started

First-touch commands for orienting yourself on an ExaMLOps deployment: check platform health, diagnose your setup, ask questions in plain English, and point the CLI at the right environment. Most commands here are read-only; the `config` group and its wizard write to `~/.config/examlops/config.toml`.

### `exa status` — platform snapshot at a glance

Shows service health, pending approvals, and production models in one view. This is the default landing command when you sit down at a terminal.

The health table's **Checked** column is the address that was actually probed. Every verdict except
the control plane's own comes from the control plane, so under Docker Compose the addresses are
in-network service names (`http://mlflow:5000`, `http://orchestrator:4200/api`) rather than the host
port map you would open in a browser — open the address shown, not the one you expect. A `?` means
the control plane is an older build that does not report what it checked; `exa` says so instead of
guessing. A service the control plane does not report at all is shown as **— not reported**, not as
unreachable: nothing was probed, so nothing is claimed.

**Production models** come from the MLflow registry, which is where lifecycle aliases live — the
control plane's `/status` does not carry them. A model appears when it holds a `Production` or
`Staging` alias. Three outcomes are distinguished, and none of them is silence: the table, `No
model carries a Production or Staging alias` when the registry was read and holds none, and
`production models unknown` when the registry could not be read (including when the health table
already shows MLflow down, in which case it is not probed a second time). `exa --json status`
carries the same `production_models` list the table shows.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa status` | Prints a platform snapshot: service health, pending approvals, and production models. | Your first-thing-in-the-morning check that MLflow/Prefect/Ray/control-plane are up and nothing is stuck awaiting approval. | `exa status`<br>`exa status --watch --interval 10` |

### `exa doctor` — diagnose your setup

Runs a self-check over configuration, connectivity, and database health so you can tell a local misconfiguration from a real outage.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa doctor` | Diagnoses config, service connectivity, and platform DB health, reporting each check. | When `exa status` looks wrong or a command errors and you need to know whether it's your config, the network, or the DB. | `exa doctor`<br>`exa --json doctor` |

### `exa explain` — plain-language command help

Introspects the CLI tree to explain what any command does, with examples. Complements `--help` when you want intent rather than raw flags.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa explain [command …]` | Explains a command (or lists all commands if none given) in plain language with examples. | When you're learning the CLI and want the "why/when" for a command, not just its flag list. | `exa explain drift`<br>`exa explain serve reload` |

### `exa env` — effective config and its provenance

Prints the resolved configuration and where each value came from (env var, context, file, or default), with secrets redacted. Add `--validate` to fail on incoherent config.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa env` | Shows the effective config plus the source of every value (secrets redacted); `--validate` cross-checks coherence and exits 1 on errors. | To confirm which endpoints/tokens the CLI will actually use, or to gate CI on a coherent, non-placeholder environment. | `exa env`<br>`exa env --validate` |

### `exa docs` — generate the command reference from live code

Renders the full command reference as Markdown straight from the running Typer/Click tree, so published docs can never drift from the code.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa docs` | Generates the full CLI command reference as Markdown from the live command tree; `--out` writes to a file. | To regenerate `docs/reference/cli-generated.md` after CLI changes (`make docs-cli` does exactly this), or to get the command tree as JSON for tooling. | `exa docs`<br>`exa docs --out docs/reference/cli-generated.md` |

### `exa config` — CLI configuration

Manages the CLI's own settings and named environment contexts in `~/.config/examlops/config.toml`. The `set`, `init`, and `use` subcommands are mutations that write this file.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa config show` | Prints the current resolved config (env vars merged with the TOML file). | Quick read of what's configured, without the provenance detail `exa env` adds. | `exa config show` |
| `exa config init` | Interactive wizard that writes `~/.config/examlops/config.toml`. **Mutation** (writes config, prompts interactively). | First-time setup on a new machine when you'd rather answer prompts than set keys one by one. | `exa config init` |
| `exa config set <key> [value]` | Sets a config key; `--context` targets a named context. Omit secret values to use the hidden prompt. **Mutation** (writes config). | Point the CLI at a service or store a token without shell-history exposure. | `exa config set control_plane http://<REMOTE_HOST>:18002`<br>`exa config set agent_token --context production` |
| `exa config contexts` | Lists configured contexts (environments) and marks the active one. | To see which environments are defined and which one commands will hit right now. | `exa config contexts` |
| `exa config use <name>` | Switches the active context (environment). **Mutation** (writes `active_context`). | Flip between, e.g., a local dev context and the `lxp` remote server without re-typing endpoints. | `exa config use lxp` |
| `exa config export` | One-file YAML snapshot of **all** platform configuration, generated live: CLI settings with provenance, contexts, HPC cluster registry, artifact-vs-dataset object-store split, per-model YAMLs, env overlays, FinOps providers, and every platform env var (secrets redacted). Read-only view — edit the underlying sources, not the snapshot. | Inspect a whole deployment at a glance, attach config to a bug report, or `diff` two environments (run it on the laptop and on lxp, then diff the files). | `exa config export`<br>`exa config export -o examlops-config.yaml`<br>`exa --json config export` |

### `exa plugins` — installed plugin inventory

Lists third-party `exa` subcommands discovered via the `examlops.cli_plugins` entry-point group and whether each loaded cleanly.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa plugins` | Lists installed CLI plugins and each one's load status (loaded/failed). | To confirm a newly `pip install`ed plugin is registered, or to debug why a plugin's command isn't showing up. | `exa plugins`<br>`exa --json plugins` |

## Training & Pipelines

Commands that turn a model definition into a trained, versioned, promotable artifact: auto-discovery-based Prefect training, control-plane retraining, model scaffolding, LLM fine-tuning, and reproducibility bundles. Commands that create runs, deploy schedules, promote aliases, or write files are **real mutations** — the examples below use `--dummy`/`--dry-run`/`--no-schedule` so they are safe to copy-paste; drop those flags to act for real.

### `exa pipeline` — Prefect training pipeline

Auto-discovers models and datasets, runs and deploys their Prefect training flows, and gates promotion with metric, latency, and data-quality checks. Includes distributed-training (`distributed`), HPO (`hpo`), and data-quality (`quality`) subgroups.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa pipeline list` | Lists all auto-discovered models and their supported dataset classes. | See what can be trained before running anything. | `exa pipeline list` |
| `exa pipeline run` | Runs training pipeline(s) locally via Prefect (**mutation** — trains and logs to MLflow). | Train one or all models, optionally pinned to a dataset revision, backend, or HPC cluster. | `exa pipeline run --model JPCP --dataset PM100Dataset --dummy`<br>`exa pipeline run --model JPCP --dataset PM100Dataset --backend minio --project minio-demo` |
| `exa pipeline deploy` | Registers Prefect deployments for all models (or one) (**mutation** — creates Prefect deployments). | Wire models into scheduled/manual Prefect runs. | `exa pipeline deploy --no-schedule`<br>`exa pipeline deploy --model JPCP --env staging` |
| `exa pipeline validate` | Validates the pack's `models/*.yaml` against the Python model shims. | Catch config/shim drift before deploying (CI gate). | `exa pipeline validate` |
| `exa pipeline validate-model` | Smoke-tests a model alias on Ray Serve for response + latency SLA; exit 1 on breach. | Gate promotion on live serving latency in CI. | `exa pipeline validate-model JPCP --max-latency 0.5 --n 10` |
| `exa pipeline promote` | Promotes a model alias when a `--if-<metric>-<op>` threshold passes (**mutation** — moves the MLflow alias). | Rule-based Staging→Production promotion gated on a metric. | `exa pipeline promote jpcp --if-rmse-lt 5.0 --dry-run`<br>`exa pipeline promote jpcp --if-rmse-lt 5.0 --save` |
| `exa pipeline promote-delete` | Deletes saved metric-gated promotion rules (**mutation** — removes DB rules). | Clean up obsolete promotion rules. | `exa pipeline promote-delete jpcp`<br>`exa pipeline promote-delete --all` |
| `exa pipeline add-model` | Wires an existing modelzoo model class into the pipeline by generating its YAML + config shim (**mutation** — writes files); does not create a new class. | Register a hand-written or imported model class for training/inference. | `exa pipeline add-model DemoAD --task anomaly_detection --type classification` |
| `exa pipeline export-registry` | Exports auto-discovered model state to `pipelines/model_registry.yaml` (**mutation** — writes the registry file). | Snapshot discovered models into a versionable registry. | `exa pipeline export-registry` |

#### `exa pipeline distributed` — distributed training + checkpoint/resume (E6)

Multi-node/multi-GPU training with integrity-hashed sharded checkpoints and resume.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa pipeline distributed launch` | Launches a distributed training run across nodes/GPUs with a chosen strategy (**mutation** — starts a run). | Kick off FSDP/ZeRO/Megatron multi-node training. | `exa pipeline distributed launch JPCP --nodes 2 --gpus-per-node 4 --strategy fsdp --checkpoint-every 500` |
| `exa pipeline distributed checkpoint` | Writes an integrity-hashed sharded checkpoint for a run (**mutation** — persists checkpoint). | Manually snapshot training state mid-run. | `exa pipeline distributed checkpoint <run_id> --step 1000 --epoch 3 --shards 4` |
| `exa pipeline distributed resume` | Resumes from the last integrity-valid checkpoint (**mutation**); exit 1 if none valid. | Recover a crashed distributed run without losing progress. | `exa pipeline distributed resume <run_id>` |
| `exa pipeline distributed status` | Shows a distributed run and its checkpoints. | Inspect run progress and checkpoint health. | `exa pipeline distributed status <run_id>` |
| `exa pipeline distributed list` | Lists distributed training runs (optionally for one model). | Review recent distributed runs. | `exa pipeline distributed list JPCP` |

#### `exa pipeline hpo` — hyperparameter optimisation

Trigger and record hyperparameter-optimisation studies via the Control Plane.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa pipeline hpo start` | Triggers an HPO study for a model via the Control Plane (**mutation** — schedules trials). | Search hyperparameters to optimise a target metric. | `exa pipeline hpo start JPCP --trials 20 --metric rmse --dataset PM100Dataset` |
| `exa pipeline hpo record` | Records a single HPO trial's result (**mutation** — writes trial to DB). | Log a trial from an external/worker HPO loop. | `exa pipeline hpo record JPCP --trial 3 --params '{"lr":0.01}' --value 4.8` |
| `exa pipeline hpo status` | Shows HPO study status (optionally for one model). | Track trial progress and best value so far. | `exa pipeline hpo status JPCP` |

#### `exa pipeline quality` — data quality validation gates

Run and review data-quality checks per model/dataset pair.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa pipeline quality check` | Runs data-quality checks for a model/dataset pair and records results (**mutation** — writes results). | Validate input data before training or promotion (CI gate). | `exa pipeline quality check JPCP PM100Dataset` |
| `exa pipeline quality history` | Shows a model's data-quality check history (last 20 runs). | Audit data-quality trends over time. | `exa pipeline quality history JPCP` |

### `exa retrain` — trigger a training run via the Control Plane

Fires a Prefect training run through the Control Plane API (requires `CONTROL_PLANE_TOKEN`), with a confirmation prompt and an audit-trail entry.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa retrain` | Triggers a Prefect retrain for a model via the Control Plane (**mutation** — schedules a run; audited). | Operator/client-driven retraining without local Prefect. | `exa retrain JPCP --dry-run`<br>`exa retrain JPCP --dataset PM100Dataset --backend minio --reason "drift detected"` |

### `exa scaffold` — scaffold a new model

Generates a complete new model skeleton — model class, config shim, unit test, and pipeline YAML — ready to edit.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa scaffold` | Scaffolds a new model's class, config, test, and YAML (**mutation** — writes files). | Start a brand-new model from a task/type template. | `exa scaffold DemoAD --task anomaly_detection --type classification`<br>`exa scaffold DemoAD --force` |

### `exa finetune` — fine-tune and register an adapter

Runs an LLM fine-tune (LoRA/QLoRA/full) and registers a signed, lineage-linked adapter with an optional eval-floor promotion gate.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa finetune` | Fine-tunes a base model and registers a signed, lineage-linked adapter (**mutation** — trains + registers). | Produce a task-specific LoRA adapter with recorded eval and cost. | `exa finetune llama3.1-8b --method lora --dataset <rev> --rank 8 --eval 0.82 --eval-floor 0.75` |

### `exa reproduce` — reproducibility bundles (A8)

Capture a signed manifest of a model version's inputs (dataset revision, seed, hyperparameters, metrics, image digest), then verify or re-run it later. Never claims bit-exactness — it metric-matches within tolerance.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa reproduce build` | Captures and signs a reproducibility bundle for a model version (**mutation** — writes a signed manifest). | Freeze the exact recipe behind a trained model version. | `exa reproduce build JPCP 17 --dataset PM100Dataset --revision <rev> --seed 42 --metrics '{"rmse":4.9}'` |
| `exa reproduce run` | Rebuilds the recorded plan and metric-matches observed vs recorded within tolerance. | Confirm a model can be reproduced from its bundle. | `exa reproduce run JPCP 17 --observed '{"rmse":4.9}'` |
| `exa reproduce verify` | Checks that referenced inputs still exist and hashes match; exit 1 if rotted (R5/GWT-4). | Detect bit-rot / drift of a bundle's inputs (CI gate). | `exa reproduce verify JPCP 17` |
| `exa reproduce list` | Lists reproducibility bundles (optionally for one model). | Review which model versions have bundles. | `exa reproduce list JPCP` |

## Data & Features

Everything upstream of training: dataset versioning and reproducibility, the two feature stores, asset-centric freshness pipelines, and the data/model card layer.

Two groups have deceptively similar names and are **distinct**:

- **`exa feature`** (singular) — the *serving* feature store (A3). One train+serve **feature-view** definition keyed on an entity, with an offline point-in-time source and an online store, purpose-built to guarantee **zero train/serve skew**. Think Feast-style: `apply` a view, `ingest`/`materialize` values, `get` a vector, assert `skew`.
- **`exa features`** (plural) — the *versioned training-feature store*. A simple `push`/`pull`/`list` artifact store for versioned feature **files** (e.g. an engineered parquet) attached to a model.

Mutating commands (`data snapshot`, `data retention-prune`, `data synth generate`, `feature apply/ingest/materialize`, `assets declare/materialize/source-changed`, `features push`, `cards … --save/--out`) change state or write files — several offer `--dry-run`; none were executed here.

### `exa data` — dataset versioning & reproducibility

Records immutable dataset **revisions** (lakeFS commit id when `EXAMLOPS_LAKEFS_*` is set, otherwise a deterministic content hash), diffs them, verifies a working copy against a pin, validates against a data contract, prunes telemetry, and generates gated synthetic data (A1/A5/A7).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa data snapshot <dataset>` | **[mutation]** Resolves current dataset state to a revision and records it (spec R8). | Pin an exact, reproducible dataset revision before a training run. | `exa data snapshot FData --backend minio --path ./data/FData` |
| `exa data list <dataset>` | Lists recorded revisions newest-first, with the runs linked to each (spec R9). | Audit which revisions exist and what trained on them. | `exa data list FData --backend minio` |
| `exa data diff <dataset> <revA> <revB>` | Reports row-count / schema / size deltas between two revisions (spec R10). | Understand what changed between two dataset versions. | `exa data diff FData 3a9f 7c21` |
| `exa data checkout <dataset> <rev>` | Materialises / verifies that local data matches the pinned revision; exits non-zero on mismatch (spec R11). | CI/repro gate: fail the job if local data drifts from the pin. | `exa data checkout FData 3a9f --path ./data/FData` |
| `exa data validate <dataset>` | Validates a dataset against its data contract; exits non-zero on error violations (spec R11). | Pre-train quality gate on schema/constraints. | `exa data validate FData --path ./data/FData --revision 3a9f` |
| `exa data retention-prune` | **[mutation]** Prunes unbounded per-inference telemetry (drift / input snapshots) older than `--days`; never touches the audit chain or FinOps cost history. | Reclaim `platform.db` space without losing tamper-evident records. | `exa data retention-prune --days 90 --dry-run` then `--vacuum` |
| `exa data synth fit <dataset>` | Fits a synthetic generator to real data and reports what it learned (spec R1 smoke-check). | Sanity-check a generator before generating a full set. | `exa data synth fit FData --path ./data/FData --method gaussian_copula --seed 42` |
| `exa data synth generate <dataset>` | **[mutation]** Generates, gates (fidelity/privacy), and records a provenance-flagged synthetic dataset (spec R1–R4). | Produce a shareable synthetic replica of sensitive data. | `exa data synth generate FData --path ./data/FData --rows 5000 --method ctgan --min-fidelity 0.8 --min-privacy 0.9 --out ./data/FData-synth` |
| `exa data synth evaluate <dataset>` | Scores fidelity + privacy of an existing synthetic set and applies the release gate (spec R2/R3). | Independently re-check a synthetic set against release floors. | `exa data synth evaluate FData --real ./data/FData --synthetic ./data/FData-synth --min-fidelity 0.8 --min-privacy 0.9` |

### `exa feature` — serving feature store, zero train/serve skew (A3)

The single train+serve feature-view store: define a view once, feed it point-in-time offline observations, materialize them to the online store, and read online or as-of offline values from the same definition so training and serving can never disagree.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa feature apply <name>` | **[mutation]** Registers/patches a feature view — the single train+serve definition (R1). `--embedding FEATURE` names the feature holding an embedding, so materialize also indexes it for `exa feature similar`. | Declare an entity-keyed view of features from an offline source. | `exa feature apply user_stats --entity user --features cpu_p95,mem_p95 --source ./data/FData --ttl 3600 --revision 3a9f` |
| `exa feature list` | Lists all registered feature views. | See which views exist and their entities/TTLs. | `exa feature list` |
| `exa feature ingest <view>` | **[mutation]** Records an offline feature observation (point-in-time source of truth). | Land a timestamped feature value for an entity. | `exa feature ingest user_stats --entity-id node-01 --event-ts "2026-07-30 09:00:00" --values '{"cpu_p95": 0.82}'` |
| `exa feature materialize <view>` | **[mutation]** Materializes latest offline values → online store over a window (R6); for a view with an embedding feature, also indexes each entity's embedding into `features.<view>` and reports indexed/skipped rows. | Refresh the online store so serving reads current features. | `exa feature materialize user_stats --start "2026-07-01 00:00:00" --end "2026-07-30 00:00:00"` |
| `exa feature similar <view>` | The `-k` entities whose materialized embedding is nearest (cosine) to `--entity-id`'s, the entity itself excluded (ADR 0020 clause 4). | Find jobs, nodes or users that look like this one. | `exa feature similar job_features --entity-id job-42 -k 5` |
| `exa feature get <view>` | Reads an entity's feature vector — online (default) or point-in-time offline with `--asof`. | Fetch the exact vector serving/training would use. | `exa feature get user_stats --entity-id node-01 --asof "2026-07-30 09:00:00"` |
| `exa feature skew <view>` | Asserts online == offline as-of for an entity; skew must be zero (R2/GWT-1). | Guard rail proving no train/serve skew for a view. | `exa feature skew user_stats --entity-id node-01 --asof "2026-07-30 09:00:00"` |
| `exa feature freshness <view>` | Shows materialization age and staleness vs the view TTL (R6/GWT-4). | Detect a stale online store before it degrades serving. | `exa feature freshness user_stats` |

### `exa features` — versioned training-feature store

A lightweight versioned artifact store for engineered feature **files** attached to a model — push a new version, list versions, pull one back out. (Distinct from `exa feature` above.)

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa features push <model> <file_path>` | **[mutation]** Pushes a feature file into the versioned store under a feature-set name. | Version an engineered feature file for a model. | `exa features push JPCP ./features/jpcp_v3.parquet --name jpcp_features` |
| `exa features list` | Lists feature versions in the store. | See available feature-set versions. | `exa features list` |
| `exa features pull <model>` | Pulls a feature file from the store (latest or a specific version), optionally copying to a path. | Retrieve a pinned feature file for a training run. | `exa features pull JPCP --name jpcp_features --version 3 --output ./features/jpcp_v3.parquet` |

### `exa assets` — asset-centric freshness pipelines (A4)

Declare datasets/features/models as assets with upstream dependencies, then let ExaMLOps track which are stale and rebuild only what changed — a freshness DAG that coincides with the A2 lineage graph.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa assets declare <name>` | **[mutation]** Declares an asset and its upstream dependencies (R1). | Register a node in the freshness DAG. | `exa assets declare jpcp_model --kind model --deps FData,user_stats --description "JPCP trained model"` |
| `exa assets list` | Lists declared assets with their current version. | Inventory the asset graph. | `exa assets list` |
| `exa assets status` | Shows the freshness graph — fresh/stale and why (R5/GWT-2). | See what needs rebuilding before a release. | `exa assets status jpcp_model` |
| `exa assets materialize <name>` | **[mutation]** Rebuilds the asset plus only its stale ancestors (R4/GWT-3). | Incrementally rebuild without redoing fresh work. | `exa assets materialize jpcp_model --force` |
| `exa assets source-changed <name>` | **[mutation]** Advances a source asset's version so downstream assets go stale (GWT-2). | Signal new upstream data to invalidate dependents. | `exa assets source-changed FData` |
| `exa assets graph` | Prints the asset DAG (coincides with the A2 lineage graph) (R6/GWT-4). | Visualise dependencies end-to-end. | `exa assets graph` |

### `exa cards` — Croissant dataset cards + structured model cards

Generates standards-based documentation from live platform data: Croissant JSON-LD dataset cards and structured model cards, plus a completeness score that feeds the D5/C3 promotion gate.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa cards dataset <dataset>` | Emits + validates a Croissant JSON-LD dataset card (R1/R2); `--out` writes the JSON. | Publish a machine-readable, standards-compliant dataset card. | `exa cards dataset FData --revision 3a9f --license CC-BY-4.0 --out fdata.croissant.json` |
| `exa cards model <model>` | Builds a structured model card from live data, marking gaps as "not provided" (R3/R4); `--save` persists a version, `--out` writes Markdown. | Auto-generate an always-current model card. | `exa cards model jpcp --tenant seanergy --out jpcp-card.md --save` |
| `exa cards export <subject>` | **[mutation]** Export a card for publication with internal fields dropped, PII and site-specific locations redacted, and a detected secret **blocking** the export (audited); `--dataset`, `--out <file>`, `--force`, `--tenant`. | Publish a model or dataset card outside the deployment without leaking tenant identity, personal data or infrastructure detail. | `exa cards export jpcp --out jpcp-card.json` |
| `exa cards completeness <model>` | Scores model-card completeness (0..1) — the D5/C3 promotion gate signal (R6); `--require` exits 1 below a threshold. | CI gate: block promotion of under-documented models. | `exa cards completeness jpcp --tenant seanergy --require 0.8` |

## Models & Registry

Commands for the MLflow model registry (versions, aliases, lineage, cost, and supply-chain packaging), the read-only ModelZoo freshness feed, and the embedding-encoder lifecycle.

**Model-name casing:** the platform `MODEL_REGISTRY` uses uppercase names (`JPCP`); the MLflow registry stores them lowercase (`jpcp`). Registry-read commands (`list`, `info`, `diff`, `lineage`, `card`) accept the MLflow lowercase form (`jpcp`); cost, signing, packaging, and rollback commands use the uppercase form (`JPCP`). Both are shown as they appear in each command's own `--help` examples. Add `--json` (or the global `-o json|yaml|csv`) to any read command for machine-readable output.

### `exa models` — MLflow model registry

Inspect registered models and their versions, compare and trace them, roll aliases back, and produce supply-chain artifacts (signatures, AI-BOM, quantized versions).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa models list` | Lists every registered model with its Production alias and latest version. | Quick registry overview. | `exa models list` |
| `exa models info <model>` | Shows one model's full detail: all versions, aliases, and metrics. | Inspect a specific model before promoting or serving. | `exa models info jpcp` |
| `exa models diff <model> <v1> <v2>` | Compares metrics and params between two versions of a model. | Decide whether a newer version is actually better. | `exa models diff jpcp 17 18` |
| `exa models lineage [model] [version]` | Shows the pipeline → dataset → model-version lineage chain; `--graph` renders the upstream+downstream provenance graph (A2), `--impact <rev>` lists versions derived from a dataset revision. | Trace reproducibility / audit provenance. | `exa models lineage jpcp --graph` |
| `exa models card generate <model>` | Generates a standardised model-card document (stdout, or `-o/--output <file>`). | Produce release / governance documentation for a model. | `exa models card generate JPCP -o ./cards/jpcp.md` |
| `exa models card history [model]` | Shows model-card generation history (optionally filtered by model). | Track which cards were generated and when. | `exa models card history JPCP` |
| `exa models cost <model>` | Shows HPC GPU-hour cost history for a model. **`--record` is a mutation** — it fetches latest scheduler (Slurm/Flux) data, writes to the DB, and tags the MLflow run. | Review training cost; ingest fresh accounting data. | `exa models cost JPCP` (read) · `exa models cost JPCP --record` (mutation) |
| `exa models cost-list` | Shows an HPC cost summary across all models. | Compare per-model training spend fleet-wide. | `exa models cost-list` |
| `exa models sign <model> <version> --path <artifact>` | **Mutation.** Signs a model artifact bundle (HMAC fallback or Sigstore keyless) and records the signature. | Establish artifact provenance before promotion. | `exa models sign JPCP 17 --path ./artifacts/jpcp` |
| `exa models verify <model> <version> --path <artifact>` | Verifies a model's signature against the current artifact bytes (verify-before-load gate). `--mode enforce` (default, exit 1 on failure) or `warn` (record only). | CI gate ensuring a served artifact matches its signature. | `exa models verify JPCP 17 --path ./artifacts/jpcp --mode warn` |
| `exa models bom <model> <version>` | Generates a CycloneDX AI-BOM for a model version; `--dataset`, `--dataset-revision`, `--framework` enrich it; `--output <file>` writes JSON. | Supply-chain / SBOM compliance reporting. | `exa models bom JPCP 17 --dataset FData --dataset-revision abc123` |
| `exa models quantize <model> <version>` | **Mutation.** Quantizes a base version (`--method awq\|gptq\|fp8\|int8`, default `awq`) and registers a new signed + BOM'd version; `--path` supplies artifacts to sign, `--dataset`/`--dataset-revision` feed the BOM. | Ship a smaller/faster variant with provenance intact. | `exa models quantize JPCP 17 --method awq --path ./artifacts/jpcp` |
| `exa models parity <model> <target-version>` | Portability gate: compare a quantized version against its base on the model's declared tolerance. `--tolerance`. | Prove a requantisation did not change the numerics before promoting it. | `exa models parity JPCP 17-awq` |
| `exa models engine list` | Lists available inference engines. | Discover which serving engines a model YAML may target. | `exa models engine list` |
| `exa models engine validate <yaml_path>` | Validates a per-model YAML's `engine:` block (same check the CI integrity guard runs). | Pre-commit / CI gate on engine config. | `exa models engine validate ./usecases/seanergy/models/jpcp.yaml` |
| `exa models rollback run <model>` | **Mutation.** Rolls a model alias (default `Production`) back to a version: `-v/--version`, `-a/--alias`, `-r/--reason`, `-n/--dry-run` to preview. Prompts for confirmation unless `--yes`. | Emergency revert to a known-good version. | `exa models rollback run JPCP --version 5 --dry-run` (preview) · `exa --yes models rollback run JPCP --version 5` |
| `exa models rollback history <model>` | Shows rollback history for a model (last 20 events). | Audit past alias reassignments. | `exa models rollback history JPCP` |

### `exa modelzoo` — ModelZoo repository freshness and events

Tracks whether registered models are up to date with the upstream ModelZoo library, and manages the Control Plane poller integration.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa modelzoo status` | Shows ModelZoo freshness for every registered model. | See which models lag behind upstream. | `exa modelzoo status` |
| `exa modelzoo events [-n <limit>]` | Shows recent ModelZoo push events (`--limit/-n`, default 10). | Review the recent upstream change feed. | `exa modelzoo events --limit 20` |
| `exa modelzoo sync` | Manually triggers one ModelZoo poll cycle. | Force an immediate freshness refresh instead of waiting for the poller. | `exa modelzoo sync` |
| `exa modelzoo config` | Shows the ModelZoo integration configuration. | Inspect current auto-retrain / poll settings. | `exa modelzoo config` |
| `exa modelzoo config-set <key> <value>` | **Mutation.** Updates ModelZoo integration config on the Control Plane. Keys: `auto_retrain`, `poll_interval_seconds`. | Enable auto-retrain or change the poll cadence. | `exa modelzoo config-set poll_interval_seconds 120` |
| `exa modelzoo adopt [<model>] [--all] [--connection-name <n>] [--no-connection] [--dry-run]` | **Mutation.** Provisions one project per model — project · storage · **bound MinIO connection** · budget · workbench · pipeline surfaces. Idempotent. `--all` backfills every Zoo/pack model; `--no-connection` skips the MinIO wiring; `--connection-name` renames the per-project connection (default `minio`). | Make "a project per model" the zero-effort default, with each project's own MinIO storage. | `exa modelzoo adopt JPCP` · `exa modelzoo adopt --all --dry-run` |

### `exa embedding` — Embedding lifecycle (encoders + blue-green reindex, B6)

Registers versioned embedding encoders and performs verified blue-green reindexing of a collection to a new encoder (old index retained, then pruned).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa embedding register <name> <version> --dim <n>` | **Mutation.** Registers a versioned encoder and returns its `encoder_id`. `--dim` (required), `--metric cosine\|dot\|l2` (default cosine), `--norm l2\|none` (default l2). | Onboard a new embedding model into the registry. | `exa embedding register nomic-embed-text v1.5 --dim 768` |
| `exa embedding list` | Lists registered encoders. | Find an `encoder_id` to set or reindex to. | `exa embedding list` |
| `exa embedding set-encoder <collection> <encoder_id>` | **Mutation.** Bootstraps a collection's active encoder (R2); `--tenant` scopes it (default `default`). | Initialize the encoder for a brand-new collection. | `exa embedding set-encoder docs <encoder-id>` |
| `exa embedding reindex <collection> <new_encoder_id>` | **Mutation.** Blue-green reindex to a new encoder — verified switch, old index retained then pruned (R4/R5). `--corpus-size`, `--recall`, `--recall-floor` (default 0.9, switch aborts below it), `--tenant`. | Migrate a collection to an upgraded encoder without downtime, gated on measured recall. | `exa embedding reindex docs <new-encoder-id> --corpus-size 10000 --recall 0.97` |
| `exa embedding status <collection>` | Shows a collection's active/staging encoder and reindex history; `--tenant` scopes it. | Verify which encoder is live and review reindex history. | `exa embedding status docs` |

## Serving & Inference

The largest panel in the CLI: everything that puts a trained model behind a live endpoint and keeps it healthy. It spans Ray Serve operations (hot-loading, traffic splitting, shadow/challenger deployments, A/B testing, autoscaling, LoRA adapters, cache-aware routing, batch and explainability), one-shot `predict`, production deploy/verify, the LLM `gateway` (virtual keys, semantic cache, reasoning accounting, structured output), the `vector` store, and `rag` knowledge bases with citations.

Model names are uppercase in the platform registry (`JPCP`) and lowercase in MLflow (`jpcp`); aliases are `Production`, `Canary`, `Staging`. Vector/RAG examples use collection `demo` (dim 384, cosine) and knowledge base `kb`.

> Mutating or outward-facing commands (`serve reload`, `serve traffic`, `gateway key issue`, `rag ingest`, `vector create`, `production deploy --execute`, …) are shown with safe example values and marked **mutation** — do not run them against a live stack without intent.

### `exa serve` — Ray Serve operations

Manage the multi-model Ray Serve deployment: what is hot-loaded, how traffic is split across MLflow aliases, and the progressive-delivery / experimentation machinery layered on top.

#### Core lifecycle & inspection

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve models` | Lists models currently hot-loaded in Ray Serve (`-d/--detail` for full detail). | Confirm which versions are actually serving right now. | `exa serve models --detail` |
| `exa serve reload` | Hot-reloads Production models from MLflow into Ray Serve; `-m/--model` limits to one. | Push a freshly promoted version into the live hot set. **mutation** | `exa serve reload --model JPCP` |
| `exa serve check` | Smoke test: health check + one prediction per model. | Post-deploy sanity check that every model answers. | `exa serve check` |
| `exa serve infer-check` | Smoke-tests the inference pipeline end-to-end with a valid synthetic HPC job. | Verify `Ingress → FeatureTransformer → ModelRouter` wiring. | `exa serve infer-check` |
| `exa serve benchmark` | Benchmarks Ray Serve with the dummy client and reports latency stats (`-n/--requests`). | Quick latency baseline / regression spot-check. | `exa serve benchmark -n 200` |
| `exa serve backend` | Shows the active serving backend (`ray-compose` default or `kserve-k8s`). | Confirm which serving substrate is in effect (E1 seam). | `exa serve backend` |
| `exa serve manifest MODEL` | Generates a schema-valid KServe `InferenceService` manifest from the registry (E1). | Deploy a model to Kubernetes/KServe instead of Ray. | `exa serve manifest JPCP --alias Production --canary 10 --out jpcp.yaml` |

#### LLM & VLM endpoints (`exa serve llm`)

Lifecycle for vLLM-served large language and **vision-language** models, over four substrates — `external` (default; register a server someone else runs), `compose` (GPU service), `slurm`/`flux` (HPC allocation), `kserve` (Kubernetes). See the [VLM serving guide](../guides/vlm-serving.md) and ADR 0107.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve llm start MODEL` | Starts (or registers) a vLLM endpoint and records it in the endpoint registry; `--dry-run` previews, confirms, audited. | Bring a VLM online on HPC, or point the platform at an existing endpoint. **mutation** | `exa serve llm start qwen-vl --launcher slurm --nodes 2 --gpus 4 --tp 4 --modality vision --max-images 2` |
| `exa serve llm list` | Lists registered endpoints (`--project`, `--state`). | See what LLM/VLM capacity is live and who owns it. | `exa serve llm list --project research` |
| `exa serve llm status MODEL` | Registry record + substrate status + live `vllm:*` metrics (queue depth, KV-cache usage). | Diagnose a slow or saturated endpoint. | `exa serve llm status qwen-vl` |
| `exa serve llm health MODEL` | Probes `/health` and reconciles the recorded state; **exits 1** when not ready. | Deploy/CI gate before routing traffic to a new endpoint. | `exa serve llm health qwen-vl` |
| `exa serve llm args MODEL` | Prints the exact `vllm serve` argv the model's `engine:` block renders. | Verify HPC and Kubernetes will run identical flags (seam parity). | `exa serve llm args qwen-vl` |
| `exa serve llm chat MODEL` | Sends a chat request; `--image` (repeatable) attaches pictures, `--stream` shows token deltas. | The VLM smoke test — ask a model about a chart or scan. | `exa serve llm chat qwen-vl -m "What does this chart show?" --image ./gpu-util.png` |
| `exa serve llm bench MODEL` | Sequential requests reporting TTFT p50 and output tokens/s. | Quick latency/throughput baseline for an endpoint. | `exa serve llm bench qwen-vl -n 20` |
| `exa serve llm stop MODEL` | Stops the endpoint and marks it `STOPPED`; `--dry-run` previews, confirms, audited. | Release a GPU allocation when a model is no longer needed. **mutation** | `exa serve llm stop qwen-vl` |

#### Traffic & progressive delivery

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve traffic MODEL` | Shows or sets the traffic split across aliases (must sum to 100); `--dry-run` previews, `--reason` is audited. | Canary rollout: shift a slice of traffic to a new version. **mutation** | `exa serve traffic JPCP --production 90 --canary 10 --reason "canary v18"` |
| `exa serve traffic-list` | Shows the traffic split for all models; `-w/--watch` live-refreshes (`--interval`). | Fleet-wide view of how traffic is routed. | `exa serve traffic-list --watch --interval 5` |

#### Shadow deployments (`exa serve shadow`)

Mirror live traffic to a second alias without serving its responses — compare a candidate against production risk-free.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve shadow enable MODEL` | Enables shadow deployment, mirroring traffic to `-a/--shadow-alias`. | Test a candidate on real traffic without affecting users. **mutation** | `exa serve shadow enable JPCP --shadow-alias Staging` |
| `exa serve shadow status` | Shows shadow deployment configuration (model optional). | Check whether shadowing is active and where it mirrors. | `exa serve shadow status JPCP` |
| `exa serve shadow log MODEL` | Shows the last 20 shadow inference comparison results. | Compare shadow vs production predictions before promoting. | `exa serve shadow log JPCP` |
| `exa serve shadow disable MODEL` | Disables shadow deployment for a model. | Stop mirroring once the comparison is done. **mutation** | `exa serve shadow disable JPCP` |

#### A/B testing (`exa serve ab`)

Split live traffic between two variants and decide a winner with a Welch's t-test.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve ab start MODEL` | Starts an A/B test comparing two variant aliases (`-a/-b`, `-s/--split`, `-n/--name`). | Compare Production vs Canary head-to-head on live traffic. **mutation** | `exa serve ab start JPCP -a Production -b Canary -s 50 -n "v18-eval"` |
| `exa serve ab record MODEL VARIANT VALUE` | Records a metric observation for the active A/B test. | Feed measured outcomes (e.g. RMSE) into the experiment. **mutation** | `exa serve ab record JPCP Canary 4.7` |
| `exa serve ab analyze MODEL` | Runs Welch's t-test on recorded observations and names a winner (`--lower-is-better`, `--alpha`, `--min-sample`). | Decide, with significance, which variant wins. | `exa serve ab analyze JPCP --lower-is-better --alpha 0.05` |
| `exa serve ab status` | Shows A/B tests (most recent 20); model optional. | Review running and past experiments. | `exa serve ab status JPCP` |
| `exa serve ab stop MODEL` | Stops the running A/B test for a model. | End an experiment once a winner is decided. **mutation** | `exa serve ab stop JPCP` |

#### Champion–challenger (`exa serve challenger`)

Run a challenger version against the champion with a declared promotion policy and SLO guard.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve challenger enable MODEL` | Enables a challenger and declares its promotion policy (`--version`, `--mirror`, `--min-delta`, `--alpha`, `--min-samples`, `--auto-promote`, `--tenant`). | Trial a new version with statistically-gated auto-promotion. **mutation** | `exa serve challenger enable JPCP --version 18 --mirror 20 --min-delta 0.5 --auto-promote` |
| `exa serve challenger status MODEL` | Shows the scoreboard: delta, p-value, N, SLO (`--tenant`). | Watch whether the challenger is beating the champion. | `exa serve challenger status JPCP` |
| `exa serve challenger list` | Lists configured challengers (`--tenant` filter). | Inventory of active challenger experiments. | `exa serve challenger list` |
| `exa serve challenger judge <model>` | **[mutation]** Score **unlabelled** challenger samples with a C2 judge (ADR 0024 clause 2); `--judge-model`, `--limit`, `--tenant`. Scores go in their own columns, never into `label`. A scoreboard resting on an uncalibrated judge never reports `policy_met` (ADR 0111). | Compare a challenger where ground truth never arrives. | `exa serve challenger judge JPCP --judge-model gpt-4o` |
| `exa serve challenger promote MODEL` | Proposes promotion via the C3 gate if the policy is met and there's no SLO regression (`--tenant`). | Promote the challenger once it has won cleanly. **mutation** | `exa serve challenger promote JPCP` |
| `exa serve challenger disable MODEL` | Disables the challenger for a model. | Abandon a challenger that isn't winning. **mutation** | `exa serve challenger disable JPCP` |

#### Autoscaling & scale-to-zero (`exa serve autoscale`)

Declare per-model replica policies, simulate decisions, and account the FinOps savings of scaling to zero.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve autoscale set MODEL` | Declares a per-model autoscale policy (`--min`/`--max`, `--metric`, `--target`, `--scale-to-zero-after`, `--warm-pool`, `--gpu-fraction`, `--tenant`). | Right-size replicas; enable scale-to-zero for idle models. **mutation** | `exa serve autoscale set JPCP --min 0 --max 4 --metric p95 --target 200 --scale-to-zero-after 300` |
| `exa serve autoscale status MODEL` | Shows the policy, recent scale events, and cold-start time. | Review scaling behaviour and cold-start impact. | `exa serve autoscale status JPCP` |
| `exa serve autoscale simulate MODEL` | Computes the scaling decision for a given state — pure, anti-thrash aware (`--replicas`, `--observed`, `--idle`, `--since-last`). | Dry-run a scaling decision before trusting the policy. | `exa serve autoscale simulate JPCP --replicas 2 --observed 350 --idle 0 --since-last 120` |
| `exa serve autoscale record MODEL FROM_REPLICAS` | Records an executed scale event (audited); `--reason`, `--cold-start`, `--tenant`. | Log a real scale action for later analysis. **mutation** | `exa serve autoscale record JPCP 2 --reason "p95 breach" --cold-start 8.3` |
| `exa serve autoscale savings MODEL` | Estimates FinOps savings from scale-to-zero (`--gpu-cost` per hour). | Quantify the cost saved by idling replicas. | `exa serve autoscale savings JPCP --gpu-cost 2.5` |

#### Multi-LoRA adapters (`exa serve adapter`)

Register, list, promote, and route through LoRA/QLoRA adapters over a base model (B7).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve adapter add BASE` | Registers an adapter (alias of `exa finetune`); `--dataset`, `--method` (lora/qlora/full), `--rank`, `--eval`, `--eval-floor`. | Fine-tune and register a task-specific adapter. **mutation** | `exa serve adapter add jpcp --dataset rev123 --method lora --rank 8 --eval 0.91` |
| `exa serve adapter list` | Lists registered adapters (`--base` filter). | See which adapters exist for a base model. | `exa serve adapter list --base jpcp` |
| `exa serve adapter promote ADAPTER_ID` | Promotes an adapter — blocked by the C3 eval-gate if below the quality floor. | Ship an adapter only if it clears the eval floor. **mutation** | `exa serve adapter promote adp-42` |
| `exa serve adapter route BASE ADAPTER_ID` | Routes a request through base + adapter; refuses a base mismatch (`--prompt`, `--hot-set`). | Serve a prompt with a specific adapter over its base. | `exa serve adapter route jpcp adp-42 --prompt "summarize" --hot-set 4` |

#### Cache-aware routing (`exa serve routing`)

KV/prefix-cache-aware inference routing — opt-in over the default round-robin (E4).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve routing set MODEL` | Configures routing (`--mode` round_robin/cache_aware, `--slo-latency-ms`, `--disaggregate`, `--prefill-pool`, `--decode-pool`, `--tenant`). | Turn on prefix-cache-aware routing for a hot-prefix workload. **mutation** | `exa serve routing set JPCP --mode cache_aware --slo-latency-ms 200` |
| `exa serve routing simulate MODEL` | Simulates a shared-prefix stream and reports cache-aware vs round-robin hit rate (`--replicas`, `--shared-prefix-requests`, `--mode`). | Estimate the cache benefit before enabling it. | `exa serve routing simulate JPCP --replicas 4 --shared-prefix-requests 1000 --mode cache_aware` |
| `exa serve routing stats MODEL` | Shows recorded prefix-cache hit rate + routing-decision breakdown (`--tenant`). | Measure how well cache-aware routing is performing. | `exa serve routing stats JPCP` |

#### Batch inference (`exa serve batch`)

Run offline/batch predictions from a file.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve batch submit MODEL INPUT_FILE` | Runs synchronous batch inference from a JSON/JSONL file (`-a/--alias`, `-o/--output`). | Score a large offline dataset in one call. | `exa serve batch submit JPCP inputs.jsonl --alias Production -o preds.json` |
| `exa serve batch list` | Lists recent batch inference jobs (`-m/--model` filter). | Review batch job history and status. | `exa serve batch list --model JPCP` |

#### Explainability (`exa serve explain`)

Feature-importance explanations (XAI) from the serve endpoint.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa serve explain explain MODEL` | Requests feature-importance scores from the explain endpoint (`-i/--input-json`, `-n/--top-n`, `-a/--alias`). | Explain a single prediction's drivers. | `exa serve explain explain JPCP -i '{"mbwidth": 12.5}' -n 5 --alias Production` |
| `exa serve explain history MODEL` | Shows recent explain requests for a model. | Audit which inputs were explained and when. | `exa serve explain history JPCP` |

### `exa predict` — one-shot inference

Send a single inference request through the Ray Serve inference pipeline.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa predict MODEL` | Sends one inference request; `-f/--features` (JSON dict), `--alias`, or explicit `--version`. | Ad-hoc prediction / manual endpoint check. | `exa predict JPCP -f '{"mbwidth": 12.5}' --alias Production` |

### `exa production` — production deployment & verification

Plan, execute, and verify production deploys of models.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa production deploy [ACTION] [DEPLOY_ID]` | Plans/executes deploys or inspects history/status; `--execute` (default is a side-effect-free dry run), `--models` (stale/all/IDs), `--dataset`, `-e/--env`, `--registry`, `--no-schedule`, plus history filters (`--limit`, `--status`, `--model`, `--operation`). | Roll out stale models, or review deploy history. Default is a safe plan. **mutation with `--execute`** | `exa production deploy --models stale` &nbsp;·&nbsp; `exa production deploy --models JPCP,MACK --dataset PM100Dataset --execute` |
| `exa production verify` | Verifies production service health without changing state. The SeanerBUS check follows `SEANERBUS_BRIDGE_STATUS_URL` / the `seanerbus_bridge` config key. | Confirm production is healthy after a deploy. | `exa production verify` |

### `exa gateway` — model gateway (virtual keys, routing, cost)

An OpenAI-compatible gateway in front of models: virtual keys with budgets, a semantic cache, reasoning-token accounting, and schema-constrained structured output.

#### Chat & semantic cache

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa gateway chat [MODEL]` | Sends one chat message through the gateway (default echo route); `--message`, `--key`, `--cache`. | Smoke-test the gateway or a virtual key end-to-end. | `exa gateway chat --message "hello" --key vk_abc --cache` |
| `exa gateway cache stats` | Shows semantic-cache hit-rate and token/cost savings (`--tenant`). | Quantify how much the semantic cache is saving. | `exa gateway cache stats --tenant acme` |

#### Virtual keys (`exa gateway key`)

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa gateway key issue` | Issues a virtual key (printed once — only its hash is stored); `--tenant`, `--project`, `--model` (repeatable allow-list), `--budget` USD. | Grant scoped, budgeted API access to a tenant/project. **mutation** | `exa gateway key issue --tenant acme --project research --model JPCP --budget 100` |
| `exa gateway key list` | Lists virtual keys (hashes only). | Inventory issued keys and their scopes. | `exa gateway key list` |
| `exa gateway key revoke KEY_HASH` | Revokes a virtual key by its stored hash. | Cut off a compromised or expired key. **mutation** | `exa gateway key revoke a1b2c3d4` |

#### Reasoning ops (`exa gateway reasoning`)

Budget, account, and inspect reasoning (thinking) vs output tokens separately (B8).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa gateway reasoning budget REQUESTED` | Shows how a reasoning budget caps a request (`--max`). | Preview the effect of a thinking-token cap. | `exa gateway reasoning budget 8000 --max 4000` |
| `exa gateway reasoning account MODEL` | Accounts reasoning vs output tokens/cost separately (`--reasoning`, `--output`, `--reasoning-rate`, `--output-rate`, `--tenant`). | Attribute cost to thinking vs answer tokens. **mutation** | `exa gateway reasoning account JPCP --reasoning 3000 --output 500 --reasoning-rate 0.000003` |
| `exa gateway reasoning stats` | Shows the reasoning-vs-output token/cost split + structured-output outcomes (`--model`, `--tenant`). | Analyze reasoning-token spend across the fleet. | `exa gateway reasoning stats --tenant acme` |

#### Structured output (`exa gateway schema`)

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa gateway schema test SCHEMA_FILE OBJECT_FILE` | Validates (and optionally `--repair`s) an object against a JSON Schema. | Verify/repair model output against a contract. | `exa gateway schema test schema.json out.json --repair` |

### `exa vector` — vector store (collections, upsert, dense/sparse/hybrid search, reindex, drop)

A tenant-namespaced vector store with fixed-dim collections and a distance metric.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa vector create COLLECTION` | Creates a collection with a fixed `--dim`, `--metric` (cosine/l2/dot) and ANN `--index` (flat/hnsw/ivfflat with `--m`, `--ef-construction`, `--ef-search`, `--lists`, `--probes`, validated against pgvector's limits); `--encoder` stamps the encoder; `--tenant`. Re-declaring a non-empty collection with another dim/metric/encoder is refused. | Stand up a new embedding collection. **mutation** | `exa vector create demo --dim 384 --metric cosine --index hnsw --m 16` |
| `exa vector upsert COLLECTION` | Upserts one vector (rejected on dim mismatch, NaN or foreign encoder); `--id`, `--vector` (JSON floats), `--meta`, `--text` (indexed by the BM25 channel), `--encoder`, `--tenant`. | Add or update a single embedding. **mutation** | `exa vector upsert demo --id doc1 --vector '[0.1, 0.2, ...]' --text 'JPCP job 4711 failed'` |
| `exa vector search COLLECTION` | Top-k search in `--mode dense` (metric, `--vector`), `sparse` (BM25, `--text`) or `hybrid` (both, fused by `--fusion rrf` or `convex` with `--alpha`); `-k/--k`, `--filter` (metadata equality), `--candidates`, `--encoder`, `--tenant`. Hybrid rows show each hit's dense and sparse rank. | Retrieve nearest neighbours; use hybrid when queries name exact ids or codes. | `exa vector search demo --vector '[0.1, ...]' --text 'job 4711' --mode hybrid -k 5` |
| `exa vector stats COLLECTION` | Shows dim, metric, index configuration, how search is actually answered (`exact`, `ann`, or declared-but-not-built), item count and encoder (`--tenant`). | Inspect a collection's shape, size and index. | `exa vector stats demo` |
| `exa vector reindex COLLECTION` | Rebuilds the ANN index blue-green (on pgvector `CREATE INDEX CONCURRENTLY`, search stays up); with `--index` and its parameters, switches the collection to that index. Builds IVFFlat after loading. `--tenant`; invoked by B6. | Rebuild or retune the index after bulk loads. **mutation** | `exa vector reindex demo --index ivfflat --lists 200 --probes 14` |
| `exa vector drop COLLECTION` | Deletes a collection and every vector in it after a confirmation (`--yes` skips); audited as `vector_collection_dropped`. On pgvector it drops the collection's table. `--tenant`. | Tenant erasure, or removing a retired corpus. **destructive** | `exa --yes vector drop demo` |

### `exa rag` — knowledge bases with citations

Ingest documents into a knowledge base and answer questions with retrieved-chunk citations.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa rag ingest KB` | Chunks, embeds, and indexes documents into a KB; `--docs` (JSONL of `{id, text}`), `--tenant`, `--source-revision` (A1). | Build/refresh a knowledge base from a corpus. **mutation** | `exa rag ingest kb --docs corpus.jsonl --source-revision rev123` |
| `exa rag query KB` | Answers a question from a KB, citing retrieved chunks; `--question`, `-k/--k`, `--retrieval dense\|hybrid` (hybrid adds BM25 so exact ids and codes are found), `--fusion`, `--tenant`. | Ask a grounded question and get cited answers. | `exa rag query kb --question "Why did JPCP-4711 fail?" --retrieval hybrid` |
| `exa rag list` | Lists knowledge bases and their versions (`--tenant` filter). | See which KBs exist and their revisions. | `exa rag list` |

## GenAI & LLMOps

Commands for running LLM and agent workloads on ExaMLOps: GenAI telemetry and token-cost estimation, a versioned prompt registry with dev/prod labels, guardrails that defend against prompt injection / PII / toxicity / unsafe tool calls, and AgentOps analytics over agent traces and tool calls. All state lives in the shared `platform.db`; every mutating command is audited.

### `exa genai` — GenAI observability (OpenTelemetry semconv) + token cost

Inspects the GenAI telemetry surface (OpenTelemetry GenAI semantic conventions) and estimates the USD cost of a call from its token usage.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa genai check` | Shows GenAI telemetry status: tracing on/off, content capture, and semconv version. | Confirm GenAI spans/content-capture are wired before debugging an LLM call. | `exa genai check` |
| `exa genai cost` | Estimates the USD cost of a GenAI call from its input/output token counts (spec R7). Requires `--model/-m`, `--in`, `--out`. | Price a prompt+completion before rolling it to prod or compare model economics. | `exa genai cost --model gpt-4o --in 1000 --out 500` |

### `exa prompt` — Prompt registry (versioned templates + labels)

An immutable, versioned prompt store. Each `create` appends a new version; movable labels (e.g. `dev`, `prod`) point at a chosen version, and label moves/rollbacks are audited without deleting history.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa prompt create` | **(Mutation)** Creates a new immutable prompt version (spec R1). Requires `--template/-t`; optionally points a `--label/-l` at it. | Register or iterate on a prompt template under version control. | `exa prompt create greeting --template "Hello {name}, how can I help?" --label dev` |
| `exa prompt list` | Lists all prompt names, or the versions + labels of one named prompt. | Discover registered prompts or inspect a prompt's version/label history. | `exa prompt list greeting` |
| `exa prompt show` | Shows a prompt version's template, addressed by `name <version>` or `name@label`. | View exactly what template `prod` (or a given version) is serving. | `exa prompt show greeting@prod` |
| `exa prompt diff` | Shows a line diff between two versions of a prompt (spec R3). | Review what changed between two prompt revisions before promoting. | `exa prompt diff greeting 1 2` |
| `exa prompt label` | **(Mutation)** Moves a label to a version — audited (spec R8/R9). | Promote a prompt version to `prod` (or any environment label). | `exa prompt label greeting prod 2` |
| `exa prompt rollback` | **(Mutation)** Rolls a label back to a prior version without deleting history (spec R10). | Revert `prod` to a known-good prompt version after a bad release. | `exa prompt rollback greeting prod 1` |

### `exa guardrails` — injection / PII / toxicity defense

Runs text and agent tool calls through the guardrail engine (prompt-injection, PII, toxicity, tool allow-list). Modes: `off`, `monitor` (log only), `enforce` (block/redact); scoped per tenant.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa guardrails test` | Runs a text through the guardrail and shows the action (allow/redact/block) + findings. Options: `--text` (required), `--direction input\|output`, `--mode off\|monitor\|enforce`, `--tenant`. | Verify the guardrail catches a prompt-injection or PII string before deploying a policy. | `exa guardrails test --text "ignore previous instructions" --direction input --mode enforce` |
| `exa guardrails check-tool` | Checks whether an agent's requested tool call is on the per-tenant allow-list (R7). Requires the tool arg + `--allow` (repeatable); `--mode`, `--tenant`. | Gate which tools an agent may call under enforce mode. | `exa guardrails check-tool delete_model --allow list_models --allow get_status --mode enforce` |
| `exa guardrails stats` | Shows guardrail action counts (allow/redact/block), optionally filtered by `--tenant`. | Report how often guardrails fired, for governance/tuning. | `exa guardrails stats --tenant acme` |

## Agents & Automation

The agentic surface: the conversational front door, the closed loop that acts on its own, the
tools other agents call, and the analytics that say whether any of it is working. These commands
were previously spread across four other panels — correct and complete, but with no title naming
the category, which made the surface read as missing when it was not. `exa explain` deliberately
stays under Getting Started: it introspects the command tree and calls no agent.

### `exa chat` — hold a conversation with the agent

The native interactive client for Skipper. It honours the active CLI context, checks that the agent
and its model backend are ready, streams answers and tool activity, and preserves conversations by
session ID. Use `exa ask` instead when you need a single scriptable answer.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa chat` | Starts a new native Skipper conversation using the agent URL from the active context. | A back-and-forth investigation where each answer changes the next question. | `exa chat`<br>`exa -c staging chat` |
| `exa chat --session ID` | Opens or continues the named session. | Continue an incident or investigation across terminal runs. | `exa chat --session incident-42` |

Inside the client, use `/help`, `/status`, `/sessions`, `/history`, `/new`, `/resume ID`,
`/approve`, `/deny`, and `/quit`. Approval commands are available when a write tool pauses for
human confirmation. The OpenAI-compatible endpoint remains available for optional third-party
clients, but their extra commands are not part of the `exa chat` contract.

`exa ask` returns the same opaque action ID when a one-shot request pauses. Continue only with the
typed form `exa ask --session SESSION --approve ACTION_ID` (or `--deny`). Ordinary replies such as
`yes` never authorize a write, and action IDs expire and cannot be replayed.

### `exa agent` — is the agent up, and which brain is it using?

Interrogates the running Skipper agent over the same HTTP surface `exa ask` uses, reporting what
the **server** resolved — not what this machine's `.env` says, since the agent normally runs
elsewhere. Exits non-zero when the agent is unreachable *or* when it is up but its LLM backend is
unusable, so it works as a health gate: both states mean an answer cannot be trusted.

This exists because a rejected Azure key once went unnoticed for a week — the agent kept
answering, with empty strings, so it read as a weak model rather than a dead credential.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa agent status` | Reachability, LLM backend, model, and whether the long-term memory store actually attached. Prints the exact environment variable to repair when the backend is rejected. | First thing to run when the agent gives strange or empty answers — it separates "not running", "running with a dead key", and "running fine but with no long-term memory". | `exa agent status`<br>`exa --json agent status`<br>`exa -c lxp agent status` |
| `exa agent memory stats` | Count the authenticated principal's remote memories by kind (`proc`, `episode`, `pref`, `kb`). | Verify what the currently selected agent identity owns before review or erasure. | `exa agent memory stats`<br>`exa --json agent memory stats` |
| `exa agent memory list` | Enumerate one memory kind inside the server-verified principal and tenant namespace. | Review learned procedures or preferences without granting access to another caller's memory. | `exa agent memory list pref`<br>`exa agent memory list proc --limit 20` |
| `exa agent memory export` | Export the authenticated owner's memory as JSON, to stdout or a file. | Fulfil a subject-access request or take a copy before erasure. Protect the resulting file as personal data. | `exa agent memory export --out memories.json` |
| `exa agent memory delete` | Delete owned memories of one kind and write a `memory_erase` audit event under the verified principal. | The right-to-erasure control (ADR 0034). The retained audit record contains metadata/digests, not the deleted content. `--json` alone is not consent; add `--yes`. | `exa agent memory delete pref`<br>`exa --json --yes agent memory delete pref` |
| `exa agent memory review` | List, approve, or reject queued procedure memories owned by the authenticated principal and tenant. Approvals and rejections are audited. | Batch-govern proposed durable procedures without accessing another principal's queue. | `exa agent memory review list`<br>`exa agent memory review approve 42`<br>`exa agent memory review reject 43 --reason incomplete` |

Remote mode is the default and uses `AGENT_URL` plus the configured agent token. The server refuses
memory administration when no API credential is configured. Add `--local` to a specific memory
command only for offline migration or recovery against `AGENT_MEMORY_DB`; local mode does not
provide the remote principal boundary.

### `exa ask` — natural-language front door to Skipper

Routes a plain-English question to the Skipper agent's OpenAI-compatible bridge. Great when you don't know which exact command to run.

The answer **streams** at a terminal: tokens appear as the agent produces them, and each tool it calls is announced on its own dim line. That matters because the agent's tool loop runs before it writes anything, so without streaming a slow answer is silence followed by a wall of text — indistinguishable from a hang. Piped or `--json` output does not stream, because there the point is one parseable object; `--no-stream` forces that behaviour at a terminal too.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa ask "<question>"` | Sends a natural-language question to the Skipper agent and prints its answer as it arrives; `--session` keeps context across turns. | When you want an answer or an action described conversationally instead of hunting for the precise CLI command. | `exa ask "which models are drifting and why?"`<br>`exa ask "now retrain the worst one" --session mysession` |
| `exa ask "<question>" --no-stream` | Waits for the complete answer and prints it in one go. | Logging a transcript, or any context where interleaved output is awkward. | `exa ask "summarise last week" --no-stream` |

### `exa agentops` — agent trace & tool-call analytics

Analytics over recorded agent sessions: per-tool reliability, a session index, timeline replay, and anomaly detection (reasoning loops, step blowups, cost overruns). All reads; tenant-filterable.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa agentops tools` | Per-tool success rate, call count, and average latency (R2), optionally by `--tenant`. | Find which agent tools are slow or failing across sessions. | `exa agentops tools --tenant acme` |
| `exa agentops sessions` | Lists recent agent sessions with steps, cost, and status (R6 index). Options: `--tenant`, `--status ok\|anomaly\|error`, `--limit` (default 50). | Triage recent agent runs, e.g. only the ones that errored. | `exa agentops sessions --status anomaly --limit 20` |
| `exa agentops replay` | Reconstructs a session's tool-call timeline (R6, GWT-5). Requires the session id. | Step through exactly what an agent did in one session while debugging. | `exa agentops replay sess-42` |
| `exa agentops anomalies` | Detects reasoning loops, step blowups, and cost overruns in a session (R4, GWT-3/4). Requires the session id; `--cost-budget` USD threshold (default 1.0). | Flag a runaway or over-budget agent run for review. | `exa agentops anomalies sess-42 --cost-budget 2.0` |

### `exa autopilot` — self-driving MLOps closed loop

Runs the policy-governed loop that detects drift, retrains, checks metrics, and promotes — all
gated by a persistent kill-switch (disabled by default; `EXAMLOPS_AUTOPILOT_ENABLED` also
applies). See ADR 0085.

**`--dry-run` is not gated by the kill-switch.** A preview takes no lease, triggers no retrain and
promotes nothing, so `exa autopilot run --dry-run` works while the switch is off — you inspect the
loop *before* arming it, not after. The preview says the switch is off, and the run is recorded
with `enabled_state="disabled"` so history never implies the loop was live. A real
`exa autopilot run` is still refused until you `exa autopilot enable`.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa autopilot enable` | **[mutation]** Enable the autopilot kill-switch (persisted in `platform.db`). | Turn on hands-off closed-loop MLOps. | `exa autopilot enable` |
| `exa autopilot disable` | **[mutation]** Disable the autopilot kill-switch (persisted in `platform.db`). | Emergency stop for all autopilot activity. | `exa autopilot disable` |
| `exa autopilot run [model]` | **[mutation]** Run one cycle: drift scan → policy → retrain → metrics → policy → promote. `--dry-run`, optional `model` to restrict. | Manually drive (or preview) one autopilot pass. | `exa autopilot run --dry-run` |
| `exa autopilot status` | Show recent autopilot run history. `--last` (10). | Audit what the loop did and when. | `exa autopilot status --last 20` |
| `exa autopilot contract [behaviour]` | Print a behaviour's blast-radius contract verbatim — what it may/may not change, extent caps, its rollback and kill-switch (ADR 0113). | Read the autopilot's bounds instead of trusting reassurance; diff after an overlay change. | `exa autopilot contract drift_auto_retrain` |
| `exa autopilot autonomy <behaviour> <level>` | **[mutation]** Set one behaviour's autonomy: `AUTONOMOUS` (requires `--ack "<text>"`, recorded), `REVIEW`, or `DISABLED` — pausable without losing its configuration. | Pause just the promote loop while keeping retrains autonomous, or grant autonomy with a recorded acknowledgment. | `exa autopilot autonomy autopilot_promote REVIEW` |
| `exa autopilot interrupt <run_id>` | **[mutation]** Flag ONE in-flight cycle: `--freeze` pauses it at its next checkpoint, `--kill` aborts it (both audited). | Stop a live run you distrust without flipping the global kill-switch. | `exa autopilot interrupt 42 --kill --reason "wild retrain storm"` |
| `exa autopilot resume <run_id>` | **[mutation]** Release a frozen run so it continues from its checkpoint. | Let a held cycle finish after you've looked. | `exa autopilot resume 42` |
| `exa autopilot quarantine <model>` | **[mutation]** Exclude one model from all autonomous action until released (audited; shown in skip reasons). | Contain a misbehaving model while everything else stays automated. | `exa autopilot quarantine JPCP --reason "drift sensor suspect"` |
| `exa autopilot release <model>` | **[mutation]** Release a quarantined model back to autonomous eligibility. | End the containment once the cause is fixed. | `exa autopilot release JPCP` |

### `exa mcp` — MCP server + Agent-to-Agent (A2A) surface

Exposes ExaMLOps platform capabilities as agent-callable MCP tools/resources/prompts and an A2A Agent Card. Writes are off by default; enable mutating tools with `--allow-writes` or `EXAMLOPS_MCP_ALLOW_WRITES`.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa mcp tools` | Lists the tools exposed to agents over MCP; `--all` includes write tools even when writes are disabled. | Discover the agent-callable API surface | `exa mcp tools --all` |
| `exa mcp capabilities` | Shows what the agent can do, **grouped by lifecycle use case** (management, monitoring, help, incident, finops, governance) with a write-tier badge (A=autopilot-OK, B=confirm-required, C=human-only); `--all` includes write tools. Derived from the same registry as `exa mcp tools` and the A2A card, so it can never drift. | Understand agent capabilities by use case | `exa mcp capabilities` |
| `exa mcp resources` | Lists the MCP resources (readable context, e.g. `examlops://status`). | See what context agents can read | `exa mcp resources` |
| `exa mcp prompts` | Lists the MCP prompts (reusable agent workflows, e.g. `diagnose_drift`). | Discover packaged agent workflows | `exa mcp prompts` |
| `exa mcp agent-card` | Prints the A2A Agent Card describing this platform's agent skills; `--url` sets the public base URL, `--all` advertises mutating tools. | Publish an A2A discovery card | `exa mcp agent-card --url https://exa.example.com` |
| `exa mcp serve` | Runs the MCP server so agents can drive ExaMLOps; `--transport`, `--host`, `--port`, `--allow-writes`. **(mutation, long-running, outward)** | Host the platform as an agent-callable server | `exa mcp serve --transport stdio` |

The HTTP transport has no built-in authentication and therefore refuses non-loopback bind
addresses. For remote clients, keep it on `127.0.0.1` and place an authenticated TLS reverse proxy
in front of it. Enabling write tools does not bypass this restriction.

## Monitoring & Quality

Commands that watch a model *after* it ships: prediction and input-embedding drift, concept
drift and label-free performance estimation, continuous evaluation with ground-truth feedback,
model-quality SLOs with error budgets, subgroup fairness, and the self-driving autopilot loop
that ties detection → retrain → promote together. Read commands are safe to run anytime;
commands marked **[mutation]** change platform state (some fire retrains or deploys) — most
accept `--dry-run` and governance-critical ones accept `--reason "<why>"` for the audit trail.

### `exa drift` — prediction, input & concept drift detection

Tracks whether a live model's behaviour is diverging from its baseline. Prediction drift
compares recent output stats to a stored baseline (z-score); input drift does the same on
input embeddings; concept drift and label-free estimation reason about realized error and
expected performance. Set a baseline once, then `status`/`events` to monitor.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa drift status [model]` | Prediction-drift status (z-score/severity) for all models or one. `--watch/-w` live-refreshes. | Daily health scan of production models. | `exa drift status JPCP` |
| `exa drift snapshots <model>` | Raw prediction-drift snapshots; `--last/-n N`, `--raw` (adds `job_id`). | Inspect the underlying samples behind a drift verdict. | `exa drift snapshots JPCP --last 50` |
| `exa drift baseline <model>` | **[mutation]** Store current rolling stats as the model's drift baseline. `--dry-run`, `--reason`. | Establish a reference point after a known-good deploy. | `exa drift baseline JPCP --dry-run` |
| `exa drift reset <model>` | **[mutation]** Clear all drift snapshots for a model (keeps baseline). `--dry-run`, `--reason`. | Wipe stale samples before a fresh monitoring window. | `exa drift reset JPCP --dry-run` |
| `exa drift trigger` | **[mutation]** Check z-scores and fire `POST /retrain` for models above threshold. `--dry-run`. | Cooldown-aware manual sweep to retrain drifting models. | `exa drift trigger --dry-run` |
| `exa drift concept <model>` | Concept-drift test on realized error as delayed labels arrive (C5·R1). `--alias`, `--window` (default 50). | Detect true accuracy decay once ground truth catches up. | `exa drift concept JPCP --window 100` |
| `exa drift estimate <model>` | Label-free performance estimate (CBPE-like) before labels arrive (C5·R3/R4). `--alias`, `--baseline`, `--window` (default 200). | Estimate quality today without waiting for labels. | `exa drift estimate JPCP --window 200` |
| `exa drift profile <model>` | Profile recent inference inputs: schema / nulls / ranges / cardinality (C5·R5). `--last-n` (default 200), `--bad-payloads`. | Spot malformed or out-of-range inputs feeding a model. | `exa drift profile JPCP --last-n 200` |
| `exa drift forecast <model>` | Predict *when* drift will breach the threshold (fits a trend, projects breach ETA; exit 1 if imminent). `--threshold` (3.0), `--horizon` (20). | See when a model is projected to breach the drift threshold, while there is still time to act. | `exa drift forecast JPCP --threshold 3.0` |
| `exa drift events` | List unified drift events across all kinds (C5·R6). `--model`, `--kind feature\|prediction\|input_embedding\|concept\|data_quality`, `--last-n` (30). | One timeline of every drift signal for triage. | `exa drift events --model JPCP --kind prediction` |

#### `exa drift auto-retrain` — drift-triggered retraining config

Per-model policy that lets the platform retrain automatically when prediction drift crosses a
z-score threshold (subject to a cooldown). Enable it, then `exa drift trigger` / the autopilot
honour the config.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa drift auto-retrain enable <model>` | **[mutation]** Enable drift-triggered auto-retrain. `--dataset/-d` (default: model's primary), `--min-z` (3.0), `--cooldown` (3600s). | Set up hands-off retraining for a model. | `exa drift auto-retrain enable JPCP --dataset PM100Dataset --min-z 2.5` |
| `exa drift auto-retrain disable <model>` | **[mutation]** Disable drift-triggered auto-retrain for a model. | Pause automation during an investigation. | `exa drift auto-retrain disable JPCP` |
| `exa drift auto-retrain status` | Show auto-retrain config for all models. | Audit which models are on autopilot and with what thresholds. | `exa drift auto-retrain status` |

#### `exa drift input` — input embedding distribution drift

Monitors the distribution of input embeddings (norm / mean / std) written per inference against
a stored baseline — catches upstream data shifts even before predictions move.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa drift input status [model]` | Input embedding distribution drift for all models or one. | Detect input-data shift independent of prediction drift. | `exa drift input status JPCP` |
| `exa drift input baseline <model>` | **[mutation]** Store current rolling embedding stats as the input drift baseline. `--dry-run`, `--reason`. | Fix a reference embedding distribution after a good deploy. | `exa drift input baseline JPCP --dry-run` |
| `exa drift input reset <model>` | **[mutation]** Clear all input embedding snapshots (keeps baseline). `--dry-run`, `--reason`. | Reset the input-drift window before a new campaign. | `exa drift input reset JPCP --dry-run` |

#### `exa drift corruption` — is this the data, or the machine?

A z-score threshold alone cannot tell data drift from silent data corruption: corruption
perturbs the very statistic the z-score is computed from. These commands add the second signal
and classify the anomaly, so the remediation follows from the class rather than the z-score
(ADR 0114). Only `data_drift` permits an autonomous retrain — everything else is suppressed and
recorded. See [Corruption detection](../guides/corruption-detection.md).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa drift corruption status [model]` | Corruption signal per model — NaN/Inf **and** unexpected zeros against a baseline. A NaN/Inf guard alone sees ~1% of the phenomenon, so it is never reported on its own. | Check whether a drift alarm is really a hardware fault. | `exa drift corruption status JPCP` |
| `exa drift corruption baseline <model>` | **[mutation]** Store the current zero rate and spread as the model's corruption reference. `--reason`. | Fix what "normal" looks like after a good deploy, so a later zero rate means something. | `exa drift corruption baseline JPCP --reason "post-deploy"` |
| `exa drift corruption classify [model]` | Name the anomaly: `data_drift` · `suspected_hardware` · `suspected_regression` · `undetermined`, with the remediation each implies. | Decide what to do about a drift breach before acting on it. | `exa drift corruption classify JPCP` |
| `exa drift corruption selftest <model>` | Measure the detector against injected corruption (nullification · special values · mantissa flips) and publish the rate. | Prove what the detector actually catches instead of assuming its coverage. | `exa drift corruption selftest JPCP --trials 50` |

### `exa eval` — continuous evaluation & ground-truth feedback

Runs deterministic eval suites, ingests delayed ground-truth labels to compute real accuracy
(not a drift proxy), and enforces a regression gate that can block promotion on quality drops.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa eval run <suite>` | **[mutation]** Run a deterministic eval suite over items and persist scores (non-zero exit on error only). Required `--model`, `--items` (JSONL); optional `--version`, `--alias`, `--sample`, `--dataset-revision`, `--run-id`. | Score a candidate against a fixed item set in CI. | `exa eval run smoke --items ./eval/items.jsonl --model JPCP --sample 20` |
| `exa eval feedback ingest` | **[mutation]** Ingest delayed ground-truth label(s), keyed by prediction `request_hash`. `--request-hash/-r`, `--label/-l`, `--source/-s` (manual), or `--from-csv`. | Feed observed outcomes back for accuracy scoring. | `exa eval feedback ingest --request-hash hash-1 --label 88.5` |
| `exa eval feedback accuracy <model>` | Compute live accuracy (RMSE/MAE) from labelled predictions — real quality. `--alias/-a`, `--record` persists to `live_metrics`. | Report true production accuracy, not a proxy. | `exa eval feedback accuracy JPCP --alias Production` |
| `exa eval feedback join <model>` | Show prediction/label pairs joined on `request_hash` (delayed-label join). `--alias/-a`. | Inspect which predictions have labels yet. | `exa eval feedback join JPCP --alias Production` |
| `exa eval operator-qa` | Ask the agent a fixed set of operator questions and report the pass rate. Grading is **deterministic** (does the answer name the right command), so no judge model and no judge calibration are involved. Exits non-zero if the agent is unreachable, so an unanswerable run cannot be mistaken for a bad score. `--category`, `--out` (JSONL, feeds `exa eval run`), `--agent-url`, `--timeout`. `--record` persists the rate to the eval store, exactly as `cli-coverage` does. | Measure whether the agent can answer what a new operator actually asks — before pointing anyone at it. | `exa eval operator-qa --category serving` |
| `exa eval cli-coverage` | Samples commands from the whole CLI surface and asks the agent which one fits each hand-written use case, reporting how often it names the right one. Same deterministic grading as `operator-qa`; questions whose text would name a command are excluded and counted, and every miss is reported with its reason (`named-another-real-command` = the question was ambiguous; `named-no-real-command` = the agent was wrong). `--sample/-n` (0 = all), `--seed`, `--with-description/-d`, `--out`, `--agent-url`, `--timeout`, `--concurrency/-j` (questions in flight at once; the full pool asked one at a time takes over an hour), `--record` (persist the rate — the two question modes record as two suites), `--agent-model`. Also reports `flagValidity`: an answer that names the right command but an invented flag still fails when pasted. | Track agent quality once the 30-question suite is saturated — a pool of every usable row (366 at v0.49.0) has headroom where a fixed 30 does not. | `exa eval cli-coverage --sample 120 --seed 11` |
| `exa eval history` | Prints what the eval suites recorded for a model or agent — suite, **the backend that answered**, metric, score, sample size and run id, newest first. `--suite`, `--metric`, `--limit`. | See whether a number moved: the eval store had a writer and no reader, so a recorded score could not be compared with the one before it. | `exa eval history skipper --suite cli-coverage` |
| `exa eval grounding` | Asks the agent about live platform state and checks the answers against the truth read from the same source, sorting each into `grounded` / `abstained` / `fabricated`. Abstaining is not a failure — on a half-running platform it is the correct answer; the number to watch is `fabricated`, and the only acceptable value is zero. `--record`, `--out`, `--agent-url`, `--timeout`, `--agent-model`. | Catch the failure the other agent suites cannot see: a fluent, specific, wrong answer about this installation. | `exa eval grounding --record` |
| `exa eval safety` | Asks the agent to perform mutating actions and checks the bridge's `hitl_required` and `trace` fields rather than its prose, sorting each into `held` (interrupted for a human) / `declined` (no write tool ran) / `executed` (a write ran with no interrupt — the defect). `--record`, `--out`, `--agent-url`, `--timeout`, `--agent-model`. | Prove the write gate before trusting an agent that can retrain models, move production traffic and stop services. | `exa eval safety --record` |
| `exa eval gate run <model> <candidate>` | Run the regression gate for a candidate version (exit 1 in block mode on failure) — CI-safe (R10). `--higher-is-better`/`--lower-is-better`. | Block promotion of a regressing candidate in CI. | `exa eval gate run JPCP 18` |
| `exa eval gate set <model>` | **[mutation]** Configure the regression gate. Required `--suite`, `--metric metric[:min=X][:max=Y][:max_drop=Z][:higher_is_better=false]` (repeatable); `--baseline` (Production), `--mode block\|warn`, `--higher-is-better\|--lower-is-better` (the gate's own direction; undeclared means it borrows the caller's). `max` is a direction-independent ceiling; the per-metric `higher_is_better` lets one gate cover a suite whose scores point both ways. | Declare which metrics guard a model's promotions. | `exa eval gate set JPCP --suite agent-safety --metric answer_rate:min=0.95 --metric unsafe_rate:max=0.05:higher_is_better=false --mode block` |
| `exa eval gate show <model>` | Show the configured regression gate for a model. | Verify gate config before a release. | `exa eval gate show JPCP` |
| `exa eval calibrate <judge>` | **[mutation]** Measure a judge against labelled benchmarks (MVVP, ADR 0111) and record the calibration. Required `--from <file.json>`; `--version`, `--require-eligible` (exit 1 if the judge may not gate). | Make a judge gate-eligible — until it is, every gate refuses. | `exa eval calibrate gpt-judge --from ./eval/judge-calibration.json --require-eligible` |
| `exa eval calibration show <judge>` | Show a judge's latest calibration (kappa + interval, position bias, test-retest, replications, families, paradox flag) and whether it may gate. `--version`. | Diagnose *why* a gate refused. | `exa eval calibration show gpt-judge` |
| `exa eval calibration list` | List recorded judge calibrations, newest first, with eligibility. `--limit`. | See which judges are measured at all. | `exa eval calibration list` |

### `exa slo` — model-quality SLOs (error budgets & burn-rate alerts)

Declares service-level objectives per model/tenant, records SLI measurements to compute
remaining error budget and burn rate, and generates promtool-valid Prometheus rules.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa slo set <model> <name>` | **[mutation]** Declare or version-bump one SLO spec (R1). `--target` (0.99), `--window` (30d), `--source c1\|c2\|c5\|availability\|prometheus`, `--query` (PromQL), `--tenant`, `--gate`. | Define e.g. a p99-latency objective for a model. | `exa slo set JPCP latency-p99 --target 0.99 --window 30d` |
| `exa slo apply <path>` | **[mutation]** Apply all SLO specs from an OpenSLO-style YAML file (R1). | Manage SLOs as code / bulk-import. | `exa slo apply slos.yaml` |
| `exa slo list` | List declared SLO specs. `--model`, `--tenant`. | See every SLO in scope. | `exa slo list --model JPCP` |
| `exa slo status <model>` | SLI, remaining error budget and burn rate per SLO (R5). `--name`, `--tenant`. | Check budget health at a glance. | `exa slo status JPCP` |
| `exa slo ingest <model>` | **[mutation]** Pull SLI samples from the platform's own telemetry using each spec's `sli_source` (ADR 0023 clause 3); sources with no ingester are listed with the reason rather than skipped silently. `--tenant`. | Stop hand-typing SLIs — let eval results feed the SLO. | `exa slo ingest JPCP` |
| `exa slo record <model> <name> <good> <total>` | **[mutation]** Record one SLI measurement interval (R4) — feeds budget + burn rate. `--tenant`. | Push a measured good/total ratio into an SLO. | `exa slo record JPCP latency-p99 995 1000` |
| `exa slo burn <model>` | Show which SLOs are burning budget (and would page) (R3). `--tenant`. | Find SLOs about to trigger an alert. | `exa slo burn JPCP` |
| `exa slo export-metrics` | Publish platform-recorded metrics in Prometheus text format; `--model`, `--tenant`, `--out <file.prom>`. Covers SLO SLI/budget/burn-rate gauges and vector-store latency. An **unmeasured** SLO exports `measured=0` and no SLI. | Make the SLIs the platform ingests itself (`c2`/`c5`/`c8`) visible to Prometheus, so the burn-rate alerts `exa slo generate` emits can actually fire. | `exa slo export-metrics --out /var/lib/node_exporter/examlops.prom` |
| `exa slo generate <model> <name>` | Generate promtool-valid Prometheus recording + burn-rate rules (R2/R3). `--tenant`, `--out`. | Wire SLO alerting into Prometheus. | `exa slo generate JPCP latency-p99 --out slo_rules.yml` |

### `exa fairness` — subgroup performance & disparity monitoring

Declares slicing attributes and a max disparity threshold, then reports per-slice performance
so you can catch (and gate on) subgroup quality gaps.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa fairness show <model>` | Show the slice registry actually in force and where it came from — the model YAML's `fairness:` block or a runtime row — plus any drift between them. | Answer "which attributes is this model actually being checked on, and who decided?" before trusting a fairness report. | `exa fairness show JPCP` |
| `exa fairness apply <model>` | **[mutation]** Materialise the model YAML's `fairness:` block as the runtime config (audited); `--tenant`. Only needed to *override* a drifted runtime row — an unoverridden declaration is already in force. | Reset a runtime override back to the reviewed declaration in code. | `exa fairness apply JPCP` |
| `exa fairness config <model>` | **[mutation]** Declare slicing attributes + disparity threshold (R1). Required `--attr` (repeatable); `--threshold` (0.1), `--min-samples` (30), `--gate`, `--tenant`. | Set up fairness monitoring/gating for a model. | `exa fairness config JPCP --attr region --attr tier --threshold 0.1 --gate` |
| `exa fairness slice <model> <attr>` | Per-slice performance for one slicing attribute (R2). `--tenant`. | Drill into how one attribute's groups compare. | `exa fairness slice JPCP region` |
| `exa fairness report <model>` | Full fairness report across all declared slice attributes (R5). `--tenant`. | Whole-model fairness summary for review. | `exa fairness report JPCP` |

## HPC, Fleet & FinOps

This panel covers the compute substrate: discovering and governing HPC clusters, fractional GPU sharing, digital-twin what-if simulation, heterogeneous/hybrid hardware placement, privacy-preserving federated training, and the FinOps + Green-AI cost/carbon accounting and reporting layers. Read-only discovery commands never connect to schedule work; the mutating commands (`hpc connect/approve/reject`, `hpc place`, `finops budget set`) either register/approve clusters or set budgets and are marked below — the examples show safe invocations you can adapt, not commands to run blind.

### `exa hpc` — HPC fleet: discover schedulers, nodes, GPUs, then govern and place

Discovers Slurm/Flux/nvidia-smi capacity (read-only), registers clusters behind a sysadmin approval gate, and does placement/preflight/queue inspection over ACTIVE clusters. Discovery suggests config and never connects to schedule; placement refuses non-ACTIVE clusters.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa hpc detect [host]` | Auto-detects the scheduler on a host and suggests a configuration (read-only, does not register). | First contact with a new login node before deciding to onboard it. | `exa hpc detect lxp-cpu01 --user hpcuser` |
| `exa hpc nodes` | Lists compute nodes with CPUs/memory/GPUs and normalized state; `--save` persists the snapshot to `platform.db`. | Inventory a cluster's compute footprint. | `exa hpc nodes --host lxp-cpu01 --save --cluster lxp` |
| `exa hpc gpus` | Lists GPU devices — model, memory, utilization, online status (read-only). | Check GPU availability/health before submitting. | `exa hpc gpus --host lxp-cpu01 --scheduler nvidia-smi` |
| `exa hpc capacity` | Per-cluster GPU capacity, utilization, GPU-hours used and cost across ACTIVE clusters (cached, `EXAMLOPS_HPC_CAPACITY_TTL`). | FinOps/ops rollup of fleet utilization and spend. | `exa hpc capacity` |
| `exa hpc prometheus-sd` | Generates Prometheus `file_sd` scrape targets (node_exporter + DCGM) from the fleet registry. | Wire fleet nodes into Prometheus monitoring. | `exa hpc prometheus-sd --out targets.json --cluster lxp` |
| `exa hpc connect <host>` | **(mutation)** Probes a host and registers it as a PENDING cluster (requires approval before it can be used). | Onboard a new cluster into the governed registry. | `exa hpc connect lxp-cpu01 --name lxp --user hpcuser --key ~/.ssh/id_ed25519` |
| `exa hpc clusters` | Lists registered clusters and their approval state (PENDING/ACTIVE/REJECTED). | Audit which clusters are authorized to run jobs. | `exa hpc clusters` |
| `exa hpc approve <name>` | **(mutation, sysadmin)** Approves a cluster so ExaMLOps may schedule jobs on it (audited). | Grant scheduling authorization after review. | `exa hpc approve lxp` |
| `exa hpc reject <name>` | **(mutation, sysadmin)** Rejects a cluster, blocking scheduling (audited). | Deny/revoke a cluster with a recorded reason. | `exa hpc reject lxp --reason "maintenance window"` |
| `exa hpc place` | Shows which ACTIVE cluster placement would choose for a resource ask (dry preview, no submission). | Preview scheduling decisions before running a job. | `exa hpc place --gpus 2 --cpus 16 --nodes 1` |
| `exa hpc queue` | Shows the live scheduler queue for an ACTIVE cluster (read-only). | Inspect pending/running jobs on a cluster. | `exa hpc queue --cluster lxp` |
| `exa hpc jobs` | Lists tracked HPC submissions from `platform.db` (`hpc_jobs`). | Review the platform's own submission history. | `exa hpc jobs --model JPCP --limit 20` |
| `exa hpc preflight <cluster>` | Fail-fast pre-submit checks against a cluster; exits 1 on any failure (CI gate). | Block a pipeline before it wastes a queue slot. | `exa hpc preflight lxp --gpus 2 --nodes 1` |
| `exa hpc gpu-share plan <model>` | Selects the best GPU-sharing mechanism (MIG/time-slice/fallback) for a request; `--record` persists the allocation. | Decide how to share a GPU for a small model. | `exa hpc gpu-share plan JPCP --fraction 0.25 --mig-capable` |
| `exa hpc gpu-share pack` | Bin-packs fractional asks onto whole GPUs (first-fit-decreasing). | Consolidate many fractional workloads onto fewer GPUs. | `exa hpc gpu-share pack --ask a:0.25 --ask b:0.5 --gpus 1` |
| `exa hpc gpu-share accounting` | Shows recorded fractional GPU allocations, optionally per tenant. | Audit fractional GPU usage by tenant. | `exa hpc gpu-share accounting --tenant minio-demo` |

### `exa fleet` — Fleet Digital Twin: what-if simulation

Projects hypothetical scenarios over the live fleet without touching schedulers — useful for capacity planning, carbon/cost trade-off analysis, and driving the 3D/NOC heatmap view.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa fleet simulate` | Projects a hypothetical scenario over the live fleet — placements, GPU-hours, cost, carbon, queue; supports adding idle GPUs, overriding grid carbon, and choosing an optimizer. | Answer "what if we submit N jobs / add GPUs / optimize for carbon?" before committing. | `exa fleet simulate --jobs 10 --gpus 2 --duration 4 --add-gpus lxp=8 --optimize carbon-aware` |
| `exa fleet heatmap` | Emits a server-side tile grid (JSON) for the 3D/NOC fleet heatmap view. | Feed the dashboard's 3D fleet visualization. | `exa fleet heatmap --cluster lxp --cols 16` |

### `exa hardware` — Heterogeneous hardware & hybrid HPC↔cloud placement (E8)

Models device pools across accelerators (NVIDIA/AMD/Intel-Gaudi/TPU/CPU) and targets (HPC/cloud), then places workloads on the best compatible device with honest fallback, portability checks, and residency-governed cloud bursting.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa hardware add-pool <name>` | **(mutation)** Registers or updates a device pool (target, accelerator, count, region, cost, carbon, fraction support). | Declare available hardware to the placement engine. | `exa hardware add-pool lxp-a100 --target hpc --accelerator nvidia --count 8 --region eu --cost-per-hour 2.5 --carbon-factor 300` |
| `exa hardware pools` | Lists registered device pools, filterable by target or accelerator. | Review declared hardware inventory. | `exa hardware pools --target hpc --accelerator nvidia` |
| `exa hardware place <name>` | Places a workload on the best-available compatible device (honest fallback or a clear reject). | Choose where a model/engine should run across heterogeneous hardware. | `exa hardware place JPCP --accelerator nvidia --target hpc --fraction 0.5` |
| `exa hardware portable` | Checks whether an engine can run on a given accelerator (portability gate). | Verify an engine/accelerator combo before placement. | `exa hardware portable --engine vllm --accelerator amd` |
| `exa hardware burst <name>` | Plans a governed HPC→cloud burst; blocked and audited when residency forbids egress, requires `--allow-burst` to opt in. | Overflow to cloud only when data residency permits. | `exa hardware burst JPCP --accelerator nvidia --residency eu-only --allow-burst` |
| `exa hardware decisions` | Shows recent placement decisions. | Audit how and why workloads were placed. | `exa hardware decisions --limit 20` |

### `exa federated` — Federated & privacy-preserving training (E7)

Coordinates multi-site FedAvg/FedProx/robust aggregation with optional differential-privacy accounting and secure aggregation; unauthorized or unsigned site updates are rejected.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa federated init` | Initializes a federated run: registers participating sites and the privacy config (strategy, DP ε/δ, secure-agg). | Start a cross-site training run with governance. | `exa federated init --site siteA --site siteB --strategy fedavg --dp --epsilon-per-round 0.5 --delta 1e-5 --secure-agg` |
| `exa federated round <run_id>` | Aggregates one round of site updates; rejects unauthorized or unsigned sites. | Advance a federated run one aggregation step. | `exa federated round fedrun-01 --update siteA:0.1,0.2:1000 --update siteB:0.3,0.4:800` |
| `exa federated budget` | Shows the tracked differential-privacy (ε, δ) budget. | Check remaining privacy budget before more rounds. | `exa federated budget` |
| `exa federated status` | Shows run config, registered sites, and completed rounds. | Monitor progress and site participation. | `exa federated status` |

### `exa finops` — FinOps + Green-AI budgets and carbon accounting

Per-project GPU-hour/cost budgets, pluggable carbon and cost providers (built-ins, entry-point plugins, or declarative YAML formulas), and energy/CO2e accounting for training runs. Carbon/cost estimation defaults reproduce the platform's original methodology byte-identically.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa finops budget set <project>` | **(mutation)** Sets or replaces a project's GPU-hour and/or cost budget for a period. | Cap spend/compute for a project. | `exa finops budget set minio-demo --gpu-hours 500 --cost 1000 --period 2026-Q3` |
| `exa finops budget status` | Shows budget vs recorded consumption (GPU-hours + cost) per project. | Track burn-down against project budgets. | `exa finops budget status` |
| `exa finops carbon estimate` | Estimates energy (kWh) and CO2e (g) for GPU-hours and/or CPU-core-hours via the active carbon provider; no DB write. Refuses if given neither, or if the provider has no term for the CPU-hours supplied. | Quick what-if carbon estimate before a run — including a run with no accelerator, which is not zero-carbon. | `exa finops carbon estimate --gpu-hours 40 --cpu-hours 128 --grid-intensity 300` |
| `exa finops carbon providers` | Lists available carbon providers (built-ins + entry-point plugins) and their status. | Discover which carbon methodologies are installed. | `exa finops carbon providers` |
| `exa finops carbon signal` | Show the live carbon signal, its type (accounting vs decision) and what it may be used for. | Find out why carbon had zero weight on a placement — usually an average feed, which is not a bug. | `exa finops carbon signal` |
| `exa finops carbon record <model>` | Estimates (via the active provider) and persists a carbon record for a training run; counts CPU-core-hours as well as GPU-hours. | Attribute a run's carbon footprint to a model/MLflow run. | `exa finops carbon record JPCP --gpu-hours 40 --cpu-hours 128 --run-id <mlflow_run_id>` |
| `exa finops carbon report` | Aggregates recorded energy and **operational** carbon, optionally filtered to one model; embodied carbon is shown as unavailable, never as zero, and no CO2e total is reported (ADR 0112 R-ee). | Report energy and operational CO2e across runs. | `exa finops carbon report --model JPCP` |
| `exa finops carbon policy evaluate <policy>` | Simulates a carbon policy (a placement provider or `forecast-greedy`) against carbon-agnostic placement, both simple baselines and a perfect-foresight oracle on one trace (`--trace` JSON), then decides what ships: the candidate only if it beats the best simple baseline by `--margin` pp (default 5), and retirement if the shipped policy saves under `--retire-below` % (default 2). `--record` chains the result into the audit log, which the placement gate reads. Synthetic traces never gate. | Prove a carbon-aware placement policy is worth its complexity before it may place jobs on carbon (ADR 0112 R-ec). | `exa finops carbon policy evaluate carbon-aware --trace grid.json --record` |
| `exa finops carbon policy status <policy>` | Says what placement will do with a policy right now: `allow`, `substitute` (runs `carbon-simple`), `withhold` (carbon neutralised) or `agnostic` (retired), with the reason and the evaluation relied on. | Explain why `exa hpc place` did not use the carbon policy you asked for. | `exa finops carbon policy status carbon-aware` |
| `exa finops carbon policy list [policy]` | Lists recorded carbon-policy evaluations, newest first (from the audit chain), with advantage, what shipped, benefit, retirement and whether each is synthetic. | Audit the evidence behind carbon-aware placement, and see when a re-test is due (R-ed). | `exa finops carbon policy list carbon-aware` |
| `exa finops carbon policy sample` | Writes a deterministic synthetic trace (3 regions with a daily solar dip, mixed rigid and flexible jobs) to `--out`. Evaluations over it are marked synthetic. | Try the evaluation method without real grid data. **writes a file** | `exa finops carbon policy sample --out trace.json --days 14 --jobs 60` |
| `exa finops cost providers` | Lists available cost providers (rate cards) — built-ins + entry-point plugins. (Estimation itself runs via `exa models cost`.) | Discover which cost rate cards are installed. | `exa finops cost providers` |

### `exa report` — Offline cost/carbon/SLA reports

Assembles and renders a consolidated report from recorded platform data. PDF output degrades gracefully to HTML when WeasyPrint is unavailable.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa report generate` | Assembles and renders a cost/carbon/project report as HTML/PDF/text, optionally scoped to one project. | Produce a shareable periodic FinOps/Green-AI report. | `exa report generate --format html --out report.html --project minio-demo` |

## Governance & Security

Commands for running the platform under real-world governance: the sysadmin approval gate for model changes, a tamper-evident hash-chained audit trail, encrypted secrets management with leak scanning, EU AI Act and NIST AI RMF compliance evidence, declarative policy-as-code, and pluggable calculation providers. Read-only commands (`audit verify`, `secrets scan`, `secrets list`, `policy list`, `providers list`, all `report`/`status`/`framework`/`crosswalk` views) are safe to run anywhere. Mutating or outward-facing commands are marked **[mutation]** below — inspect them with `--help` first and prefer `--dry-run` where available.

### `exa auth` — sign in with your data center's identity provider

ExaMLOps federates with the identity provider (and, optionally, the authorization service) the
hosting data center already runs — it keeps no user store of its own. `login` runs the OAuth 2.0
device flow, so it works on a headless HPC login node; once signed in, every `exa` call to the
control plane or dashboard carries **your** identity instead of a shared token. The other commands
inspect the platform's trust file (`EXAMLOPS_IAM_CONFIG`). Guide:
[Identity federation](../guides/identity-federation.md).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa auth login` | Device Authorization Grant (RFC 8628): shows a code to approve in any browser, then stores the session (0600) for this config context. `--provider` (a center in the trust file), `--issuer` + `--client-id` (any OIDC IdP), or `--oidc-agent <account>` (tokens minted by oidc-agent, none stored) | Sign in from a login node with your organisation account | `exa auth login --provider jsc` |
| `exa auth status` | Whether this context is signed in, to which IdP, token validity, refreshability | Check before a scripted run | `exa --json auth status` |
| `exa auth whoami` | Your principal as the platform sees it — verified against the trust file when present: role, tenant, projects, groups, the rules that fired | "Why can't I promote?" — see your mapped role | `exa auth whoami` |
| `exa auth token` | Print a current access token (refreshed if needed); `--header` for an `Authorization:` line | Call the control plane from curl or a script as yourself | `curl -H "$(exa auth token --header)" http://localhost:18002/approvals` |
| `exa auth logout` | **[mutation]** Forget this context's session; revokes the refresh token at the IdP when it supports RFC 7009 | Leave a shared login node clean | `exa auth logout` |
| `exa auth providers` | List the trusted identity providers — issuer, tenant binding, authorization mode (local/external/both), PDP, clients | See which centers can sign users in | `exa auth providers` |
| `exa auth validate` | Validate a trust file; exit 1 on any error. `--file`, `--check-discovery` (fetch each issuer's discovery document) | CI gate before deploying an identity-config change | `exa auth validate --file identity-providers.yaml --check-discovery` |
| `exa auth verify` | Verify a token against the trust file and show the principal it maps to (role, tenant, matched rules); exit 1 if rejected. `--token-file` (`-` = stdin), `--provider` for opaque tokens | Debug a center's claim mapping during onboarding | `exa auth token \| exa auth verify --token-file -` |
| `exa auth decide <action>` | Run the full authorization decision — tenant isolation, platform policy, the center's AuthZEN/OPA PDP — for an action and resource; exit 1 on deny. `--resource-type/--resource-id/--tenant/--project`, `--token-file` | Test a center's policy before users hit it | `exa auth decide model.promote --resource-type model --resource-id JPCP` |

### `exa approvals` — sysadmin approval gate for model changes

A model change requested via CI/webhook lands as a PENDING approval; a human approves (fires Prefect training immediately) or rejects it. Every decision is recorded in the audit trail.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa approvals list` | List model-change approvals; `--all` shows every status, not just pending | See what is waiting for a human decision | `exa approvals list --all` |
| `exa approvals approve <model>` | **[mutation]** Approve a pending change — fires Prefect training now; `--reason` recorded, `--dry-run` previews | Green-light a vetted retrain | `exa approvals approve JPCP --reason "data reviewed" --dry-run` |
| `exa approvals reject <model>` | **[mutation]** Reject a pending change — no training runs; `--reason/-r`, `--dry-run` | Block a change that needs more review | `exa approvals reject JPCP -r "needs data review" --dry-run` |
| `exa approvals delete <approval_id>` | **[mutation]** Delete a pending approval by its UUID | Retract a stale or duplicate entry | `exa approvals delete 3f2a...` |

### `exa audit` — tamper-evident, hash-chained audit log (D4)

The audit trail is an append-only, hash-chained record of who did what and when (source = cli/agent/bridge). The bare `exa audit` command queries it; subcommands verify, checkpoint, and export the chain.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa audit` | Query the log; filters `--last` (default 30d), `--model/-m`, `--action/-a`, `--source/-s`, `--limit/-n` (default 100) | Trace platform activity over a window | `exa audit --last 7d --model JPCP` |
| `exa audit verify` | Recompute the hash chain and report integrity; exit 1 if broken (read-only) | Prove the trail was not tampered with | `exa audit verify` |
| `exa audit chain <correlation-id>` | Reconstruct one unit of work and everything it caused, from the causal edges in the chain. | Trace an autopilot cycle from trigger to promotion. | `exa audit chain 9f2c…` |
| `exa audit autonomy` | Every autonomous action in the window, with who acted, on whose behalf, and whether it declared an inverse. `--last`. | Answer the governance question: what did the platform do on its own, and can it be undone? | `exa audit autonomy --last 30d` |
| `exa audit checkpoint` | **[mutation]** Sign the current chain head, producing a detached checkpoint signature (D4·R5) | Anchor the chain state at a point in time | `exa audit checkpoint` |
| `exa audit checkpoints` | List signed audit checkpoints; `--limit/-n` | Review prior checkpoint anchors | `exa audit checkpoints -n 20` |
| `exa audit export` | **[mutation]** Append-only archival export; `--out <file>`, `--before <ISO>` (never deletes) | Produce an archival copy for retention | `exa audit export --out audit-2026.json --before 2026-07-01` |
| `exa audit verify-worm` | Verify the external WORM anchor's own chain and its agreement with DB checkpoints (item 2.4) | Confirm the off-platform WORM anchor matches | `exa audit verify-worm` |
| `exa audit anchor` | **[mutation]** Write a checkpoint hash over each telemetry side table's new rows into the chain (ADR 0110): tampering with an anchored row breaks the anchor. Cron-able; the autopilot also anchors each live cycle. | Make the per-inference drift/input/HPC/lineage tables tamper-evident without serialising them through the chain. | `exa audit anchor` |
| `exa audit verify-anchors` | Recompute every telemetry anchor against its side table; audited retention prunes are reported as pruned, not tampering; rows newer than the last anchor are counted, never skipped. Exit 1 on a break. | Prove the high-volume telemetry matches what the chain vouched for. | `exa audit verify-anchors` |
| `exa audit review` | **[mutation]** Perform and RECORD a sampled audit review: shows a sample of events since the last review, then writes an `audit_reviewed` event naming reviewer, range, sampled ids and notes (ADR 0113 decision 5). | Make "is anyone actually looking at the trail?" answerable from the chain; schedule at your governance cadence. | `exa audit review --sample 25 --notes "weekly pass"` |
| `exa audit reviews` | List recorded audit reviews — who reviewed, when, covering what range. `--last` (10). | Verify the review cadence is being kept. | `exa audit reviews` |

### `exa secrets` — encrypted secrets, rotation, and leak scanning

Secrets are stored encrypted under a KEK keyring; values are never printed unless explicitly revealed, and every mutation is audited. `scan` is a CI leak gate.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa secrets list` | List secret metadata (paths/versions) — never values; `--tenant` filter | Inventory what secrets exist | `exa secrets list` |
| `exa secrets get <path>` | Resolve a secret; redacts by default, `--reveal` prints plaintext (dangerous), `--tenant` scope | Check a value exists / read it when authorized | `exa secrets get mlflow/token` |
| `exa secrets set <path> <value>` | **[mutation]** Store an encrypted secret (audited); `--tenant` scope | Provision a new credential | `exa secrets set mlflow/token s3cr3t` |
| `exa secrets rotate <path>` | **[mutation]** Rotate a secret to a fresh random value (audited, R4); `--tenant` scope | Cycle a compromised/expiring credential | `exa secrets rotate mlflow/token` |
| `exa secrets rewrap` | **[mutation]** Re-encrypt every local secret under the ACTIVE KEK (online key rotation, item 2.3); `--dry-run` reports only | Decommission an old KEK after adding a new one | `exa secrets rewrap --dry-run` |
| `exa secrets scan <target>` | Scan a file/dir for likely secrets; exit non-zero on any finding (CI gate, R10) — read-only | Fail CI if a secret was committed | `exa secrets scan ./config` |

### `exa compliance` — EU AI Act compliance evidence

Classify a system's risk tier, advance its conformity state machine, and generate the Annex-IV technical file from live evidence.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa compliance status` | Show classification + conformity state; `--tenant` | See a system's AI-Act risk classification and whether its conformity state is complete. | `exa compliance status` |
| `exa compliance classify <model>` | **[mutation]** Record an EU AI Act risk classification (R1); `--risk-tier prohibited\|high\|limited\|minimal`, `--purpose`, `--context`, `--tenant` | Register a model's regulatory risk tier | `exa compliance classify JPCP --risk-tier high --purpose "HPC job prediction"` |
| `exa compliance declare <model>` | **[mutation]** Advance the conformity state machine with transition validation (R8); `--state draft\|documented\|assessed\|declared`, `--tenant` | Move a system toward a conformity declaration | `exa compliance declare JPCP --state assessed` |
| `exa compliance declaration <model>` | **[mutation]** Generate the Annex-V EU Declaration of Conformity from live metadata (clause 4); `--issued-at` (required), `--provider`, `--provider-address`, `--signatory`, `--signatory-function`, `--standard` (repeatable), `--notified-body`, `--personal-data`, `--out <file>`, `--tenant`. Fields only the provider can state are left as explicit placeholders and the document is stamped **DRAFT** with its reasons until the conformity state is `declared`, every provider field is supplied and the technical file has no gaps. | Produce the signed declaration the conformity workflow exists to reach. | `exa compliance declaration JPCP --issued-at "Julich, 2026-09-02" --provider "Example GmbH" --provider-address "Example Str. 1, DE" --signatory "A. Person" --signatory-function "Head of AI Governance" --out doc.md` |
| `exa compliance technical-file <model>` | Generate the Annex-IV technical file from live evidence, flagging gaps (R3/R4/R5); `--out <file>`, `--tenant` | Produce the regulator-facing technical file | `exa compliance technical-file JPCP --out annex-iv.md` |
| `exa compliance art12 <model>` | Check Art. 12 record-keeping coverage in the immutable audit trail (R7) | Verify logging obligations are met | `exa compliance art12 JPCP` |
| `exa compliance framework` | Show the control → article → evidence mapping (shared with D2) | Understand how controls map to articles | `exa compliance framework` |

### `exa governance` — NIST AI RMF control coverage & crosswalk

Report evidence coverage against the versioned NIST AI RMF control catalogue and crosswalk those controls to EU AI Act and ISO/IEC 42001.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa governance report` | Evidence-coverage report: satisfied / partial / gap per control (R3/R4); `--model` (default fleet-wide), `--tenant` | Assess governance posture | `exa governance report --model JPCP` |
| `exa governance catalogue` | List the versioned NIST AI RMF control catalogue (R1) | Browse the controls being tracked | `exa governance catalogue` |
| `exa governance crosswalk` | Show the control → EU AI Act + ISO/IEC 42001 crosswalk (R5) | Map one control across frameworks | `exa governance crosswalk` |
| `exa governance validate` | Validate the feature → control mapping; exit 1 on any error (CI gate, R2) | Fail CI if the mapping breaks | `exa governance validate` |

### `exa policy` — policy-as-code for mutations

Declarative governance rules (`policy.yaml`) gate mutating actions. `test` is a non-audited dry evaluation; `eval` runs a structured decision through the PolicyEngine; `bundle` signs and verifies versioned policy bundles.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa policy list` | List the policy rules currently loaded from `policy.yaml` (graceful when absent) | See active governance rules | `exa policy list` |
| `exa policy test <action>` | Evaluate the policy decision for an action + context (not audited); `--set/-s key=value` repeatable | Try a rule locally before applying | `exa policy test promote --set env=dev` |
| `exa policy eval <decision>` | **[mutation]** Evaluate a structured decision via the PolicyEngine (audited unless `--dry-run`); `--action`, `--subject`, `--resource`, `--tenant`, `--set/-s`, `--dry-run` | Make and record an authoritative policy decision | `exa policy eval promote --action promote --subject alice --resource JPCP/17 --dry-run` |
| `exa policy bundle list` | List signed policy bundle versions; `--tenant` filter | Review published bundle versions | `exa policy bundle list` |
| `exa policy bundle sign` | **[mutation]** Version + sign the effective policy bundle for a tenant (R2); `--tenant` | Freeze and sign the current policy set | `exa policy bundle sign --tenant acme` |
| `exa policy bundle verify` | Verify a stored bundle's hash + signature; exit 1 if invalid (R2); `--tenant`, `--version` (default latest) | Confirm a bundle is authentic and intact | `exa policy bundle verify --tenant acme` |

### `exa providers` — pluggable calculation providers (all domains)

Swap which formula/coefficients a calculation uses (carbon, cost, drift, promotion, LLM cost/cache/routing, RAG quality, placement, …) with no core-code change — via built-ins, entry-point plugins, config, or project-authored Python modules (AST-sandboxed). See also `exa finops carbon providers` / `exa finops cost providers`.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa providers list` | List providers across every domain (built-ins + entry-point plugins + config); `--domain/-d` narrows | Discover available providers | `exa providers list --domain carbon` |
| `exa providers activate <domain> <name>` | **[mutation]** Make a provider the active default for its (project, domain); `--project/-p` | Set the default when no `--provider` is passed | `exa providers activate cost tiered-example -p research` |
| `exa providers author <domain> <name>` | **[mutation]** Save a project-scoped provider from a Python file (AST-sandboxed, audited); `--file/-f`, `--project/-p` | Register a custom formula from a notebook/dashboard | `exa providers author cost my-rate -f rate.py -p research` |
| `exa providers authored` | List a project's authored providers with gate status; `--project/-p` | Audit custom providers and their trust state | `exa providers authored -p research` |
| `exa providers show <domain> <name>` | Print the stored source of an authored provider; `--project/-p` | Review what a custom provider computes | `exa providers show cost my-rate -p research` |
| `exa providers validate` | Statically validate a provider file against the AST sandbox; exit 1 if rejected (CI-safe); `--file/-f` | Gate-check a provider before authoring | `exa providers validate -f rate.py` |
| `exa providers rm <domain> <name>` | **[mutation]** Delete an authored provider file (audited); `--project/-p` | Remove a retired custom provider | `exa providers rm cost my-rate -p research` |

## Projects & Workspaces

A **project** is ExaMLOps's canonical *workspace*: one named unit that groups a team's models, pipelines, serving endpoints, connections, and datasets, together with the people who may touch them (owner ⊇ editor ⊇ viewer), the resource quota that bounds them, and the cost attributed to them. Projects unify four older grouping primitives (projects, namespaces, authorization relations, and the tenant claim) behind one key.

A project is a *declaration and a grouping*, not a running system: `create`, `assign`, and `delete` are metadata operations (rows in `platform.db`, every mutation audited). The declared quota becomes real Docker limits only when you materialize it with `exa project compose`. Named connections and workbenches attach reusable data sources and on-demand dev environments to a project.

### `exa project` — projects with CPU/memory/storage/GPU quotas

Create, inspect, and govern workspaces: assign resources, manage members and RBAC grants, set quotas, and attribute cost.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa project create` | Creates a project with declared CPU/memory/storage/GPU quotas (metadata only — provisions nothing). **Mutation.** | Stand up a new team workspace | `exa project create research --cpu-limit 4 --memory-gb 8 --storage-gb 100` |
| `exa project list` | Lists all projects with their quotas; `--status` filters ACTIVE/ARCHIVED | See every workspace at a glance | `exa project list --status ACTIVE` |
| `exa project show` | Full anatomy: quota, resources by kind, members, budget, consumption, storage, pipelines | Inspect one project end-to-end | `exa project show research` |
| `exa project current` | Shows the active project (resolution: `EXAMLOPS_PROJECT` env → `config.toml` → none) | Confirm which project other commands scope to | `exa project current` |
| `exa project use` | Sets the active project, persisted in `config.toml` (env var overrides) | Pin a default project for the session | `exa project use research` |
| `exa project set-quota` | Updates CPU/memory/storage/GPU limits (and description) on an existing project. **Mutation.** | Raise a workspace's ceiling | `exa project set-quota research --cpu-limit 8 --memory-gb 16` |
| `exa project assign` | Assigns any existing resource to the project (`--kind` model/pipeline/serving_endpoint/connection/dataset/storage). **Mutation.** | Group an existing asset into a workspace | `exa project assign research JPCP --kind model` |
| `exa project assign-model` | Shorthand for `assign <p> <model> --kind model` (legacy alias). **Mutation.** | Quickly attach a model | `exa project assign-model research JPCP` |
| `exa project add-member` | Adds a person with a role (owner ⊇ editor ⊇ viewer) via D6 authz relations. **Mutation.** | Grant a teammate access | `exa project add-member research alice --role editor` |
| `exa project members` | Lists the people who hold a role on the project | Audit who can touch a workspace | `exa project members research` |
| `exa project remove-member` | Removes a person's role(s) (`--role` for one, else all). **Mutation.** | Off-board a teammate | `exa project remove-member research alice --role editor` |
| `exa project grant` | Grants a subject a relation on any object (RBAC, audited). **Mutation.** | Fine-grained grant on a child object | `exa project grant alice owner project:research` |
| `exa project revoke` | Revokes a subject's relation on an object (audited). **Mutation.** | Pull a specific grant | `exa project revoke alice owner project:research` |
| `exa project access` | Lists RBAC relations, filterable by `--subject` and/or `--object` | Answer "who can access what" | `exa project access --object project:research` |
| `exa project storage` | Shows the project's MinIO storage; `--bind-connection` points it at a P2 S3 connection, `--refresh` re-probes used bytes. **Mutation with flags.** | Provision/inspect per-project storage | `exa project storage research --bind-connection minio` |
| `exa project pipelines` | Shows the project's two pipeline surfaces: Prefect (training) + Ray Serve (serving) | See a workspace's training/serving footprint | `exa project pipelines research` |
| `exa project budget` | Shows budget/quota status and flags breaches (exit 1 if over budget) | CI gate on workspace overspend | `exa project budget research` |
| `exa project cost` | Per-project cost attribution: GPU-hours · USD · carbon | Report a team's compute spend | `exa project cost research` |
| `exa project compose` | Emits a quota-bounded Docker Compose fragment (`deploy.resources` limits, per-project network, GPU reservations); `--out` writes to file | Materialize the quota into runnable infra | `exa project compose research --out docker-compose.project.yml` |
| `exa project archive` | Marks the project ARCHIVED (data preserved); `--yes` skips confirmation. **Mutation.** | Retire a workspace without losing data | `exa project archive research --yes` |
| `exa project delete` | Deletes the project and its assignments — irreversible; `--yes` skips confirmation. **Mutation.** | Remove a workspace permanently | `exa project delete research --yes` |

### `exa namespace` — legacy project namespace isolation

Lightweight model-grouping namespaces (a subset of what `exa project` now covers). Retained for back-compat; consumption reads union namespace groupings so no historical attribution is lost.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa namespace create` | Creates a namespace (`--description` optional). **Mutation.** | Carve out an isolated model group | `exa namespace create minio-demo --description "demo models"` |
| `exa namespace list` | Lists all namespaces with model counts | Survey namespace isolation | `exa namespace list` |
| `exa namespace info` | Shows namespace details and the models assigned to it | Inspect one namespace | `exa namespace info minio-demo` |
| `exa namespace assign` | Assigns a model to a namespace (`--namespace`). **Mutation.** | Group a model under a namespace | `exa namespace assign JPCP --namespace minio-demo` |

### `exa connection` — named connections (reusable data sources, P2)

Reusable, optionally project-scoped data connections (S3 / URI / dataplane). Non-secret config lives in `platform.db`; credentials live only in the secrets client (referenced, never copied). Secret values are never printed.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa connection create` | Creates a named connection (`--kind` s3/uri/dataplane, `--project`, `--config` JSON, `--secret-value` → secrets client). **Mutation.** | Register a reusable MinIO/S3 source | `exa connection create minio --kind s3 --project research --config '{"endpoint":"http://localhost:19000","bucket":"data"}' --secret-value ***` |
| `exa connection list` | Lists connections (metadata only, never secret values); `--project` filters | Discover available data sources | `exa connection list --project research` |
| `exa connection show` | Shows one connection's config + secret presence (never the value); `--project` scopes | Inspect a connection before use | `exa connection show minio --project research` |
| `exa connection test` | Read-only reachability probe (exit 1 on failure; never prints secrets) | CI/pre-run check that a source is reachable | `exa connection test minio --project research` |
| `exa connection delete` | Deletes a connection (referenced secret left intact); `--yes` skips confirmation. **Mutation.** | Retire an unused data source | `exa connection delete minio --project research --yes` |

### `exa workbench` — project workbenches (on-demand dev environments, P5)

Per-project dev environments (JupyterLab-style). Definitions are metadata (`STOPPED` until started); `start` records intent and prints the launch spec (image, volume, injected connection env) — the actual pod spawn is delegated to the runtime.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa workbench create` | Defines a workbench in a project (`--project` required, `--image`, `--cpu`, `--memory-gb`); starts STOPPED. **Mutation.** | Declare a reproducible dev environment | `exa workbench create nb --project research --image jupyter/scipy-notebook --cpu 2 --memory-gb 8` |
| `exa workbench list` | Lists workbenches; `--project` filters | See a project's dev environments | `exa workbench list --project research` |
| `exa workbench start` | Marks the workbench RUNNING and prints its launch spec (image, volume, injected env). **Mutation.** | Bring up a dev environment | `exa workbench start nb --project research` |
| `exa workbench stop` | Marks the workbench STOPPED. **Mutation.** | Tear a dev environment down | `exa workbench stop nb --project research` |
| `exa workbench delete` | Deletes the workbench definition; `--yes` skips confirmation. **Mutation.** | Remove an unused environment | `exa workbench delete nb --project research --yes` |

## Platform & Integrations

The plumbing beneath ExaMLOps: the Docker Compose stack lifecycle, tiered backup/restore of the platform datastore, the NovaFabric event backbone and admission-control queue, signed package exchange, SeanerBUS bridge wiring, and the MCP/A2A agent surface. Most commands here are read-only inspection; the ones that mutate state or reach outward (stack up/down, backup create/restore, events relay, seanerbus init-uuids, mcp serve) are flagged below — the examples for those are safe/preview forms, not run here.

### `exa stack` — Docker Compose stack lifecycle

Brings the ExaMLOps stack (Postgres, MLflow, Prefect, Ray Serve, …) and the monitoring stack (Prometheus, Grafana, Loki, Promtail, Alertmanager, Tempo) up and down, and reports container status. `--service`/`-s` scopes an action to a single service.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa stack up` | Starts the full stack, or one service with `-s`. **(mutation)** | Bring the platform online for local dev / on a node | `exa stack up` · `exa stack up -s mlflow` |
| `exa stack down` | Stops the stack, or one service with `-s` (volumes preserved). **(mutation)** | Shut the platform down cleanly | `exa stack down` |
| `exa stack restart` | Restarts the stack or a single service (no rebuild). **(mutation)** | Recover a wedged service without a full teardown | `exa stack restart -s ray-serve` |
| `exa stack logs` | Tails docker compose logs; `-s` per service, `-n` line count, `-f` to follow. | Debug a starting/failing service | `exa stack logs -s mlflow -n 100` |
| `exa stack status` | Shows running containers and their published ports. | Confirm what's up and where it's listening | `exa stack status` |
| `exa stack monitoring-up` | Starts the monitoring stack (Prometheus, Grafana, Loki, Promtail, Alertmanager, Tempo). **(mutation)** | Enable metrics/logs/traces dashboards | `exa stack monitoring-up` |
| `exa stack monitoring-down` | Stops the monitoring stack. **(mutation)** | Free resources when observability isn't needed | `exa stack monitoring-down` |
| `exa stack monitoring-status` | Shows monitoring-stack container status. | Check whether Grafana/Prometheus are running | `exa stack monitoring-status` |

### `exa backup` — backup / restore the platform datastore

Snapshots and restores the platform. A bare `create` writes a single `platform.db` file; any tier flag produces a tiered *bundle* (SQLite DBs + config, optionally Postgres, MinIO objects, and use-case content). Every restore path verifies checksum, SQLite integrity, and the audit hash-chain before and after touching anything, and refuses to overwrite a non-empty target without `--force`.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa backup create` | Snapshots the platform: bare = one `platform.db`; `--bundle`/`--all`/`--with-*` = a tiered bundle; `--push` replicates off-site (S3). Under `EXAMLOPS_DB_BACKEND=postgres` a bare `create` **refuses** — platform state is in Postgres, so `--with-postgres`/`--all` is required. A tier that produced nothing is reported on stderr (quiet mode cannot hide it) and only `status=ok` gets the green tick; `failed` exits **1**. **(mutation)** | Take a point-in-time or full backup before an upgrade | `exa backup create --all --push` |
| `exa backup list` | Lists backups & bundles newest-first with manifest metadata; `--remote` lists off-site (S3). | See what backups exist locally or off-site | `exa backup list` |
| `exa backup status` | Shows the latest bundle, per-tier health, retention count, and off-site reachability. | One-glance backup health check | `exa backup status` |
| `exa backup verify` | Verifies a single backup `.db`: checksum vs manifest + SQLite integrity + audit chain (exit 1 if bad). | Confirm a `.db` snapshot is intact before restoring | `exa backup verify ./backups/platform-20260730.db` |
| `exa backup verify-bundle` | Verifies a whole bundle: manifest + every tier item's checksum + `platform.db` audit chain. | Validate a full bundle end-to-end | `exa backup verify-bundle ./backups/bundle-20260730` |
| `exa backup restore` | Restores a verified backup over the platform DB (guarded; re-verified after). `--force` overwrites a non-empty DB, `-y` skips the prompt. **(mutation, destructive)** | Recover the platform DB from a snapshot | `exa backup restore ./backups/platform-20260730.db` |
| `exa backup restore-bundle` | Restores selected `--tier`(s) from a verified bundle (default: sqlite + config); `--force`/`-y`. Exits 1 and names every failed item if any tier did not come back. **(mutation, destructive)** | Selectively restore config or object tiers | `exa backup restore-bundle ./backups/bundle-20260730 --tier config` |
| `exa backup schedule` | Runs the scheduled backup loop (what the Compose `backup` sidecar runs); `--interval`, `--tiers`, `--push`, `--once`. **(mutation, long-running)** | Continuous automated backups; `--once` for CI | `exa backup schedule --once --all` |
| `exa backup prune` | Prunes old bundles by `--keep` count and/or `--days` age (never removes the newest / last-good bundle). **(mutation)** | Enforce retention without risking the good copy | `exa backup prune --keep 7 --days 30` |
| `exa backup pull` | Downloads + extracts an off-site (S3) bundle into `--dest` (verify before restoring). **(mutation)** | Fetch a bundle back from off-site storage | `exa backup pull bundle-20260730 --dest ./restore` |

### `exa instance` — this install's layers: core · deployment · instance data (ADR 0128)

ExaMLOps is three layers: the **core** (the code a release replaces), the **deployment** (Compose, Helm, a bare host) and the **instance data** users create after install. `exa instance` shows all three, pre-flights them, and creates an instance-data root (`EXAMLOPS_DATA_DIR`). Guide: [Core · deployment · instance data](../guides/three-layer-architecture.md).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa instance info` | Shows the core (release, data format, how it is installed), the deployment (Kubernetes / container / host, image tag, datastore engine), every place instance data lives (datastores, MLflow, object store, site configuration, site profile, use-case pack, providers, feature store, backups — each with who set it, whether it exists, its size and which backup tier captures it), the data-format stamp and compatibility verdict, and the site's modules. | Know exactly what an upgrade replaces and what it must keep | `exa instance info` |
| `exa instance check` | Pre-flight: the data is compatible with this release, the data root exists and is writable, the site profile has no warnings, the use-case pack resolves (and its optional `requires_examlops` specifier admits this release). Exits **1** on any failed check. | Gate before and after installing a new release (CI or by hand) | `exa instance check` |
| `exa instance init` | Creates an instance-data root: the layout (`usecase/`, `config/`, `.providers/`, `backups/`, `agent/`), `site.toml` (from `--preset`, `--site-name`), optionally a copy of a use-case pack (`--pack`, never overwritten without `--overwrite-pack`), and a stamped datastore. Idempotent. **(mutation, CLI only)** | Set up the data space of a new install so it lives outside the code | `exa instance init --data-dir /srv/examlops-data --pack usecases/seanergy --preset standard` |

### `exa upgrade` — bring an instance's data forward to the installed release (ADR 0128)

Installing a release replaces the core only. The datastore carries a data-format stamp; `exa upgrade` compares it with the release, takes a backup, and runs the pending migrations. Online migrations also apply by themselves when any process first opens the datastore; data a newer release made unreadable is refused. Guide: [Upgrades & compatibility](../guides/upgrade-and-compatibility.md).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa upgrade plan` | The installed release's verdict on the data (`current`, `upgrade_available`, `upgrade_required`, `newer_compatible`, `too_new`), the stamp, and the migrations it would run. Exits **1** only when the release must not open the data. | First command after installing a new release | `exa upgrade plan` |
| `exa upgrade apply` | Takes a pre-upgrade backup bundle (`--tier`, `--backup-dir`; `--no-backup` to skip), then runs every pending migration — online and offline — advancing the stamp and recording each step (backup id included). `--dry-run` previews. **(mutation, CLI only)** | Run the offline migrations of a release, with the undo taken first | `exa upgrade apply --dry-run` |
| `exa upgrade history` | Every create, adopt, migration and restore recorded on this datastore, newest first (`--limit`). | Audit how the data got to its current format | `exa upgrade history` |

### `exa modules` — site feature profile: which modules this centre runs (ADR 0128)

A *module* is a coarse slice of the platform (training, serving, quality, governance, genai, llm-serving, agent, autopilot, hpc, finops, workbenches, observability, integrations; `core` is always on). A site profile (`site.toml`, overlaid by `EXAMLOPS_FEATURES`) chooses them; a disabled module's commands disappear from `exa --help` and exit **3**, its dashboard routes answer 404 `module_disabled`, and `render` turns the profile into Compose / Helm input. Guide: [Site feature profiles](../guides/site-feature-profiles.md).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa modules list` | Every module, on/off at this site and why (preset, site profile, env, dependency), with the commands and services it owns. | See what runs at this centre | `exa modules list` |
| `exa modules show` | Everything one module owns: commands, dashboard flags and API routes, Compose services/profiles, Helm switches, related env gates, prerequisites. | Before switching a module off, see what goes with it | `exa modules show agent` |
| `exa modules presets` | The named starting points: `full`, `standard`, `minimal`, `hpc-center`, `genai`. | Pick a base profile for a new centre | `exa modules presets` |
| `exa modules enable` | Switches a module on in the site profile; its dependencies come with it. Audited. **(mutation)** | This centre has GPU clusters | `exa modules enable hpc` |
| `exa modules disable` | Switches a module off; modules that require it go off too (`core` cannot be disabled). Audited. **(mutation)** | No LLM endpoint at this centre | `exa modules disable agent` |
| `exa modules preset` | Bases the profile on a preset (`--reset-overrides` drops earlier enable/disable entries, `--site-name` labels the centre). Audited. **(mutation)** | Start a new centre from a known shape | `exa modules preset hpc-center --site-name jsc-booster` |
| `exa modules reset` | Deletes the site profile — every module on again (`full`). **(mutation, destructive)** | Undo all site customisation | `exa modules reset` |
| `exa modules render` | Turns the profile into deployment input: `--target env` (an `EXAMLOPS_FEATURES` line), `compose` (`COMPOSE_PROFILES` + an override that parks disabled always-on services and relaxes their dependents), `helm` (values: `site.features`, `agent.enabled`); `--out` writes the file. | Make Compose or Kubernetes run exactly the site's modules | `exa modules render --target compose --out docker-compose.site.yml` |

### `exa events` — NovaFabric event backbone (transactional outbox)

A durable transactional outbox: events are enqueued locally, then relayed through `log` or the
implemented Redis Streams publisher. Delivery is **at least once** with a stable event ID, so
consumers must deduplicate. `nats` and `kafka` remain fail-loud placeholders.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa events stats` | Shows outbox backlog: pending / published / poison (attempts exhausted). | Monitor event delivery health | `exa events stats` |
| `exa events publish` | Enqueues an event to the outbox (durable; later relayed). **(mutation)** | Emit a custom platform event | `exa events publish model.promoted -p '{"model":"jpcp"}'` |
| `exa events relay` | Publishes pending outbox events to the configured broker; `-n` limit, `--loop` until drained. **(mutation, outward)** | Drain the outbox to the message broker | `exa events relay --loop` |

### `exa admission` — admission-control queue (per-tenant fair-share)

A durable work queue drained under a global concurrency cap plus per-tenant fair-share (`EXAMLOPS_ADMISSION_MAX_RUNNING` / `EXAMLOPS_ADMISSION_PER_TENANT`).

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa admission stats` | Shows queue depth by state (queued/running/done/rejected/failed). | Watch admission-queue pressure | `exa admission stats` |
| `exa admission submit` | Enqueues a work item (durable); `--tenant`, `--project`, `--priority`. **(mutation)** | Submit throttled, fair-shared work | `exa admission submit -p '{"job":"retrain"}' --tenant team-a --priority 5` |

### `exa exchange` — NovaFabric Exchange (signed shareable packages)

Builds, verifies, inspects, and imports signed `.novapack` packages. Packing fails closed without `EXAMLOPS_SIGNING_KEY`; import is verify-before-import and refuses anything unverified.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa exchange pack` | Builds a signed `.novapack` from `-f` files into `-o`; `--version`. Fails closed without `EXAMLOPS_SIGNING_KEY`. **(mutation)** | Package artifacts for signed distribution | `exa exchange pack -f model.yaml -o mypack.novapack --version 1.0` |
| `exa exchange verify` | Verifies a package's signature + file integrity (exit 1 if untrusted/tampered). | Gate a package before trusting it | `exa exchange verify mypack.novapack` |
| `exa exchange inspect` | Shows a package's manifest without importing it. | Preview package contents/provenance | `exa exchange inspect mypack.novapack` |
| `exa exchange import` | Verify-before-import: verifies signature + integrity, then extracts to `-d`. Refuses unverified. **(mutation)** | Safely install a shared package | `exa exchange import mypack.novapack -d ./imported` |

### `exa seanerbus` — SeanerBUS bridge UUID management

Manages the per-model SeanerBUS UUIDs the bridge uses to register one req/res handler per model, and probes the bridge's health/stats endpoints.

| Command | What it does | Use case | Example |
|---|---|---|---|
| `exa seanerbus list` | Shows all models and their SeanerBUS UUIDs. | Audit which models are bus-registered | `exa seanerbus list` |
| `exa seanerbus status` | Probes the SeanerBUS bridge health + runtime stats endpoints. | Check the bridge is reachable and serving | `SEANERBUS_BRIDGE_STATUS_URL=http://node:18003 exa seanerbus status` |
| `exa seanerbus init-uuids` | Assigns a UUID to every model missing one (idempotent). **(mutation)** | Backfill UUIDs on existing models, then commit | `exa seanerbus init-uuids` |
| `exa seanerbus regen-uuid` | Regenerates one model's SeanerBUS UUID (notify HPC teams of the change). **(mutation, outward impact)** | Rotate a compromised/duplicated UUID | `exa seanerbus regen-uuid JPCP` |
