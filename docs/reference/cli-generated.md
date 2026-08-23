# `exa`

ExaMLOps platform CLI — manage models, training, inference, and services.

- `--output, -o` — Output format: table (human) | json | yaml | csv | md | html (scripting/agents/reports)
- `--json` — Shorthand for --output json (kept for compatibility)
- `--context, -c` — Use a named config context for this invocation
- `--yes, -y` — Skip all confirmation prompts
- `--quiet, -q` — Suppress non-essential output (hints, info, progress detail)
- `--verbose, -v` — Show extra diagnostic detail
- `--version, -V` — Print version and exit

## `exa admission`

Admission-control queue (per-tenant fair-share)

### `exa admission stats`

Show queue depth by state (queued/running/done/rejected/failed).

### `exa admission submit`

Enqueue a work item (durable; drained under the global + per-tenant caps).

- `--payload, -p` — JSON payload
- `--tenant` — Tenant for fair-share accounting
- `--project` — Project attribution
- `--priority` — Higher runs first within a tenant

## `exa agent`

Skipper agent — health, backend and memory

### `exa agent memory`

Enumerate, export and erase the agent's long-term memory (ADR 0034)

#### `exa agent memory delete`

Erase memories, cascading to derived ones. Audited to ``audit_events``.

The immutable audit log is a separate store and is deliberately *not* erased — ADR 0034
keeps the record that an erasure happened while removing what was remembered.

- `--scope` — Limit erasure to one scope (e.g. an operator)
- `--operator` — Who is performing the erasure (audited)

#### `exa agent memory export`

Export every stored memory as JSON — the subject-access half of ADR 0034.

- `--out` — Write JSON here instead of stdout

#### `exa agent memory list`

Enumerate stored memories of one kind.

- `--scope` — Task-class / model / operator scope
- `--limit` — Maximum items to show

#### `exa agent memory stats`

Summarise what the agent remembers, by memory kind.

### `exa agent status`

Show the agent's reachability, LLM backend, model and memory tier.

Exits non-zero when the agent is unreachable *or* when it is up but its backend is
unusable. Both mean "do not trust an answer from this agent", which is the question a
script is really asking, and collapsing them into one exit code is what makes this
usable as a health gate.

## `exa agentops`

AgentOps — agent trace & tool-call analytics

### `exa agentops anomalies`

Detect reasoning loops, step blowups, and cost overruns (R4, GWT-3/4).

- `--cost-budget` — USD budget for overrun check

### `exa agentops replay`

Reconstruct a session's tool-call timeline (R6, GWT-5).

### `exa agentops sessions`

List recent agent sessions with steps, cost, and status (R6 index).

- `--tenant` — Filter to one tenant
- `--status` — ok | anomaly | error
- `--limit` — Max sessions (newest first)

### `exa agentops tools`

Per-tool success rate, call count, and average latency (R2).

- `--tenant` — Filter to one tenant (D6)

## `exa approvals`

Sysadmin approval gate

### `exa approvals approve`

Approve a pending model change — fires Prefect training immediately.

- `--dry-run` — Show what would be approved without firing training
- `--reason` — Why you are making this change (recorded in the audit trail)

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
- `--stream` — Print the answer as it is generated (default: on at a terminal, off when piped)

## `exa assets`

Asset-centric pipelines — freshness DAG + rebuild (A4)

### `exa assets declare`

Declare an asset and its upstream dependencies (R1).

- `--kind` — dataset | feature | model
- `--deps` — Comma-separated upstream asset names
- `--description` — Human description

### `exa assets graph`

Print the asset DAG (coincides with the A2 lineage graph) (R6/GWT-4).

### `exa assets list`

List declared assets with their current version.

### `exa assets materialize`

Rebuild the asset + its stale ancestors only (R4/GWT-3).

- `--force` — Rebuild even if fresh

### `exa assets source-changed`

Advance a source asset's version so downstream assets go stale (GWT-2).

### `exa assets status`

Show the freshness graph — fresh/stale + why (R5/GWT-2).

## `exa audit`

Audit log — tamper-evident, hash-chained (D4)

- `--last` — Time window (e.g. 7d, 30d)
- `--model, -m` — Filter by target model
- `--action, -a` — Filter by action type
- `--source, -s` — Filter by source (cli/agent/bridge)
- `--limit, -n` — Max events to show

### `exa audit checkpoint`

Sign the current chain head, producing a detached checkpoint signature (D4·R5).

### `exa audit checkpoints`

List signed audit checkpoints.

- `--limit, -n` — Max checkpoints to show

### `exa audit export`

Archival export of the audit trail (D4·R4). Append-only — never deletes.

- `--out` — Write the archival JSON export to this file
- `--before` — Only events before this ISO timestamp

### `exa audit verify`

Recompute the hash chain and report integrity (D4·R2/R6). Exit 1 if broken.

### `exa audit verify-worm`

Verify the external WORM anchor: its own chain + agreement with the DB checkpoints (item 2.4).

## `exa autopilot`

Self-driving MLOps closed loop (detect→retrain→promote, policy-governed)

### `exa autopilot disable`

Disable the autopilot kill-switch (persistent, stored in platform.db).

### `exa autopilot enable`

Enable the autopilot kill-switch (persistent, stored in platform.db).

### `exa autopilot run`

Run one autopilot cycle: drift scan → policy → retrain → metrics → policy → promote.

- `--dry-run` — Preview without acting

### `exa autopilot status`

Show recent autopilot run history.

- `--last` — Number of recent runs to show

## `exa backup`

Backup / restore the platform datastore

### `exa backup create`

Snapshot the platform. Bare = single ``platform.db`` file; any tier flag = a tiered bundle.

- `--out, -o` — Directory to write the backup into
- `--all` — Full bundle: all SQLite DBs + config + Postgres + MinIO objects
- `--bundle` — Control-plane bundle (all SQLite DBs + config) instead of one .db
- `--with-postgres` — Include the Postgres tier
- `--with-objects` — Include the MinIO/object-store tier
- `--with-content` — Include use-case packs / envs / .dualgit classification
- `--push` — Replicate the finished bundle off-site (S3)
- `--strict` — Fail (don't skip) any requested tier that can't run

### `exa backup list`

List available backups & bundles (newest first) with their manifest metadata.

- `--dir, -d` — Backup directory
- `--remote` — List off-site bundles (S3) instead

### `exa backup prune`

Prune old bundles by count and/or age (never removes the newest / last-good bundle).

- `--dir, -d` — Backup directory
- `--keep` — Keep the newest N bundles
- `--days` — Keep bundles newer than N days

### `exa backup pull`

Download + extract an off-site bundle (verify it before restoring).

- `--dest` — Directory to extract into

### `exa backup restore`

Restore a verified backup over the platform DB (guarded + re-verified after).

- `--force` — Overwrite a non-empty target DB (DANGEROUS)
- `--yes, -y` — Skip the confirmation prompt

### `exa backup restore-bundle`

Restore selected tiers from a verified bundle (guarded; verifies before touching anything).

- `--tier` — Tier(s) to restore (repeatable). Default: sqlite + config
- `--force` — Overwrite non-empty targets (DANGEROUS)
- `--yes, -y` — Skip the confirmation prompt

### `exa backup schedule`

Run the scheduled backup loop (what the Compose ``backup`` sidecar runs).

- `--interval` — Seconds between cycles (env default)
- `--tiers` — Comma-separated tiers (env default)
- `--out, -o` — Backup directory (env default)
- `--push` — Replicate each bundle off-site
- `--all` — Shorthand for --tiers sqlite,config,postgres,objects
- `--once` — Run a single cycle then exit (for tests/CI)

### `exa backup status`

Show the latest bundle, per-tier health, retention count, and off-site reachability.

- `--dir, -d` — Backup directory

### `exa backup verify`

Verify a backup: checksum vs manifest + SQLite integrity + audit hash-chain. Exit 1 if bad.

### `exa backup verify-bundle`

Verify a whole bundle: manifest + every tier item's checksum + platform.db audit chain.

## `exa cards`

Croissant dataset cards + structured model cards

### `exa cards completeness`

Score model-card completeness (0..1) — the D5/C3 promotion gate signal (R6).

- `--tenant` — Tenant scope
- `--require` — Exit 1 if completeness below this fraction (0..1)

### `exa cards dataset`

Emit + validate a Croissant JSON-LD dataset card (R1/R2).

- `--revision` — Dataset revision (A1)
- `--license` — Dataset license
- `--out` — Write the Croissant JSON to this file

### `exa cards model`

Build a structured model card from live data — gaps as 'not provided' (R3/R4).

- `--tenant` — Tenant scope (D6)
- `--out` — Write the card Markdown to this file
- `--save` — Persist a versioned card

## `exa chat`

Interactive conversation with the Skipper agent (kq client)

## `exa compliance`

EU AI Act compliance — classify, Annex-IV, Art.12

### `exa compliance art12`

Check Art. 12 record-keeping coverage in the immutable audit trail (R7).

### `exa compliance classify`

Record a system's EU AI Act risk classification (R1).

- `--risk-tier` — prohibited|high|limited|minimal
- `--purpose` — Intended purpose
- `--context` — Deployment context
- `--tenant` — Tenant scope (D6)

### `exa compliance declare`

Advance the conformity state machine with transition validation (R8).

- `--state` — draft|documented|assessed|declared
- `--tenant` — Tenant scope

### `exa compliance framework`

Show the control→article→evidence mapping (shared with D2).

### `exa compliance status`

Show compliance classification + conformity state.

- `--tenant` — Tenant scope

### `exa compliance technical-file`

Generate the Annex-IV technical file from live evidence, flagging gaps (R3/R4/R5).

- `--out` — Write the Annex-IV Markdown to this file
- `--tenant` — Tenant scope

## `exa config`

CLI configuration

### `exa config contexts`

List configured contexts (environments) and show the active one.

### `exa config export`

One-file YAML snapshot of ALL ExaMLOps configuration (generated, secrets redacted).

Aggregates the CLI config (with provenance), contexts, the HPC cluster registry,
object-store split (artifact vs dataset MinIO), per-model YAMLs, environment
overlays, FinOps providers, and every platform env var — always derived live
from the real sources, so it can never drift from reality.

- `--out, -o` — Write the snapshot to a file instead of stdout

### `exa config init`

Interactive wizard — write ~/.config/examlops/config.toml.

### `exa config set`

Set a single config key in ~/.config/examlops/config.toml.

- `--context, -c` — Write into a named context instead of the default

### `exa config show`

Print the current resolved config (env vars + TOML file).

### `exa config use`

Switch the active context (environment).

## `exa connection`

Named Connections — reusable data sources (P2)

### `exa connection create`

Create a named connection.

- `--kind, -k` — Connection kind: s3, uri, dataplane
- `--project, -p` — Owning project (omit = global)
- `--config, -c` — Non-secret config as JSON
- `--secret-value` — Secret (stored in the secrets client, never in platform.db)

### `exa connection delete`

Delete a connection (the referenced secret is left intact).

- `--project, -p` — Owning project
- `--yes, -y` — Skip confirmation

### `exa connection list`

List connections (metadata only — never secret values).

- `--project, -p` — Filter by project

### `exa connection show`

Show one connection (config + secret presence, never the secret value).

- `--project, -p` — Owning project

### `exa connection test`

Read-only reachability probe (exit 1 on failure; never prints secrets).

- `--project, -p` — Owning project

## `exa data`

Dataset versioning & reproducibility (revisions, diff, checkout)

### `exa data checkout`

Materialise / verify the exact pinned data, or exit non-zero (spec R11).

- `--path, -p` — Local file/dir to verify against a content revision

### `exa data diff`

Report row-count / schema / size deltas between two revisions (spec R10).

### `exa data list`

List recorded revisions newest-first, with linked runs (spec R9).

- `--backend, -b` — Filter by backend

### `exa data retention-prune`

Prune unbounded per-inference telemetry (drift / input snapshots) older than --days.

Never touches the tamper-evident audit log or FinOps cost history. Use --dry-run first to see the
row counts, then run without it (optionally with --vacuum) to reclaim space.

- `--days` — Delete telemetry older than this many days
- `--dry-run` — Report what would be pruned; change nothing
- `--vacuum` — Reclaim freed pages after pruning

### `exa data snapshot`

Resolve the current dataset state to a revision and record it (spec R8).

- `--backend, -b` — Storage backend (zenodo|minio|dataplane)
- `--path, -p` — Local file/dir of already-materialised parquet to hash

### `exa data synth`

Synthetic data generation + fidelity/privacy gate (A7)

#### `exa data synth evaluate`

Score fidelity + privacy of an existing synthetic set and apply the gate (spec R2/R3).

- `--real` — Local parquet file/dir of the real data
- `--synthetic` — Local parquet file/dir of the synthetic data
- `--min-fidelity` — Fidelity release floor
- `--min-privacy` — Privacy release floor

#### `exa data synth fit`

Fit a generator to real data and report what it learned (spec R1 smoke-check).

- `--path, -p` — Local parquet file/dir of real data
- `--method, -m` — gaussian_copula | ctgan | tvae
- `--seed` — Deterministic seed

#### `exa data synth generate`

Generate, gate, and record a provenance-flagged synthetic dataset (spec R1–R4).

- `--path, -p` — Local parquet file/dir of real data
- `--rows, -n` — Number of synthetic rows to generate
- `--method, -m` — gaussian_copula | ctgan | tvae
- `--seed` — Deterministic seed
- `--min-fidelity` — Fidelity release floor
- `--min-privacy` — Privacy release floor
- `--out, -o` — Directory to write the released synthetic parquet
- `--force` — Record even if the gate blocks (still flagged, never as real)

### `exa data validate`

Validate a dataset against its data contract; exit non-zero on error violations (spec R11).

- `--path, -p` — Local parquet file/dir to validate
- `--revision` — A1 revision id for provenance

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

- `--dataset, -d` — Dataset class name (default: model's primary dataset)
- `--min-z` — Z-score threshold to trigger retrain
- `--cooldown` — Seconds between triggers

#### `exa drift auto-retrain status`

Show auto-retrain config for all models.

### `exa drift baseline`

Store current rolling stats as the drift baseline for a model.

- `--dry-run` — Show the baseline that would be set without writing it
- `--reason` — Why you are making this change (recorded in the audit trail)

### `exa drift concept`

Concept-drift test on realized error as delayed labels arrive (C5·R1).

- `--alias` — Restrict to one serving alias
- `--window` — Recent window size (samples)

### `exa drift estimate`

Label-free performance estimate (CBPE-like) before labels arrive (C5·R3/R4).

- `--alias` — Restrict to one serving alias
- `--baseline` — Baseline metric to compare against
- `--window` — Recent predictions to estimate over

### `exa drift events`

List unified drift events across all kinds (C5·R6).

- `--model` — Filter to one model
- `--kind` — feature|prediction|input_embedding|concept|data_quality
- `--last-n` — Max events (newest first)

### `exa drift forecast`

Predict WHEN a model's drift will breach the threshold (pre-emptive, item 5.2).

Fits a trend to recent prediction drift and projects the breach ETA, so the autopilot can
retrain BEFORE the degradation window instead of after. Exit 1 if a breach is imminent.

- `--threshold` — Critical z-score threshold
- `--horizon` — Look-ahead steps

### `exa drift input`

#### `exa drift input baseline`

Store current rolling embedding statistics as the input drift baseline.

- `--dry-run` — Show the input baseline that would be set without writing it
- `--reason` — Why you are making this change (recorded in the audit trail)

#### `exa drift input reset`

Clear all input embedding snapshots for a model (keeps baseline).

- `--dry-run` — Show how many snapshots would be cleared without deleting them
- `--reason` — Why you are making this change (recorded in the audit trail)

#### `exa drift input status`

Show input embedding distribution drift for all models (or one model).

### `exa drift profile`

Profile recent inference inputs: schema / nulls / ranges / cardinality (C5·R5).

- `--last-n` — Recent predictions to profile
- `--bad-payloads` — A5 bad-payload count to fold in

### `exa drift reset`

Clear all drift snapshots for a model (keeps baseline).

- `--dry-run` — Show how many snapshots would be cleared without deleting them
- `--reason` — Why you are making this change (recorded in the audit trail)

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

## `exa embedding`

Embedding lifecycle — encoders + blue-green reindex (B6)

### `exa embedding list`

List registered encoders.

### `exa embedding register`

Register a versioned encoder → encoder_id (R1).

- `--dim` — Embedding dimension
- `--metric` — cosine | dot | l2
- `--norm` — Normalization (l2/none)

### `exa embedding reindex`

Blue-green reindex to a new encoder — verified switch, old retained then pruned (R4/R5).

- `--tenant` — Tenant scope
- `--corpus-size` — Docs to re-embed
- `--recall` — Measured recall of the new index
- `--recall-floor` — Minimum recall to switch

### `exa embedding set-encoder`

Bootstrap a collection's active encoder (R2).

- `--tenant` — Tenant scope

### `exa embedding status`

Show a collection's active/staging encoder + reindex history.

- `--tenant` — Tenant scope

## `exa env`

Show the effective configuration and the source of every value.

With ``--validate``, cross-check the effective environment for enterprise-readiness (backend ↔
endpoint consistency, OIDC coherence, weak/placeholder secrets) and exit non-zero on any error.

- `--validate` — Check the effective config for coherence; exit 1 on errors (4.2)

## `exa eval`

Continuous evaluation and feedback

### `exa eval calibrate`

Measure a judge against labelled benchmarks and record the calibration.

Recording a *failing* calibration is not an error: the measurement is the point. Pass
``--require-eligible`` to make a CI job fail on a judge that may not gate.

- `--from` — JSON of collected judgments (see `exa eval calibration list -h`)
- `--version` — Judge prompt/model version
- `--require-eligible` — Exit 1 if the judge fails the MVVP — CI-safe

### `exa eval calibration`

Judge calibration — measure a judge before it may gate (ADR 0111)

#### `exa eval calibration list`

List recorded judge calibrations, newest first.

- `--limit` — Rows to show

#### `exa eval calibration show`

Show a judge's latest calibration and whether it may gate.

- `--version` — Pin to a judge version

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

### `exa eval gate`

Eval regression gate (block/warn promotion on regression)

#### `exa eval gate run`

Run the gate for a candidate version (exit 1 in block mode on failure) — CI-safe (R10).

- `--higher-is-better` — Metric direction

#### `exa eval gate set`

Configure the regression gate for a model.

- `--suite` — C2 suite that produces the scores
- `--metric` — metric[:min=X][:max_drop=Y] (repeatable)
- `--baseline` — Baseline alias
- `--mode` — block | warn

#### `exa eval gate show`

Show the configured gate for a model.

### `exa eval operator-qa`

Ask the agent a fixed set of operator questions and report the pass rate.

Measures whether the agent can answer what a new operator actually asks. Grading is
deterministic (does the answer name the right command), so no judge model is involved and
no judge calibration is required. Exits non-zero if the agent is unreachable, so an
unanswerable run can never be mistaken for a bad score.

- `--category` — Only questions in this category
- `--out` — Write the answers as JSONL (feeds `exa eval run`)
- `--agent-url` — Agent bridge base URL (default: configured agent_url)
- `--timeout` — Per-question timeout in seconds

### `exa eval run`

Run a deterministic eval suite over items and persist scores (exit != 0 on error only).

- `--model` — Model the suite evaluates
- `--items` — JSONL of {output, reference?, prompt?}
- `--version` — Candidate model version
- `--alias` — Alias being evaluated
- `--sample` — Sample N items by request_hash
- `--dataset-revision` — A1 revision
- `--run-id` — Idempotency key (default: derived)

## `exa events`

NovaFabric event backbone (transactional outbox)

### `exa events publish`

Enqueue an event to the outbox (durable; relayed by `exa events relay`).

- `--payload, -p` — JSON payload

### `exa events relay`

Publish pending outbox events to the configured broker (EXAMLOPS_EVENT_PUBLISHER).

- `--limit, -n` — Max events to publish this pass
- `--loop` — Keep relaying until the outbox is drained

### `exa events stats`

Show outbox backlog: pending / published / poison (attempts exhausted).

## `exa exchange`

NovaFabric Exchange — signed shareable packages

### `exa exchange import`

Verify-before-import: verify signature + integrity, then extract. Refuses unverified.

- `--dest, -d` — Directory to extract into

### `exa exchange inspect`

Show a package's manifest without importing it.

### `exa exchange pack`

Build a signed .novapack (fails closed without EXAMLOPS_SIGNING_KEY).

- `--file, -f` — File to include (repeatable)
- `--out, -o` — Output .novapack path
- `--version` — Package version

### `exa exchange verify`

Verify a package's signature + file integrity. Exit 1 if untrusted/tampered.

## `exa explain`

Explain what a command does, in plain language, with examples.

## `exa fairness`

Fairness — subgroup performance & disparity monitoring

### `exa fairness config`

Declare slicing attributes + disparity threshold for a model (R1).

- `--attr` — Slicing attribute (repeatable)
- `--threshold` — Max allowed disparity
- `--min-samples` — Noise guard per slice
- `--gate` — Gate promotion on disparity (C3)
- `--tenant` — Tenant scope (D6)

### `exa fairness report`

Full fairness report across all declared slice attributes (R5).

- `--tenant` — Tenant scope

### `exa fairness slice`

Show per-slice performance for one slicing attribute (R2, GWT-1).

- `--tenant` — Tenant scope

## `exa feature`

Feature store — one train/serve definition, no skew (A3)

### `exa feature apply`

Register/patch a feature view — the single train+serve definition (R1).

- `--entity` — Entity the view is keyed on
- `--features` — Comma-separated feature names
- `--source` — Offline source hint (parquet/table)
- `--ttl` — Freshness TTL in seconds (0 = no staleness alert)
- `--revision` — A1 dataset revision pin

### `exa feature freshness`

Show materialization age and staleness vs the view TTL (R6/GWT-4).

### `exa feature get`

Read an entity's feature vector — online (default) or point-in-time offline (--asof).

- `--entity-id` — Entity id
- `--asof` — Point-in-time (offline as-of) instead of the online value

### `exa feature ingest`

Record an offline feature observation (point-in-time source of truth).

- `--entity-id` — Entity id
- `--event-ts` — Event timestamp (YYYY-MM-DD HH:MM:SS)
- `--values` — JSON object of feature values

### `exa feature list`

List registered feature views.

### `exa feature materialize`

Materialize latest offline values → online store (R6).

- `--start` — Window start timestamp
- `--end` — Window end timestamp

### `exa feature skew`

Assert online == offline as-of for an entity (skew must be zero) (R2/GWT-1).

- `--entity-id` — Entity id
- `--asof` — Event timestamp to compare as-of

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

## `exa federated`

Federated & privacy-preserving training — FedAvg/DP/secure-agg (E7)

### `exa federated budget`

Show the tracked differential-privacy (ε, δ) budget.

### `exa federated init`

Initialize a federated run: register sites + privacy config.

- `--site` — Participating site (repeatable)
- `--strategy` — fedavg | fedprox | robust
- `--run-id` — Explicit run id (else derived)
- `--dp` — Enable differential privacy accounting
- `--epsilon-per-round` — DP ε spent per round
- `--delta` — DP δ
- `--secure-agg` — Hide per-site updates
- `--unauthorized` — Site to register but NOT authorize (repeatable)

### `exa federated round`

Aggregate one round of site updates (rejects unauthorized/unsigned sites).

- `--update` — site:w1,w2,…:num_samples[:loss] (repeatable)
- `--unsigned` — Treat this site's update as unsigned (repeatable)

### `exa federated status`

Show run config, sites, and completed rounds.

## `exa finetune`

Run a fine-tune and register a signed, lineage-linked adapter (R1/R3/GWT-1).

- `--method` — lora | qlora | full
- `--dataset` — A1-pinned dataset revision
- `--rank` — LoRA rank
- `--target-modules` — Comma-separated modules
- `--eval` — Recorded eval score
- `--eval-floor` — C3 quality floor for promotion
- `--cost` — Fine-tune GPU-hours
- `--adapter-id` — Explicit adapter id

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

## `exa fleet`

Fleet Digital Twin — what-if simulation

### `exa fleet heatmap`

Server-side tile grid for the 3D/NOC fleet heatmap (item 5.4); JSON the 3D view renders.

- `--cluster, -c` — Limit to one cluster
- `--cols` — Grid width override

### `exa fleet simulate`

Project a hypothetical scenario over the live fleet — placements, GPU-hours, cost, carbon, queue.

- `--jobs, -j` — Number of jobs to submit in the scenario
- `--gpus, -g` — GPUs per job
- `--nodes, -N` — Nodes per job
- `--duration` — Hours per job (for cost/carbon)
- `--add-gpus` — Add idle GPUs, e.g. --add-gpus lxp=8 (repeatable)
- `--carbon` — Override grid carbon, e.g. --carbon lxp=600 (repeatable)
- `--optimize` — Placement provider: least-loaded|carbon-aware|cost-aware|carbon-cost-balanced

## `exa gateway`

Model gateway — virtual keys, routing, and cost

### `exa gateway cache`

Semantic cache (B3) — hit-rate + measured savings

#### `exa gateway cache stats`

Show semantic-cache hit-rate and token/cost savings (B3).

- `--tenant` — Filter to one tenant

### `exa gateway chat`

Send one chat message through the gateway (uses the default echo route).

- `--message` — User message
- `--key` — Virtual key to authenticate with
- `--cache` — Route through the B3 semantic cache

### `exa gateway key`

Virtual key administration

#### `exa gateway key issue`

Issue a virtual key (printed once — only its hash is stored).

- `--tenant` — Tenant the key belongs to
- `--project` — Project the key belongs to
- `--model` — Allow-list model (repeatable; omit = all models)
- `--budget` — Budget in USD (omit = unlimited)

#### `exa gateway key list`

List virtual keys (hashes only).

#### `exa gateway key revoke`

Revoke a virtual key by its stored hash.

### `exa gateway reasoning`

Reasoning ops — budget/accounting/trace (B8)

#### `exa gateway reasoning account`

Account reasoning vs output tokens/cost separately (R5).

- `--reasoning` — Reasoning (thinking) tokens
- `--output` — Output tokens
- `--reasoning-rate` — $/reasoning token
- `--output-rate` — $/output token
- `--tenant` — Tenant scope

#### `exa gateway reasoning budget`

Show how a reasoning budget caps a request (R4).

- `--max` — Reasoning budget (max thinking tokens)

#### `exa gateway reasoning stats`

Reasoning-vs-output token/cost split + structured-output outcomes.

- `--model` — Filter by model
- `--tenant` — Filter by tenant

### `exa gateway schema`

Structured output — schema-constrained (B8)

#### `exa gateway schema test`

Validate (and optionally repair) an object against a JSON Schema (R1/R8).

- `--repair` — Attempt repair on invalid

## `exa genai`

GenAI observability (OpenTelemetry semconv) + token cost

### `exa genai check`

Show GenAI telemetry status: tracing on/off, content capture, semconv version.

### `exa genai cost`

Estimate the USD cost of a GenAI call from its token usage (spec R7).

- `--model, -m` — Model name (e.g. gpt-4o)
- `--in` — Input (prompt) token count
- `--out` — Output (completion) token count

## `exa governance`

NIST AI RMF control coverage & crosswalk

### `exa governance catalogue`

List the versioned NIST AI RMF control catalogue (R1).

### `exa governance crosswalk`

Show the control → EU AI Act + ISO/IEC 42001 crosswalk (R5).

### `exa governance report`

Evidence-coverage report: satisfied / partial / gap per control (R3/R4).

- `--model` — One model (default: fleet-wide posture)
- `--tenant` — Tenant scope (D6)

### `exa governance validate`

Validate the feature→control mapping (CI gate, R2). Exit 1 on any error.

## `exa guardrails`

Guardrails — injection/PII/toxicity defense

### `exa guardrails check-tool`

Check an agent tool call against the per-tenant allow-list (R7).

- `--allow` — Allowed tool (repeatable)
- `--mode` — off | monitor | enforce
- `--tenant` — Tenant scope

### `exa guardrails stats`

Show guardrail action counts (allow/redact/block).

- `--tenant` — Filter to one tenant

### `exa guardrails test`

Run a text through the guardrail and show the action + findings.

- `--text` — Text to run through the guardrail
- `--direction` — input | output
- `--mode` — off | monitor | enforce
- `--tenant` — Tenant policy scope (D6)

## `exa hardware`

Heterogeneous hardware & hybrid HPC↔cloud placement (E8)

### `exa hardware add-pool`

Register (or update) a device pool.

- `--target` — hpc | cloud
- `--accelerator` — nvidia|amd|intel-gaudi|tpu|cpu
- `--capability` — Capability tag (repeatable)
- `--count` — Devices available
- `--region` — Region (for residency + carbon)
- `--cost-per-hour` — Cost per device-hour
- `--carbon-factor` — gCO2e per device-hour
- `--supports-fractions` — Vendor GPU fractioning (MIG)

### `exa hardware burst`

Plan a governed HPC→cloud burst (blocked + audited when residency forbids egress).

- `--accelerator` — Requested accelerator
- `--engine` — Engine (E2)
- `--residency` — open | eu-only | no-egress
- `--allow-burst` — Opt in to cloud burst

### `exa hardware decisions`

Show recent placement decisions.

- `--limit` — Rows to show

### `exa hardware place`

Place a workload on the best-available compatible device (honest fallback / clear reject).

- `--accelerator` — Requested accelerator
- `--engine` — Serving/training engine (E2)
- `--target` — hpc | cloud (default: any)
- `--capability` — Required capability (repeatable)
- `--fraction` — GPU fraction (0<f≤1)

### `exa hardware pools`

List registered device pools.

- `--target` — Filter by hpc|cloud
- `--accelerator` — Filter by accelerator

### `exa hardware portable`

Check whether an engine can run on a given accelerator (portability gate).

- `--engine` — Engine name
- `--accelerator` — Target accelerator

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

### `exa hpc gpu-share`

Fractional GPU allocation & bin-packing (E3)

#### `exa hpc gpu-share accounting`

Show recorded fractional GPU allocations.

- `--tenant` — Filter to one tenant

#### `exa hpc gpu-share pack`

Bin-pack fractional asks onto whole GPUs (first-fit-decreasing).

- `--ask` — label:fraction (repeatable)
- `--gpus` — Number of whole GPUs available
- `--mig-capable` — Cluster supports MIG
- `--timeslice` — Cluster supports time-slicing

#### `exa hpc gpu-share plan`

Select the best GPU-sharing mechanism for a request (honest fallback).

- `--fraction` — GPU fraction requested (0..1)
- `--mig` — MIG profile (e.g. 2g.10gb)
- `--mig-capable` — Cluster supports MIG
- `--timeslice` — Cluster supports time-slicing
- `--record` — Persist the allocation
- `--tenant` — Tenant scope

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

### `exa hpc prometheus-sd`

Generate Prometheus file_sd scrape targets (node_exporter + DCGM) from the fleet registry (3.1).

- `--out, -o` — Write file_sd JSON here (else stdout)
- `--cluster, -c` — Limit to one cluster

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

### `exa mcp capabilities`

Show what the agent can do, grouped by lifecycle use case (management, monitoring, …).

- `--all` — Include mutating (write) tools even if writes are disabled

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

### `exa models bom`

Generate a CycloneDX AI-BOM for a model version.

- `--dataset` — Training dataset name
- `--dataset-revision` — Pinned dataset revision (A1)
- `--framework` — ML framework
- `--output` — Write BOM JSON to this file

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

### `exa models engine`

Inspect and validate per-model engine config

#### `exa models engine list`

List available inference engines.

#### `exa models engine validate`

Validate a model YAML's engine block (the CI integrity guard uses the same check).

### `exa models info`

Show detail for one model: all versions, aliases, metrics.

### `exa models lineage`

Show the pipeline → dataset → model version lineage chain (or the A2 graph).

- `--graph` — Show the upstream+downstream provenance graph (A2)
- `--impact` — List model versions derived from a dataset revision (A2)

### `exa models list`

List all registered models with their production alias and latest version.

### `exa models quantize`

Quantize a model → register a new signed + BOM'd version (GWT-3).

- `--method` — awq | gptq | fp8 | int8
- `--path` — Local artifact dir to sign for the new version (D3)
- `--dataset` — Training dataset (for BOM)
- `--dataset-revision` — Pinned dataset revision (for BOM)

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

### `exa models sign`

Sign a model artifact bundle (HMAC fallback or Sigstore keyless).

- `--path` — Local artifact file or directory to sign

### `exa models verify`

Verify a model's signature against current artifact bytes (verify-before-load gate).

- `--path` — Local artifact file or directory to verify
- `--mode` — enforce (exit 1 on failure) or warn (record only)

## `exa modelzoo`

ModelZoo repository freshness and events

### `exa modelzoo adopt`

Provision one project per model (storage · MinIO connection · budget · workbench · pipelines).
Idempotent.

Wires each model its own project with a bound per-project MinIO/S3 connection (endpoint/keys from
the platform S3 env; secret via the secrets store, never printed). A project can still hold
several models via `exa project assign` — this just makes one-project-per-model the zero-effort
default (ADR 0086).

- `--all` — Adopt every Zoo/pack model.
- `--connection-name` — Name of the per-project S3/MinIO connection to provision.
- `--no-connection` — Skip provisioning the per-project MinIO connection.
- `--dry-run` — Preview what would be provisioned without writing.

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
already lives in modelzoo/seanergys_modelzoo/models/tasks/.

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

### `exa pipeline distributed`

Distributed training + checkpoint/resume (E6)

#### `exa pipeline distributed checkpoint`

Write an integrity-hashed sharded checkpoint (R3/R5).

- `--step` — Training step
- `--epoch` — Training epoch
- `--shards` — Number of shards
- `--state` — JSON optimizer/model state summary

#### `exa pipeline distributed launch`

Launch a distributed training run (R1/R2/R8).

- `--nodes` — Number of nodes
- `--gpus-per-node` — GPUs per node
- `--strategy` — fsdp | zero | megatron
- `--dataset-revision` — A1 revision pin
- `--checkpoint-every` — Checkpoint interval
- `--run-id` — Explicit run id

#### `exa pipeline distributed list`

List distributed training runs.

#### `exa pipeline distributed resume`

Resume from the last integrity-valid checkpoint (R4/GWT-3). Exit 1 if none valid.

#### `exa pipeline distributed status`

Show a distributed run + its checkpoints.

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
- `--force` — Override a failing C3 eval gate (audited, D4)

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
- `--dataset-revision` — Pin training to a recorded dataset revision (see `exa data list`)
- `--env` — YAML registry env overlay
- `--registry` — Path to model_registry.yaml
- `--cluster, -C` — Target an ACTIVE HPC cluster by name, or 'auto' to let placement choose
- `--gpus, -g` — GPUs to request (for --cluster auto placement)
- `--project, -p` — Scope the run to a Project (ADR 0088): tags the run and attributes its cost

### `exa pipeline validate`

Validate the pack's models/*.yaml against Python model shims.

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

### `exa policy bundle`

Signed, versioned policy bundles (D5)

#### `exa policy bundle list`

List signed policy bundle versions.

- `--tenant` — Filter by tenant

#### `exa policy bundle sign`

Version + sign the effective policy bundle for a tenant (R2).

- `--tenant` — Tenant scope

#### `exa policy bundle verify`

Verify a stored policy bundle's hash + signature (R2). Exit 1 if invalid.

- `--tenant` — Tenant scope
- `--version` — Specific version (default latest)

### `exa policy eval`

Evaluate a structured governance decision via the PolicyEngine (D5, R1/R5/GWT-5).

- `--action` — Action verb (promote/deploy/allocate/…)
- `--subject` — Who is acting
- `--resource` — What is acted on (e.g. JPCP/17)
- `--tenant` — Tenant scope
- `--set, -s` — Context key=value (repeatable)
- `--dry-run` — Explain without auditing (R5)

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

## `exa project`

ExaMLOps Projects (CPU/memory/storage/GPU quotas)

### `exa project access`

List RBAC relations (by subject and/or object).

- `--subject` — Show all grants for a subject
- `--object` — Show all grants on an object

### `exa project add-member`

Add a person to a project (owner ⊇ editor ⊇ viewer).

- `--role, -r` — owner | editor | viewer

### `exa project archive`

Archive a project (marks ARCHIVED; data is preserved).

- `--yes, -y` — Skip confirmation

### `exa project assign`

Assign any resource (model/pipeline/serving/connection/dataset/storage) to a project.

- `--kind, -k` — Resource kind: model, pipeline, serving_endpoint, connection, dataset, storage

### `exa project assign-model`

Assign a model to a project (alias for: exa project assign <p> <model> --kind model).

### `exa project budget`

Show budget/quota status and flag breaches (exit 1 if over budget).

### `exa project compose`

Generate a Docker Compose fragment with resource limits for this project.

The output enforces the project's CPU/memory quota across all its containers.
Merge it with your main docker-compose.yml or pass it to docker compose -f.

Resource limits follow Docker Compose v3 ``deploy.resources`` semantics:
- ``cpus``: fractional CPU cores (e.g. 2.0 = 2 cores)
- ``memory``: total RAM (e.g. 8589934592 bytes = 8 GB)

- `--out, -o` — Write to file instead of stdout

### `exa project cost`

Show per-project cost attribution (GPU-hours · USD · carbon).

### `exa project create`

Create a new project with resource quotas (CPU/memory/storage/GPU).

- `--description, -d` — Project description
- `--cpu-limit` — Total CPU cores for this project
- `--memory-gb` — Total RAM in GB for this project
- `--storage-gb` — Total storage in GB for this project
- `--gpu-limit` — Total GPU count for this project (0 = no GPUs)

### `exa project current`

Show the active project (EXAMLOPS_PROJECT env → config.toml → none).

### `exa project delete`

Delete a project and remove all its model assignments (irreversible).

- `--yes, -y` — Skip confirmation

### `exa project grant`

Grant a subject a relation on an object (RBAC, audited, spec D6).

### `exa project list`

List all projects with their resource quotas.

- `--status, -s` — Filter by status (ACTIVE|ARCHIVED)

### `exa project members`

List the people who have a role on a project.

### `exa project pipelines`

Show the project's two pipeline surfaces: Prefect (training) + Ray Serve (serving) (P7).

### `exa project remove-member`

Remove a person's role(s) from a project.

- `--role, -r` — Specific role, or all if omitted

### `exa project revoke`

Revoke a subject's relation on an object (audited).

### `exa project set-quota`

Update resource quotas for an existing project.

- `--cpu-limit` — New CPU limit (cores)
- `--memory-gb` — New RAM limit (GB)
- `--storage-gb` — New storage limit (GB)
- `--gpu-limit` — New GPU limit (count)
- `--description` — New description

### `exa project show`

Show the full project anatomy: quota, resources by kind, members, budget, consumption.

### `exa project storage`

Show (or bind/refresh) the project's MinIO storage location (P6).

- `--bind-connection` — Point storage at a P2 S3 connection (by name)
- `--refresh` — Re-probe used bytes from MinIO

### `exa project use`

Set the active project (persisted in config.toml; EXAMLOPS_PROJECT env overrides).

## `exa prompt`

Prompt registry — versioned templates + labels (dev/prod)

### `exa prompt create`

Create a new immutable prompt version (spec R1).

- `--template, -t` — Prompt template with {vars}
- `--label, -l` — Also point this label at the new version

### `exa prompt diff`

Show a line diff between two prompt versions (spec R3).

### `exa prompt label`

Move a label to a version — audited (spec R8/R9).

### `exa prompt list`

List prompt names, or the versions + labels of one prompt.

### `exa prompt rollback`

Roll a label back to a prior version without deleting history (spec R10).

### `exa prompt show`

Show a prompt version's template (by version or name@label).

## `exa providers`

Pluggable calculation providers (all domains)

### `exa providers activate`

Make a provider the active one for its (project, domain) — used when no --provider is given.

- `--project, -p`

### `exa providers author`

Save a project-scoped provider from a Python file (AST-sandboxed; audited).

- `--file, -f` — Python file defining a Provider subclass
- `--project, -p` — Project that owns the provider

### `exa providers authored`

List a project's notebook/dashboard-authored providers (with gate status).

- `--project, -p` — Project to list authored providers for

### `exa providers list`

List calculation providers across every domain (built-ins + entry-point plugins + config).

- `--domain, -d` — Only this domain (default: all known domains)

### `exa providers rm`

Delete an authored provider file (audited).

- `--project, -p`

### `exa providers show`

Print the stored source of an authored provider.

- `--project, -p`

### `exa providers validate`

Statically validate a provider file against the AST sandbox (exit 1 if rejected). CI-safe.

- `--file, -f` — Python file to gate-check (no side effects)

## `exa rag`

RAG — ingest knowledge bases and query with citations

### `exa rag ingest`

Chunk, embed, and index documents into a knowledge base.

- `--docs` — JSONL of {id, text}
- `--tenant` — Tenant namespace (D6)
- `--source-revision` — A1 dataset/source revision to version against

### `exa rag list`

List knowledge bases and their versions.

- `--tenant` — Filter to one tenant

### `exa rag query`

Answer a question from a knowledge base, citing retrieved chunks.

- `--question` — The question to answer
- `-k, --k` — Number of chunks to retrieve
- `--tenant` — Tenant namespace

## `exa report`

Offline cost/carbon/SLA reports

### `exa report generate`

Assemble + render a cost/carbon/project report. PDF degrades to HTML if WeasyPrint is absent.

- `--format, -f` — html | pdf | text
- `--out, -o` — Write to this file (else stdout)
- `--project` — Scope to one project

## `exa reproduce`

Reproducibility bundles — signed manifest + verify (A8)

### `exa reproduce build`

Capture + sign a reproducibility bundle for a model version (R1/R2).

- `--dataset` — Dataset name
- `--revision` — A1 dataset revision
- `--seed` — RNG seed to record
- `--hyperparams` — JSON hyperparameters
- `--metrics` — JSON recorded metrics
- `--image-digest` — Container image digest

### `exa reproduce list`

List reproducibility bundles.

### `exa reproduce run`

Rebuild plan + metric-match within tolerance — never claims bit-exactness (R3/GWT-2).

- `--observed` — JSON of re-observed metrics to match against recorded

### `exa reproduce verify`

Check referenced inputs still exist + hashes match (R5/GWT-4). Exit 1 if rotted.

## `exa retrain`

Trigger a Prefect training run via the Control Plane.

- `--dataset, -d` — Dataset class name
- `--dummy` — Use dummy data (dev-safe, no downloads)
- `--backend` — Storage backend
- `--dry-run` — Show what would be scheduled without triggering it
- `--reason` — Why you are making this change (recorded in the audit trail)

## `exa scaffold`

Scaffold a new model: model class, config, unit test, and YAML.

- `--task, -t` — Task type
- `--type, -T` — ML task type
- `--force` — Overwrite existing files

## `exa seanerbus`

SeanerBUS bridge UUID management

### `exa seanerbus init-uuids`

Assign a SeanerBUS UUID to every model that doesn't have one. Idempotent.

### `exa seanerbus list`

Show all models and their SeanerBUS UUIDs.

### `exa seanerbus regen-uuid`

Regenerate the SeanerBUS UUID for one model. Notify HPC teams of the change.

### `exa seanerbus status`

Probe the SeanerBUS bridge health and runtime stats endpoints.

## `exa secrets`

Secrets management, rotation, and leak scanning

### `exa secrets get`

Resolve a secret. Redacts by default; --reveal prints plaintext.

Also reports the backend that served it (vault/local/env), and warns when a configured
vault was unreachable — that fallback changes which store the value came from.

- `--tenant` — Tenant scope
- `--reveal` — Print the plaintext value (dangerous)

### `exa secrets list`

List secret metadata (paths/versions) — never values.

- `--tenant` — Filter by tenant

### `exa secrets rewrap`

Re-encrypt every local secret under the ACTIVE KEK (online key rotation, item 2.3).

Run after adding a new key to EXAMLOPS_SECRETS_KEYS and pointing EXAMLOPS_SECRETS_ACTIVE_KEY at
it: secrets migrate to the new key so the old one can be decommissioned. Plaintext never leaves
the process; the operation is audited.

- `--dry-run` — Report what would rewrap; change nothing

### `exa secrets rotate`

Rotate a secret to a fresh random value (audited, spec R4).

- `--tenant` — Tenant scope

### `exa secrets scan`

Scan a file/dir for likely secrets; exit non-zero on any finding (CI gate, R10).

### `exa secrets set`

Store an encrypted secret in the local store (audited).

- `--tenant` — Tenant scope

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

### `exa serve adapter`

Multi-LoRA adapters (add/list/promote/route) (B7)

#### `exa serve adapter add`

Register an adapter (alias of `exa finetune`) (R6).

- `--dataset` — A1 dataset revision
- `--method` — lora | qlora | full
- `--rank` — LoRA rank
- `--eval` — Eval score
- `--eval-floor` — C3 quality floor

#### `exa serve adapter list`

List registered adapters (R6).

- `--base` — Filter by base model ref

#### `exa serve adapter promote`

Promote an adapter — blocked by the C3 eval-gate if below floor (R2/GWT-2).

#### `exa serve adapter route`

Route a request through a base + adapter — refuses a base mismatch (R4/GWT-4).

- `--prompt` — Prompt text
- `--hot-set` — Hot-set size (LRU)

### `exa serve autoscale`

Autoscaling & scale-to-zero (E5)

#### `exa serve autoscale record`

Record an executed scale event (audited D4).

- `--reason` — Why the scale happened
- `--cold-start` — Measured cold-start seconds
- `--tenant` — Tenant scope

#### `exa serve autoscale savings`

Estimate FinOps savings from scale-to-zero (R7).

- `--gpu-cost` — GPU cost per hour

#### `exa serve autoscale set`

Declare a per-model autoscale policy (R1).

- `--min` — Minimum replicas (0 enables scale-to-zero floor)
- `--max` — Maximum replicas
- `--metric` — rps|queue_depth|gpu_util|p95
- `--target` — Target value for the metric
- `--scale-to-zero-after` — Idle seconds before scaling to zero (0 disables)
- `--warm-pool` — Warm replicas to keep (avoid cold start)
- `--gpu-fraction` — E3 GPU fraction per replica
- `--tenant` — Tenant scope

#### `exa serve autoscale simulate`

Compute the scaling decision for a given state (pure, anti-thrash aware) (R2/R3).

- `--replicas` — Current replica count
- `--observed` — Observed metric value
- `--idle` — Idle seconds (for scale-to-zero)
- `--since-last` — Seconds since last scale

#### `exa serve autoscale status`

Show the autoscale policy + recent scale events + cold-start time.

### `exa serve backend`

Show the active serving backend (ray-compose default | kserve-k8s).

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

### `exa serve challenger`

Champion-challenger scoreboard & promotion

#### `exa serve challenger disable`

Disable the challenger for a model.

#### `exa serve challenger enable`

Enable a challenger and declare its promotion policy (R1/R5).

- `--version` — Challenger MLflow version
- `--mirror` — Percent of traffic to mirror (0-100)
- `--min-delta` — Min error reduction to win
- `--alpha` — Significance level
- `--min-samples` — Min labelled samples to decide
- `--auto-promote` — Promote automatically on win
- `--tenant` — Tenant scope (D6)

#### `exa serve challenger list`

List configured challengers.

- `--tenant` — Filter to one tenant

#### `exa serve challenger promote`

Propose promotion via C3 if the policy is met and no SLO regression (R5/R6).

- `--tenant` — Tenant scope

#### `exa serve challenger status`

Show the champion-challenger scoreboard: delta, p-value, N, SLO (R4).

- `--tenant` — Tenant scope

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

### `exa serve llm`

LLM/VLM endpoints — vLLM lifecycle (Track V)

#### `exa serve llm args`

Print the exact ``vllm serve`` argv this model's engine block renders.

Same renderer the Compose service, the Slurm template and the KServe manifest use, so
what is printed here is what actually runs on every substrate.

#### `exa serve llm bench`

Measure TTFT and output tokens/s against a live endpoint.

- `--requests, -n` — Sequential requests to send
- `--prompt`
- `--max-tokens`

#### `exa serve llm chat`

Send a chat request — with images, this is the VLM smoke test.

- `--message, -m` — The prompt
- `--image` — Image path or URL (repeatable) — the VLM path
- `--stream` — Stream token deltas as they arrive
- `--max-tokens` — Cap the completion length
- `--temperature` — Sampling temperature

#### `exa serve llm health`

Probe the endpoint and update its recorded state. Exits 1 when not ready (CI gate).

#### `exa serve llm list`

List registered LLM/VLM endpoints.

- `--project` — Filter by project workspace
- `--state` — Filter by lifecycle state

#### `exa serve llm start`

Start (or register) a vLLM endpoint and record it in the endpoint registry.

- `--hf-model` — Weights to serve (HF id or local path)
- `--launcher, -l` — external | compose | slurm | flux | kserve
- `--base-url` — External endpoint URL
- `--modality` — text | vision | audio | video
- `--max-images` — limit_mm_per_prompt.image (required for a vision model)
- `--media-domains` — Comma-separated allow-list for remote media (SSRF guard)
- `--local-media-path` — Directory from which file:// media may be read
- `--tp` — tensor_parallel_size (GPUs per node)
- `--pp` — pipeline_parallel_size (usually = nodes)
- `--dtype` — auto | float16 | bfloat16 | fp8 | …
- `--max-model-len` — Context length
- `--nodes` — HPC nodes to allocate
- `--gpus` — GPUs per node
- `--partition` — HPC partition/queue
- `--walltime` — HPC walltime
- `--port` — Port the server listens on
- `--project` — Attribute to a project workspace
- `--dry-run` — Preview; change nothing
- `--reason` — Why you are making this change (recorded in the audit trail)

#### `exa serve llm status`

Show one endpoint: registry record, substrate status, and live vLLM metrics.

#### `exa serve llm stop`

Stop an endpoint (and deregister it).

- `--dry-run` — Preview; change nothing
- `--reason` — Why you are making this change (recorded in the audit trail)

### `exa serve manifest`

Generate a schema-valid KServe InferenceService manifest from the model registry (E1).

- `--alias` — MLflow alias to serve
- `--canary` — Canary traffic percent (0..100)
- `--out` — Write manifest YAML to this file
- `--registry-dir` — Dir of per-model YAML (default: RAY_MODELS_DIR)

### `exa serve models`

List models currently hot-loaded in Ray Serve.

- `--detail, -d` — Show full model detail

### `exa serve reload`

Hot-reload Production models from MLflow into Ray Serve.

- `--model, -m` — Reload one model (default: all)

### `exa serve routing`

KV/prefix-cache-aware inference routing (E4)

#### `exa serve routing set`

Configure a model's inference routing (R3 default round-robin; cache-aware opt-in).

- `--mode` — round_robin | cache_aware
- `--slo-latency-ms` — Avoid replicas over this
- `--disaggregate` — Split prefill/decode pools
- `--prefill-pool` — Prefill pool name
- `--decode-pool` — Decode pool name
- `--tenant` — Tenant scope

#### `exa serve routing simulate`

Simulate a shared-prefix request stream and report the cache-aware vs round-robin hit rate.

- `--replicas` — Replica count
- `--shared-prefix-requests` — Requests sharing one prefix
- `--mode` — round_robin | cache_aware

#### `exa serve routing stats`

Show recorded prefix-cache hit rate + routing-decision breakdown.

- `--tenant` — Filter by tenant

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
- `--reason` — Why you are making this change (recorded in the audit trail)

### `exa serve traffic-list`

Show traffic split configuration for all models.

- `--watch, -w` — Live auto-refreshing view (Ctrl-C to exit)
- `--interval` — Refresh interval in seconds for --watch

## `exa slo`

Model-quality SLOs — error budgets & burn-rate alerts

### `exa slo apply`

Apply all SLO specs from a YAML file (R1).

### `exa slo burn`

Show which SLOs are burning budget (and would page) (R3).

- `--tenant` — Tenant scope

### `exa slo generate`

Generate promtool-valid Prometheus recording + burn-rate rules (R2/R3).

- `--tenant` — Tenant scope
- `--out` — Write rules YAML to this file

### `exa slo list`

List declared SLO specs.

- `--model` — Filter to one model
- `--tenant` — Filter to one tenant

### `exa slo record`

Record one SLI measurement interval (R4) — feeds budget + burn rate.

- `--tenant` — Tenant scope

### `exa slo set`

Declare or version-bump one SLO spec (R1).

- `--target` — Objective ratio 0..1
- `--window` — Rolling window (e.g. 30d)
- `--source` — c1|c2|c5|availability|prometheus
- `--query` — PromQL SLI expression (good ratio)
- `--tenant` — Tenant scope (D6)
- `--gate` — Gate promotion when budget exhausted (C3)

### `exa slo status`

Show SLI, remaining error budget, and burn rate per SLO (R5).

- `--name` — One SLO (default: all for the model)
- `--tenant` — Tenant scope

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

## `exa vector`

Vector store — collections, upsert, search, reindex

### `exa vector create`

Create a vector collection with a fixed dim + distance metric.

- `--dim` — Fixed dimensionality
- `--metric` — cosine | l2 | dot
- `--tenant` — Tenant namespace (D6)

### `exa vector reindex`

Rebuild the collection index (blue-green; recall preserved) — invoked by B6.

- `--tenant` — Tenant namespace

### `exa vector search`

Search top-k nearest by the collection metric, with optional metadata filter.

- `--vector` — JSON array of floats (query)
- `-k, --k` — Top-k results
- `--filter` — JSON metadata equality filter
- `--tenant` — Tenant namespace

### `exa vector stats`

Show collection dim, metric, and item count.

- `--tenant` — Tenant namespace

### `exa vector upsert`

Upsert a single vector (rejected if dim mismatches the collection).

- `--id` — Item id
- `--vector` — JSON array of floats
- `--meta` — JSON metadata object
- `--tenant` — Tenant namespace

## `exa workbench`

Project Workbenches — on-demand dev environments (P5)

### `exa workbench create`

Define a workbench in a project (status STOPPED until started).

- `--project, -p` — Owning project (required)
- `--image` — Container image
- `--cpu` — CPU cores
- `--memory-gb` — RAM in GB

### `exa workbench delete`

Delete a workbench definition.

- `--project, -p` — Owning project
- `--yes, -y` — Skip confirmation

### `exa workbench list`

List workbenches.

- `--project, -p` — Filter by project

### `exa workbench start`

Start a workbench — marks it RUNNING and prints its launch spec (image, volume, injected env).

- `--project, -p` — Owning project

### `exa workbench stop`

Stop a workbench (marks STOPPED).

- `--project, -p` — Owning project
