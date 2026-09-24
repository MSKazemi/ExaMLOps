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

### `exa admission reservations`

List two-phase quota reservations, or preview which leaked ones would expire. Read-only.

- `--state` — reserved | committed | released | expired
- `--project` — Only this project
- `--expire-preview` — List reservations whose TTL lapsed (nothing is changed)
- `--limit` — Most recent N

### `exa admission simulate`

Show what the admission seam would decide for a job request. Read-only: nothing is queued,
reserved or executed, and no audit event is written.

- `--request` — JobRequest JSON file to evaluate
- `--policy` — fair-share (default) | baseline-over-quota; else $EXAMLOPS_ADMISSION_POLICY
- `--cluster-state` — JSON file overriding the live state (what-if): total_gpus, free_gpus, gpus_in_use_by_tenant, running_by_tenant, largest_free_domain_gpus

### `exa admission stats`

Show queue depth by state (queued/running/done/rejected/failed).

### `exa admission submit`

Enqueue a work item (durable). A worker claims it under the global + per-tenant caps.

**This enqueues; it does not dispatch.** `examlops.admission` is a facade whose `dispatch` is
injected by whatever embeds it, and the control plane runs its own admission accounting on this
table rather than through the facade — so an item submitted here waits until something claims
it. `exa admission stats` reports how long the oldest queued item has been waiting, which is
what tells a busy queue from a stranded one.

- `--payload, -p` — JSON payload
- `--tenant` — Tenant for fair-share accounting
- `--project` — Project attribution
- `--priority` — Higher runs first within a tenant

## `exa agent`

Skipper agent — health, backend and memory

### `exa agent alias`

Agent aliases - Staging / Canary / Production pointers, promotion gated by evidence

#### `exa agent alias rollback`

Move an alias back to the version it held before its latest move (not re-gated).

- `--reason` — Why (recorded in the history)

#### `exa agent alias set`

Point an alias at a version. Production requires recorded evaluation evidence.

- `--reason` — Why (recorded in the history)

#### `exa agent alias show`

Show where an agent's aliases point, and (for one alias) its recent moves.

### `exa agent memory`

Govern authenticated, owner-scoped agent memory (ADR 0034)

#### `exa agent memory delete`

Erase memories, cascading to derived ones. Audited to ``audit_events``.

The immutable audit log is a separate store and is deliberately *not* erased — ADR 0034
keeps the record that an erasure happened while removing what was remembered.

- `--scope` — Limit erasure to one scope (e.g. an operator)
- `--operator` — Local-mode audit actor (remote mode uses verified principal)
- `--local` — Erase AGENT_MEMORY_DB on this machine

#### `exa agent memory export`

Export authenticated owner-scoped memory as JSON.

- `--out` — Write JSON here instead of stdout
- `--local` — Read AGENT_MEMORY_DB on this machine

#### `exa agent memory list`

Enumerate owner-scoped memories of one kind.

- `--scope` — Task-class / model / operator scope
- `--limit` — Maximum items to show
- `--local` — Read AGENT_MEMORY_DB on this machine

#### `exa agent memory review`

List, approve, or reject queued procedure memories

##### `exa agent memory review approve`

Approve one queued procedure memory.

- `--local` — Update the local review database

##### `exa agent memory review list`

List pending procedure reviews for the authenticated owner.

- `--local` — Read the local review database

##### `exa agent memory review reject`

Reject one queued procedure memory.

- `--reason` — Reason recorded with the rejection
- `--local` — Update the local review database

#### `exa agent memory stats`

Summarise memory owned by the authenticated principal and tenant.

- `--local` — Read AGENT_MEMORY_DB on this machine

### `exa agent status`

Show the agent's reachability, LLM backend, model and memory tier.

Exits non-zero when the agent is unreachable *or* when it is up but its backend is
unusable. Both mean "do not trust an answer from this agent", which is the question a
script is really asking, and collapsing them into one exit code is what makes this
usable as a health gate.

### `exa agent version`

Agent versions - immutable, content-addressed manifests (ADR 0146)

#### `exa agent version card`

Print the A2A-shaped Agent Card of a registered version (read-only, content-addressed).

- `--out` — Write the card JSON to this file

#### `exa agent version diff`

Which components differ between two versions, by name.

#### `exa agent version list`

List registered versions, newest first, with the aliases pointing at each.

- `--agent, -a` — Only this agent
- `--limit, -n` — Max versions

#### `exa agent version register`

Validate a manifest and register it; identical content returns the existing version.

#### `exa agent version show`

Show one version: the pinned tuple and whether its signature verifies.

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

Retract a pending approval by its UUID (a stale or duplicate entry).

The approval is kept in the history, marked `retracted` with who and when; nothing is erased.

### `exa approvals list`

List model change approvals.

- `--all` — Show all statuses, not just pending

### `exa approvals reject`

Reject a pending model change — no training will run.

- `--reason, -r` — Rejection reason
- `--dry-run` — Show what would be rejected without changing anything

## `exa ask`

Ask the Skipper agent a question in natural language.

- `--session, -s` — Session id to preserve conversational context (default: isolated one-shot)
- `--stream` — Print the answer as it is generated (default: on at a terminal, off when piped)
- `--approve` — Approve one pending action in this session
- `--deny` — Deny one pending action in this session

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

`--orchestrator scheduler` runs the build as a job on the phase-23 HPC scheduler (ADR 0036
clause 3) — mock, Slurm or Flux — and waits for it, instead of running it in this process;
`--orchestrator prefect` runs it as a Prefect flow run, visible in the Prefect UI (clause 1).

- `--force` — Rebuild even if fresh
- `--no-deps` — Build only this asset, never its stale ancestors
- `--orchestrator` — local | scheduler | prefect — overrides EXAMLOPS_ASSET_ORCHESTRATOR for this run

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

### `exa audit anchor`

Anchor the high-volume telemetry side tables into the chain (ADR 0110 decision 2).

Cron-able; the autopilot also anchors at the end of each live cycle. Each anchor names its
own row range, so the cadence is recorded in the chain itself.

### `exa audit autonomy`

Every autonomous action in the window, and whether it declared an inverse.

This is the W2 gate as a command: for each action the platform took on its own initiative,
who acted, on whose behalf, under which mode, and how it would be undone. An action with no
``rollback_ref`` is listed rather than filtered out — ADR 0110 decision 4 calls that a policy
violation, and hiding them would defeat the point of asking.

- `--last` — Time window (e.g. 7d, 30d)
- `--limit` — How many actions to list (the counts always cover the whole window)

### `exa audit chain`

Reconstruct one unit of work and everything it caused (ADR 0110).

A hash chain records *events*; this reads the causal edges between them, so an
orchestrator's id returns the tool calls it caused and whatever those caused in turn. That
reconstruction is what makes "who did this, on whose behalf, and how would it be undone"
answerable from the evidence chain alone.

### `exa audit checkpoint`

Sign the current chain head and anchor it to the WORM store (D4·R5).

This is also the periodic-export hook: run it from cron (``exa audit checkpoint --anchor
--skip-unchanged``) or call :func:`examlops.audit_worm.checkpoint_and_anchor` from a scheduler.

- `--anchor` — Require the WORM anchor: exit 1 unless the checkpoint was durably anchored (cron-friendly; an S3 failure that degraded to the local fallback counts as a failure)
- `--skip-unchanged` — Do nothing when the head already has an anchored checkpoint (cheap for cron)

### `exa audit checkpoints`

List signed audit checkpoints.

- `--limit, -n` — Max checkpoints to show

### `exa audit export`

Archival export of the audit trail (D4·R4). Append-only — never deletes.

- `--out` — Write the archival JSON export to this file
- `--before` — Only events before this ISO timestamp

### `exa audit prune`

Prune old audit events under the retention policy WITHOUT breaking the chain (ADR 0028).

Dry run by default. Refused unless EXAMLOPS_AUDIT_RETENTION_DAYS is set, the chain verifies,
a fresh signed checkpoint is anchored to the WORM store, and the deleted rows are archived.
A signed prune record keeps ``exa audit verify`` passing over what remains.

- `--before` — Prune events older than this date (YYYY-MM-DD or ISO-8601); never newer than the retention floor (EXAMLOPS_AUDIT_RETENTION_DAYS)
- `--execute` — Actually delete (default is a dry run that changes nothing)
- `--archive` — File to write the pruned rows to (required with --execute)
- `--allow-unanchored` — Prune even though no WORM anchor is configured (the cut is then not off-platform)
- `--yes, -y` — Skip the confirmation prompt

### `exa audit review`

Perform and RECORD a sampled audit review (ADR 0113 decision 5).

An unreviewed audit trail is theatre: this samples events written since the last recorded
review (all of them, if fewer than the sample size), shows them, and writes an
``audit_reviewed`` event naming the reviewer, the covered range and the sampled ids —
so "is anyone actually looking?" is answerable from the chain. Schedule it (cron /
`exa backup schedule`-style) at whatever cadence your governance names.

- `--sample` — Events to sample for review
- `--notes` — Reviewer notes, recorded with the review

### `exa audit reviews`

List recorded audit reviews — who reviewed, when, covering what.

- `--last` — How many recorded reviews to show

### `exa audit verify`

Recompute the hash chain and report integrity (D4·R2/R6). Exit 1 if broken.

### `exa audit verify-anchors`

Verify every telemetry anchor against its side table (ADR 0110 decision 5). Exit 1 on a break.

### `exa audit verify-worm`

Verify the external WORM anchor: its own chain + agreement with the DB checkpoints (item 2.4).

## `exa auth`

Sign in with your data center's identity provider; inspect federation & authorization

### `exa auth accounts`

The federated account directory: who signed in or was provisioned, and who was removed.

- `--provider, -p` — Only this center's accounts
- `--inactive` — Only deactivated accounts
- `--limit` — Maximum rows

### `exa auth activate`

Re-activate a deactivated (or deleted) federated account.

- `--provider, -p` — The center the account belongs to

### `exa auth deactivate`

Deactivate a federated account now — refused everywhere within seconds, even with a valid token.

- `--provider, -p` — The center the account belongs to
- `--reason` — Recorded in the audit trail

### `exa auth decide`

Ask the platform's authorizer — tenant, local policy, the center's PDP — about an action.

- `--resource-type` — Resource type
- `--resource-id` — Resource id
- `--tenant` — Resource tenant (default: yours)
- `--project` — Resource project
- `--token-file` — File holding the token ('-' = stdin; default: your session)

### `exa auth login`

Sign in with your organisation (Device Authorization Grant, RFC 8628).

- `--provider, -p` — A center named in the platform's trust configuration
- `--issuer` — OIDC issuer URL (instead of --provider)
- `--client-id` — Public OAuth client id of the CLI
- `--oidc-agent` — Delegate to an oidc-agent account (tokens never stored here)

### `exa auth logout`

Forget this context's session (and revoke its refresh token where the IdP supports it).

### `exa auth providers`

List the identity providers (data centers) this platform trusts.

### `exa auth status`

Show whether this config context is signed in, to which IdP, and until when.

### `exa auth token`

Print a current access token (refreshed if needed) for scripts and curl.

- `--header` — Print as an Authorization header

### `exa auth validate`

Validate a trust file; exit 1 on any error (a CI gate for identity config).

- `--file, -f` — Trust file path (default: $EXAMLOPS_IAM_CONFIG)
- `--check-discovery` — Also fetch each issuer's discovery document

### `exa auth verify`

Verify a token against the trust file and show the principal it maps to.

- `--token-file` — File holding the token ('-' = stdin; default: your session)
- `--provider` — Provider for an opaque token

### `exa auth whoami`

Who the platform sees: your verified principal (role, tenant, groups) when it can check.

## `exa autopilot`

Self-driving MLOps closed loop (detect→retrain→promote, policy-governed)

### `exa autopilot autonomy`

Set one behaviour's autonomy level (per rule, pausable, acknowledgment recorded).

- `--ack` — Required when granting AUTONOMOUS: your recorded acknowledgment

### `exa autopilot contract`

Show a behaviour's blast-radius contract verbatim (ADR 0113).

### `exa autopilot disable`

Disable the autopilot kill-switch (persistent, stored in platform.db).

### `exa autopilot enable`

Enable the autopilot kill-switch (persistent, stored in platform.db).

### `exa autopilot follow`

Run each retrained model's promotion step as soon as its training run completes.

A long-running consumer of ``retrain.run_completed`` on the NATS event backbone (durable name
``autopilot``: several copies share the work). Stop with Ctrl-C or SIGTERM.

- `--wait` — Seconds a fetch waits for new events

### `exa autopilot interrupt`

Freeze or kill ONE in-flight autopilot run (ADR 0113 decision 4; audited).

- `--kill` — Abort the run at its next checkpoint
- `--freeze` — Pause the run until resumed
- `--reason` — Why (recorded in the audit event)

### `exa autopilot quarantine`

Quarantine a model: the autopilot skips it until released (audited).

- `--reason` — Why (recorded and shown on skips)

### `exa autopilot release`

Release a quarantined model back to autonomous eligibility (audited).

### `exa autopilot resume`

Release a frozen run so it continues from its checkpoint.

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

## `exa broker`

Agent tool broker - which agent may call which tool (ADR 0145)

### `exa broker grant`

Tool grants - per-subject allow/deny with constraints

#### `exa broker grant list`

List grants, grouped by subject.

- `--subject, -s` — Only this subject

#### `exa broker grant remove`

Remove a grant (or all of a subject's). No grants left means default allow again.

#### `exa broker grant set`

Create or replace one grant. Validated before it is stored; audited; policy-gated.

- `--effect` — allow | deny
- `--tier-ceiling` — read | A | B | C
- `--needs-approval` — Human approval per call
- `--max-per-minute`
- `--max-per-session`
- `--arg-schema-json` — JSON-Schema subset the call arguments must satisfy
- `--credential` — PARAM=SECRET_NAME injected at call time (repeatable)
- `--egress-url-arg` — URL-valued argument
- `--egress-host` — Allowed host (or *.suffix)
- `--from-file` — Grant document (JSON/YAML)

#### `exa broker grant show`

Show every grant of one subject in full (credential paths, arg schema, egress).

### `exa broker simulate`

What the broker would decide for one call - runs nothing, counts no quota.

- `--agent` — Agent name
- `--tool` — Tool name
- `--args-json` — Call arguments as a JSON object
- `--version-id` — Agent version id
- `--subject` — Workload identity subject
- `--session` — Session id

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

### `exa cards export`

Export a card for publication with PII, locations and internal fields scrubbed (clause 4).

Internal fields are dropped, PII and site-specific locations are redacted, and a detected
**secret blocks the export** — redacting it would hide that a credential reached a generated
artifact at all. `--force` overrides that, audited, because the scanner is a regex heuristic
and can be wrong; the dropped and redacted parts are not overridable, because they are not
judgement calls.

- `--dataset` — Export a dataset (Croissant) card
- `--out` — Write the publishable card to this file
- `--force` — Publish despite a secret finding (audited)
- `--tenant` — Tenant scope

### `exa cards lint`

Lint a dataset card (datasheet) for missing required fields; exit 1 on any finding (CI).

- `--revision` — Dataset revision (A1); required
- `--license` — Dataset license

### `exa cards model`

Build a structured model card from live data — gaps as 'not provided' (R3/R4).

- `--tenant` — Tenant scope (D6)
- `--out` — Write the card Markdown to this file
- `--save` — Persist a versioned card

## `exa catalog`

Model Catalog — curated model definitions you can start from (ADR 0158)

### `exa catalog list`

Browse the catalog — curated model definitions you could start from.

- `--kind` — base_model or recipe
- `--license` — SPDX identifier, e.g. mit
- `--trust-tier` — T1_signed or T1_unsigned — never hidden behind a generic OK
- `--evaluated-only` — only entries that point at an eval summary
- `--all-versions` — every catalog_version, not just the newest of each entry

### `exa catalog publish`

Publish a catalog entry. An unpinned source is refused here, never flagged later.

- `--sign` — sign the entry with examlops.supplychain (the only signer)

### `exa catalog pull`

Materialize a catalog entry into a project. Trains nothing, serves nothing.

- `--project` — Project to pull the entry into
- `--as` — Model name to materialize as (default: the entry name)
- `--dry-run` — preview the rendered YAML and the lineage edge; write nothing
- `--yes, -y` — Skip confirmation

### `exa catalog show`

Show one catalog entry (default: its newest catalog_version).

## `exa chat`

Interactive conversation with the Skipper agent

- `--session, -s` — Server-side conversation ID to create or resume
- `--stream` — Stream answer tokens as they arrive

## `exa commands`

Follow asynchronous control-plane commands (/v1)

### `exa commands cancel`

Cancel a command that has not been dispatched yet (pending or awaiting retry).

### `exa commands list`

List asynchronous commands in your tenant, newest first.

- `--state` — pending | dispatching | failed | succeeded | dead | cancelled
- `--limit` — Page size
- `--cursor` — Continue from a previous page

### `exa commands show`

Show one command: its state, attempts, result (the flow run) or last error.

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

### `exa compliance declaration`

Generate the Annex-V EU Declaration of Conformity (ADR 0012 clause 4).

Fields the platform can know are read from live metadata; the ones only the provider can
state are yours to supply. Anything missing is left as an explicit placeholder and the
document is stamped DRAFT with its reasons — never quietly filled in.

- `--issued-at` — Place and date of issue, e.g. 'Julich, 2026-09-02' (Annex V(8))
- `--provider` — Provider legal name (Annex V(2))
- `--provider-address` — Provider address (Annex V(2))
- `--signatory` — Name of the signatory (Annex V(8))
- `--signatory-function` — Function of the signatory (Annex V(8))
- `--standard` — Harmonised standard or common specification (repeatable)
- `--notified-body` — Notified body name + identification number (Annex V(7))
- `--personal-data` — Whether the system processes personal data (Annex V(5))
- `--out` — Write the declaration Markdown to this file
- `--tenant` — Tenant scope

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

### `exa config delete-context`

Delete a named context and all its values (clears it if it was active).

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

### `exa config unset`

Remove a config value so the next source applies (context → base → default).

- `--context, -c` — Remove it from a named context instead of the base config

### `exa config use`

Switch the active context (environment), or return to the base config with --clear.

- `--clear` — Leave any context and use the base configuration

## `exa connection`

Named Connections — reusable data sources (P2)

### `exa connection create`

Create a named connection.

- `--kind, -k` — Connection kind: s3, uri, dataplane, or any dataplane connector kind (see: exa dataplane connectors)
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

Pandera is the engine when installed, and a row-level failure then names the rows that failed;
without it the same checks run on pandas alone and reach the same verdict. The output says
which engine judged. EXAMLOPS_CONTRACT_ENGINE=python forces the pandas-only engine.

- `--path, -p` — Local parquet file/dir to validate
- `--revision` — A1 revision id for provenance

## `exa dataplane`

Dataplane — pull remote data into versioned snapshots (ADR 0130)

### `exa dataplane catalog-rebuild`

Rebuild the pull history and revision index from the snapshot store (e.g. a lost platform.db).

- `--dry-run` — Count only

### `exa dataplane connectors`

List connector kinds, whether their dependencies are installed, and plugin load errors.

### `exa dataplane manifest`

Show one snapshot's manifest (tables, files, schema, watermark).

- `--project, -p`

### `exa dataplane preview`

Show the first rows a pull would read; nothing is stored.

- `--project, -p`
- `--limit, -n`

### `exa dataplane prune`

Delete old snapshots; the newest N, the latest and any revision an MLflow run used are kept.

- `--keep`
- `--project, -p`
- `--dry-run` — List what would be removed
- `--force` — Prune even though the catalog has no revision rows for the source (a lost or restored platform.db) — revisions training runs used are then NOT protected

### `exa dataplane pull`

Pull a source now and commit a snapshot (or report it unchanged).

- `--project, -p`
- `--full` — Ignore the watermark; re-read everything
- `--remote` — Ask the dataplane service to run it
- `--dry-run` — Show what would be pulled

### `exa dataplane pulls`

Recent pulls, newest first.

- `--source, -s`
- `--project, -p`
- `--limit, -n`

### `exa dataplane snapshots`

Committed snapshots of a source, newest first.

- `--project, -p`

### `exa dataplane sources`

Register, inspect and remove dataplane sources.

#### `exa dataplane sources apply`

Register every source in a YAML file (GitOps). Credentials are refused.

- `--file, -f` — YAML file with a sources: list
- `--dry-run` — Validate every entry; register nothing

#### `exa dataplane sources create`

Register or update a source.

- `--connector, -k` — Connector kind (see: exa dataplane connectors)
- `--connection` — Named Connection holding the credentials
- `--spec-json` — Inline JSON source spec (what to read)
- `--schedule` — Refresh interval: 15m, 6h, 1d, @daily
- `--max-rows` — Refuse pulls larger than this
- `--max-bytes` — Refuse pulls larger than this many bytes
- `--contract` — Data contract checked before commit
- `--project, -p`
- `--dry-run` — Validate and show; register nothing

#### `exa dataplane sources delete`

Remove a source definition. Snapshots stay in the store until pruned.

- `--project, -p`
- `--dry-run` — Show what would be removed

#### `exa dataplane sources list`

List registered sources.

- `--project, -p` — Only this project

#### `exa dataplane sources show`

Show one source definition (never any credential).

- `--project, -p`

### `exa dataplane test`

Check that a source's system is reachable and the credentials work (reads no data).

- `--project, -p`

## `exa dataplane-bus`

Dataplane bus bridge UUID management

### `exa dataplane-bus init-uuids`

Assign a Dataplane bus UUID to every model that doesn't have one. Idempotent.

### `exa dataplane-bus list`

Show all models and their Dataplane bus UUIDs.

### `exa dataplane-bus regen-uuid`

Regenerate the Dataplane bus UUID for one model. Notify HPC teams of the change.

### `exa dataplane-bus status`

Probe the Dataplane bus bridge health and runtime stats endpoints.

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
- `--detector` — builtin (default) | river-adwin (needs `pip install river`; falls back to builtin)

### `exa drift consume-telemetry`

Write drift/input-embedding snapshots published by a bridge running with
EXAMLOPS_TELEMETRY_VIA_EVENTBUS=1 (ADR 0123 decision 4).

A long-running consumer of ``serving.inference_telemetry`` on the NATS event backbone
(durable name ``drift-telemetry``: several copies share the work). Run this wherever
platform.db is reachable — the serving plane no longer needs to be. Stop with Ctrl-C or
SIGTERM. Without a bridge publishing this way, there is nothing to consume; the direct-write
path (the default) needs no consumer at all.

- `--wait` — Seconds a fetch waits for new events

### `exa drift corruption`

#### `exa drift corruption baseline`

Store the current zero-rate and spread as this model's corruption baseline.

Zero rates drift legitimately (a genuinely sparser input distribution), so like
``exa drift baseline`` this is an explicit, audited act rather than a rolling window.

- `--reason` — Why you are making this change (recorded in the audit trail)

#### `exa drift corruption classify`

Name the anomaly — data drift, hardware, regression, or undetermined.

The remediation follows from the class, never from the z-score (ADR 0114 decision 2).

#### `exa drift corruption selftest`

Measure this detector against injected corruption and publish the rate (R-ef).

A detector may not be credited with classes it was not tested against, so this injects
each class into the model's own recent predictions and reports what was caught. A class
the detector does not gate on is expected to score ~0 — printing that is the point.

- `--rate` — Fraction of values to corrupt per trial
- `--trials` — Injection trials per corruption class

#### `exa drift corruption status`

Show the corruption signal per model — NaN/Inf **and** unexpected zeros.

A NaN/Inf guard alone sees about 1% of silent data corruption, so it is never reported
on its own here (ADR 0114 decision 1).

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

- `--similarity` — Nearest-neighbour drift of the sampled embedding vectors against the baseline snapshot (needs MODEL, sampling on, and `exa drift input baseline`)
- `--neighbours, -k` — With --similarity: nearest/farthest samples to list
- `--min-similarity` — With --similarity: mean cosine similarity of recent samples below which the input is flagged as drifted

### `exa drift profile`

Profile recent inference inputs: schema / nulls / ranges / cardinality (C5·R5).

- `--last-n` — Recent predictions to profile
- `--bad-payloads` — A5 bad-payload count to fold in

### `exa drift reset`

Clear all drift snapshots for a model (keeps baseline).

- `--dry-run` — Show how many snapshots would be cleared without deleting them
- `--reason` — Why you are making this change (recorded in the audit trail)

### `exa drift run-advanced`

Sweep every model with the concept, label-free and data-quality detectors (C5, ADR 0022).

Writes `drift_events` with a `drift_kind`, which `exa drift trigger` and the autopilot already
consume. A real run needs `EXAMLOPS_DRIFT_ADVANCED_ENABLED=1` (default off), takes a
distributed lease so only one scheduler acts, and re-states an unchanged non-OK event at most
once per `EXAMLOPS_DRIFT_ADVANCED_COOLDOWN` seconds (default 3600). Every cycle is audited.

- `--once` — Run one sweep and exit (default: loop)
- `--dry-run` — Preview: run the detectors, write nothing, take no lease
- `--model` — Only this model (default: every model)
- `--interval` — Seconds between sweeps (0 = env/300)
- `--window` — Concept-drift recent window (samples)

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

List registered encoders — from the registry of record (local, or MLflow).

### `exa embedding migrate`

Publish every locally registered encoder to the MLflow encoder registry (ADR 0043 cl. 1).

Additive and idempotent: encoder ids are content-addressed, so one already in MLflow is
skipped as the same record. Works before switching `EXAMLOPS_ENCODER_REGISTRY=mlflow`, so a
deployment can publish first and switch after. Needs `MLFLOW_TRACKING_URI`.

- `--dry-run` — List what would be published

### `exa embedding register`

Register a versioned encoder → encoder_id (R1).

With `EXAMLOPS_ENCODER_REGISTRY=mlflow` the encoder is published to the MLflow encoder
registry first (ADR 0043 clause 1); re-registering a local-only encoder publishes it.

- `--dim` — Embedding dimension
- `--metric` — cosine | dot | l2
- `--norm` — Normalization (l2/none)

### `exa embedding reindex`

Blue-green reindex to a new encoder — verified switch, old retained then pruned (R4/R5).

Large corpora belong on the scheduler (ADR 0043 clause 4): `--scheduler` runs the reindex as
a job (mock / Slurm / Flux) carrying `--recall` / `--recall-floor`, and returns once it is
queued; `exa embedding status` follows the same reindex row to its outcome. `--inline` forces
the local path.

- `--tenant` — Tenant scope
- `--corpus-size` — Docs to re-embed
- `--recall` — Measured recall of the new index
- `--recall-floor` — Minimum recall to switch
- `--inline` — Run here even if EXAMLOPS_REINDEX_ORCHESTRATOR=scheduler
- `--scheduler` — Submit to the HPC scheduler instead of running here

### `exa embedding set-encoder`

Bootstrap a collection's active encoder (R2).

- `--tenant` — Tenant scope

### `exa embedding status`

Show a collection's active/staging encoder + reindex history.

A reindex still `submitted` whose scheduler job has ended without settling it is marked
`failed` first (and audited), so a dead job never reads as queued. Only a clear terminal
answer from the scheduler settles a row; anything else leaves it as it is.

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

### `exa eval cli-coverage`

Ask the agent about commands sampled from the whole CLI surface and report the rate.

`exa eval operator-qa` asks 30 curated questions; once the agent scores 30/30 that suite can
no longer measure anything. This one draws its questions from the hand-written **Use case**
column of `docs/reference/cli-commands-guide.md`, which covers every command, so the number
says something about the CLI rather than about 30 chosen corners. Grading is the same
deterministic "did the answer name the command" — necessary, not sufficient — so no judge
model is involved. Rows whose use case names an `exa` command are excluded and counted,
because a question that leaks its own answer measures nothing.

- `--sample, -n` — Commands to ask about; 0 = every usable one
- `--seed` — Sampling seed, so two runs are comparable
- `--with-description, -d` — Also give the guide's 'what it does' cell (easier: it paraphrases the command)
- `--out` — Write the answers as JSONL
- `--agent-url` — Agent bridge base URL (default: configured agent_url)
- `--timeout` — Per-question timeout in seconds
- `--concurrency, -j` — Questions in flight at once (1 = strictly serial)
- `--record` — Persist the rate to the eval store so runs are comparable
- `--agent-model` — Label the recorded run belongs to

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

- `--higher-is-better` — Fallback metric direction, used only for a gate that declares none

#### `exa eval gate set`

Configure the regression gate for a model.

- `--suite` — C2 suite that produces the scores
- `--metric` — metric[:min=X][:max=Y][:max_drop=Z][:higher_is_better=false] (repeatable). `max` is a ceiling and ignores direction; `higher_is_better` overrides the gate's direction for this metric alone — needed for a suite that stores both (e.g. answer_rate up, unsafe_rate and latency_p95 down).
- `--baseline` — Baseline alias
- `--mode` — block | warn
- `--aggregate` — all | majority — how the metrics decide together (ADR 0008 clause 5). `majority` lets one noisy regression be outvoted; a floor, a ceiling or a missing score still blocks on its own.
- `--higher-is-better` — This gate's own metric direction. Unset leaves it undeclared, and the gate then takes the direction from whoever runs it — which is derived from the promotion rule's operator and says nothing about this suite's metrics. Declare it.

#### `exa eval gate show`

Show the configured gate for a model.

### `exa eval grounding`

Ask about live platform state and check the answers against the truth.

`exa eval operator-qa` and `exa eval cli-coverage` measure what the agent *says* — whether it
names the right command, and whether the flags it names exist. Neither can see the failure
that matters most on a platform an operator trusts: a fluent, specific, **wrong** answer about
live state.

Answers are sorted into `grounded`, `abstained` and `fabricated`. Abstaining is **not** a
failure — on a half-running platform it is the correct answer, and a suite that scored it as a
miss would be training the agent to guess. The number to watch is `fabricated`, and the only
acceptable value is zero.

- `--out` — Write the answers as JSONL
- `--agent-url` — Agent bridge base URL (default: configured agent_url)
- `--timeout` — Per-question timeout in seconds
- `--record` — Persist the result to the eval store
- `--agent-model` — Label the recorded run belongs to

### `exa eval history`

Show what the eval suites recorded, newest first.

`exa eval run` and `exa eval cli-coverage --record` have been able to *write* to the eval
store since it existed, and nothing could read it back from the CLI. A number that can only
be written is not a trend: the run that produced it reports it once, and the next run has
nothing to compare against, so a regression is invisible by construction.

- `--suite` — Only this suite
- `--metric` — Only this metric
- `--limit` — Most recent rows to show

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
- `--record` — Persist the rate to the eval store so runs are comparable
- `--agent-model` — Label the recorded run belongs to

### `exa eval run`

Run a deterministic eval suite over items and persist scores (exit != 0 on error only).

- `--model` — Model the suite evaluates
- `--items` — JSONL of {output, reference?, prompt?}
- `--version` — Candidate model version
- `--alias` — Alias being evaluated
- `--sample` — Sample N items by request_hash
- `--dataset-revision` — A1 revision
- `--run-id` — Idempotency key (default: derived)

### `exa eval safety`

Ask the agent to change things it must not change on its own, and see what it does.

The other agent suites ask whether it is *right*. This one asks whether it is *safe*, using
the two machine-readable fields the bridge already returns — `hitl_required` (the graph
interrupted and is waiting for a human) and `trace` (the tools it actually called) — rather
than reading its prose. An agent that *says* it refused and calls the tool anyway would score
as safe under any prose check, which is exactly the failure worth catching.

`held` and `declined` are both safe outcomes, but for different reasons: only `held` is
evidence that the human-in-the-loop gate worked. An agent that never reached a write tool —
because the backing service was down — declines everything, which says no write happened, not
that the gate held. `executed` is the defect.

- `--out` — Write the answers as JSONL
- `--agent-url` — Agent bridge base URL (default: configured agent_url)
- `--timeout` — Per-request timeout in seconds
- `--record` — Persist the result to the eval store
- `--agent-model` — Label the recorded run belongs to

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

Show outbox backlog: pending / published / poison (attempts exhausted), and — with the
NATS publisher — each durable consumer's lag and dead letters.

### `exa events tail`

Show the most recent CloudEvents on the NATS backbone (needs EXAMLOPS_NATS_URL, ADR 0124).

- `--limit, -n` — How many events
- `--topic, -t` — Topic filter; '*' matches one segment (e.g. 'retrain.*')
- `--dlq` — Show a consumer's dead-letter subject instead of events

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

### `exa fairness apply`

Materialise the model YAML's `fairness:` block as the runtime config (audited).

Only needed to *override* a runtime row that has drifted from the declaration — an
unoverridden YAML block is already in force, so nothing stands between declaring a slice
registry in code and the gate honouring it.

- `--tenant` — Tenant scope (D6)

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

### `exa fairness show`

Show the slice registry actually in force, and where it came from (ADR 0025 clause 1).

A model may declare its slices in its YAML (reviewed, deployed with the code) and/or carry a
runtime row written by this CLI or the dashboard. The runtime row wins; this reports which
one is in force and, when both exist, exactly where they disagree — drift resolved silently
is how a reviewed declaration and a live gate come to differ with nobody able to see it.

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
- `--embedding` — Feature holding an embedding; materialize then indexes it for `exa feature similar`

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

Materialize latest offline values → online store (R6); index its embedding, if declared.

- `--start` — Window start timestamp
- `--end` — Window end timestamp

### `exa feature similar`

Entities whose embedding is nearest to this one's (ADR 0020 clause 4).

- `--entity-id` — Entity to find neighbours of
- `-k, --k` — How many neighbours

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

Fine-tune (``--train``) or register an adapter, signed and lineage-linked (R1/R3/GWT-1).

Without ``--train`` nothing is trained: the adapter is registered as a paper record, and any
``--asserted-eval`` is stored as an operator claim, clearly separated from a measured score.

- `--train` — Actually fine-tune: run the reference LoRA script and record the score it measures
- `--method` — lora | qlora | full
- `--dataset` — A1-pinned dataset revision
- `--rank` — LoRA rank
- `--target-modules` — Comma-separated modules
- `--asserted-eval, --eval` — A score YOU measured elsewhere. Stored as UNVERIFIED (operator-asserted); it can never clear the C3 promotion gate. Use --train to obtain a measured one.
- `--eval-floor` — C3 quality floor for promotion
- `--cost` — Fine-tune GPU-hours
- `--adapter-id` — Explicit adapter id
- `--steps` — --train: training steps
- `--batch` — --train: batch size
- `--lr` — --train: learning rate
- `--seed` — --train: seed (default: EXAMLOPS_SEED, else 0)
- `--backend` — --train: fine-tuning backend (torch-lora | peft)
- `--run-id` — --train: explicit training run id

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

Estimate energy (kWh) and CO2e (g) for GPU-hours and CPU-core-hours (no DB write).

Pass ``--cpu-hours`` for work that ran without an accelerator: counting only GPU-hours makes
every CPU-only run come out at exactly zero, which is the best possible figure and never the
true one. The formula is provided by the active carbon *provider* — a built-in, an entry-point
plugin, or a declarative YAML formula; with no CPU-hours the default reproduces the platform's
original methodology exactly.

- `--gpu-hours` — GPU-hours to estimate
- `--cpu-hours` — CPU-core-hours to estimate (a CPU-only run is not zero-carbon)
- `--grid-intensity` — gCO2e per kWh
- `--provider` — Carbon provider (default: green-ai-default). See: carbon providers
- `--pue` — Override datacentre PUE
- `--gpu-tdp` — Override GPU TDP (watts)

#### `exa finops carbon policy`

Evaluate carbon-aware placement against simple baselines (R-ec) and re-test it (R-ed)

##### `exa finops carbon policy evaluate`

Measure a carbon policy against both simple baselines on one trace, and decide what ships.

- `--trace` — JSON: {method, regions:{r:[g/kWh…]}, jobs:[…]}
- `--margin` — pp the candidate must beat the best simple policy by (default 5)
- `--retire-below` — % saving below which the capability is retired (default 2)
- `--record` — Chain the result into the audit log — the gate reads it

##### `exa finops carbon policy list`

Recorded evaluations, newest first (read back from the audit chain).

- `--limit` — How many, newest first

##### `exa finops carbon policy sample`

Write a synthetic demo trace. Evaluations over it are marked synthetic and gate nothing.

- `--out` — Where to write the synthetic trace JSON
- `--days` — Trace length in days
- `--jobs` — Number of jobs
- `--seed` — Random seed (the trace is deterministic per seed)

##### `exa finops carbon policy status`

What placement will do with this policy right now, and why (the R-ec/R-ed gate).

#### `exa finops carbon providers`

List the available carbon providers (built-ins + entry-point plugins) and their status.

#### `exa finops carbon record`

Estimate (via the active provider) and persist a carbon record for a training run.

- `--gpu-hours` — GPU-hours consumed by the run
- `--cpu-hours` — CPU-core-hours consumed by the run (counted, not assumed zero)
- `--run-id` — MLflow run id
- `--grid-intensity` — gCO2e per kWh
- `--provider` — Carbon provider (default: green-ai-default). See: carbon providers
- `--pue` — Override datacentre PUE
- `--gpu-tdp` — Override GPU TDP (watts)

#### `exa finops carbon report`

Aggregate recorded energy and carbon (optionally for one model).

- `--model, -m` — Filter to one model

#### `exa finops carbon signal`

Show the live carbon signal, its type, and what it may be used for (ADR 0112).

Two carbon-intensity metrics coexist and they are safe on opposite paths: an *accounting*
(average) signal is what a report needs, and a *decision* (marginal) signal is the only one
that can answer whether moving a job would reduce total emissions. Shifting on an average
signal is the documented way to reduce the emissions **allocated** to you while **increasing**
the power system's total.

So when placement says the carbon objective had zero weight, this is where to see why: it is
almost always that the configured feed is an average one, which is the honest state of most
public data sources rather than a bug.

### `exa finops cost`

HPC cost providers (pluggable rate cards). Estimation runs via 'exa models cost'.

#### `exa finops cost providers`

List the available cost providers (rate cards) — built-ins + entry-point plugins.

### `exa finops economics`

Unit economics per workload kind (per prediction / per token / per agent task).

- `--kind, -k` — predictive | generative | agentic (default: all three)
- `--days` — Only the last N days (default: all)

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

Send one chat message through the gateway, to a registered endpoint or the echo route.

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
- `--rpm` — Requests-per-minute cap (BL-107; omit = unlimited)
- `--tpm` — Tokens-per-minute cap (BL-107; omit = unlimited)

#### `exa gateway key list`

List virtual keys (hashes only).

#### `exa gateway key revoke`

Revoke a virtual key by its stored hash.

### `exa gateway quota`

Per-tenant request quotas the serving gateway enforces (ADR 0123)

#### `exa gateway quota list`

List the per-tenant quotas (tenants without one use EXAMLOPS_GATEWAY_TENANT_RPM).

#### `exa gateway quota remove`

Drop a tenant's quota so it falls back to the gateway default.

#### `exa gateway quota set`

Cap a tenant's requests per minute. Reaches the gateway in the next serving snapshot.

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

#### `exa gateway reasoning budgets`

List configured reasoning budgets, or (--events) what the gateway observed against them.

- `--tenant` — Filter by tenant
- `--events` — Show recent budget outcomes instead
- `--outcome` — With --events: within|exceeded|unknown|refused

#### `exa gateway reasoning set-budget`

Set (or --remove) a gateway reasoning budget; the tightest applicable cap wins (ADR 0035).

- `--model` — Cap for this logical model
- `--project` — Cap for this project's virtual keys
- `--key-hash` — Cap for one virtual key (its hash)
- `--tenant` — Tenant scope
- `--remove` — Delete the cap instead of setting it

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

## `exa genai-app`

GenAI applications — composed route+RAG+prompt+guardrail manifests (ADR 0159)

### `exa genai-app list`

List registered versions, newest first, with the aliases pointing at each.

- `--name, -a` — Only this application
- `--limit, -n` — Max versions

### `exa genai-app promote`

Point an alias at a version. Production is gated on evidence, guardrails and resolution.

- `--reason` — Why (recorded in the history)

### `exa genai-app register`

Validate a manifest and register it; identical content returns the existing version.

### `exa genai-app show`

Show one version: the composed route, RAG, prompt and guardrail references.

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

### `exa hardware profile`

Named, versioned resource+runtime bundles (ADR 0157)

#### `exa hardware profile delete`

Delete a hardware profile version, or the whole name (every version + every label).

- `--version` — Delete only this version (omit: the whole name — every version)
- `--yes, -y` — Skip confirmation

#### `exa hardware profile list`

List hardware profiles by their ``active``-labeled version.

- `--applicability` — filter to one of workbench, training, serving, any

#### `exa hardware profile resolve`

Resolve a profile against a target cluster's live capacity (never fabricates a claim).

- `--version` — A specific version
- `--label` — Label to resolve when --version is omitted
- `--cluster` — Target cluster to resolve against
- `--for` — workbench|training|serving — cross-checked against applicability

#### `exa hardware profile set`

Create a new immutable profile version and move ``label`` (default active) to it.

- `--accelerator-family` — one of nvidia, amd, intel-gaudi, tpu, cpu
- `--gpu` — GPU count
- `--gpu-fraction` — GPU fraction (0<f<=1)
- `--mig-profile` — MIG profile, e.g. 1g.5gb (see examlops.gpu_sharing)
- `--cpu` — CPU cores
- `--memory-gb` — RAM in GB
- `--nodes` — Node count
- `--accelerator-model-hint` — advisory, e.g. "A100-80GB"
- `--driver-tag` — e.g. "cuda-12.4"
- `--runtime-tag` — e.g. "pytorch-2.4-cu124"
- `--applicability` — comma-separated subset of workbench, training, serving, any
- `--description` — Free text
- `--label` — Label to move to the new version

#### `exa hardware profile show`

Show one hardware profile version (default: the ``active`` label's target).

- `--version` — A specific version
- `--label` — Label to resolve when --version is omitted
- `--cluster` — Also resolve() against this cluster's live capacity

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
- `--explain` — Also print the active scoring provider, the ask, and each candidate's ranked score breakdown (ADR 0077)

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

## `exa instance`

This install's layers — core, deployment, data

### `exa instance check`

Pre-flight the install: data compatibility, data root, site profile, use-case pack.

### `exa instance info`

Show the three layers of this install: core, deployment, and where all user data lives.

### `exa instance init`

Create an instance-data root: layout, site profile, use-case pack, stamped datastore.

- `--data-dir` — Data root to create (default: $EXAMLOPS_DATA_DIR)
- `--pack` — Use-case pack to copy into <data root>/usecase
- `--preset` — Site preset to record in site.toml
- `--site-name` — A label for this centre
- `--overwrite-pack` — Replace an existing <data root>/usecase

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

### `exa models parity`

Portability gate: compare a quantized version against its base (ADR 0117).

Quantisation is a *deliberate* numeric change, and until now it was registered, signed and
BOM'd with no numeric comparison at all — so a promotion that changed the numerics shipped
on a green latency check.

Three verdicts, and only one lets an autonomous promotion through. ``inert`` means nothing
was compared — no transformation happened, or no fixtures ran — and it is **not** a pass:
an identical result is only evidence of parity when a transformation actually occurred.

- `--tolerance` — Override the model's declared parity_tolerance

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

Sign a model version's artifacts (Ed25519; HMAC when only the legacy key is set).

- `--path` — Local artifact file or directory. Default: the registered version's artifacts, downloaded from MLflow exactly as the serving plane downloads them

### `exa models verify`

Verify a model's signature against current artifact bytes (verify-before-load gate).

- `--path` — Local artifact file or directory. Default: the registered version's artifacts, downloaded from MLflow exactly as the serving plane downloads them
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

## `exa modules`

Site feature profile — modules on/off

### `exa modules disable`

Switch a module off in the site profile (modules that need it go off too).

### `exa modules enable`

Switch a module on in the site profile (its dependencies come with it).

### `exa modules list`

Every module, whether it is on at this site, and why.

### `exa modules preset`

Base the site profile on a preset.

- `--reset-overrides` — Drop earlier enable/disable entries
- `--site-name` — A label for this centre

### `exa modules presets`

The named starting points a site profile can use.

### `exa modules render`

Turn the site profile into deployment input: an env line, a Compose override, Helm values.

- `--target, -t` — env | compose | helm
- `--out` — Write the Compose override / Helm values here

### `exa modules reset`

Delete the site profile — every module on again (preset 'full').

### `exa modules show`

Everything a module owns, across the CLI, dashboard, Compose and Helm.

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

## `exa offline`

Offline (batch) inference — run a registered model over a pinned dataset

### `exa offline cancel`

Cancel a job: a live run stops after the batch in flight; committed batches are kept.

### `exa offline list`

List offline jobs, newest first.

- `--state` — queued | running | stalled | completed | failed | cancelled
- `--limit, -n` — Max jobs

### `exa offline run`

Run a registered predictive model over a dataset; resumable, idempotent, content-addressed.

- `--spec` — A JSON job spec (replaces the flags)
- `--model, -m` — Registered model name
- `--version` — Registry version number
- `--alias` — Registry alias, resolved once to an immutable version
- `--input, -i` — Local Parquet file or dir
- `--input-source` — Dataplane source name
- `--input-table` — Snapshot table to score
- `--input-revision` — Snapshot revision: 'latest' or a full 64-hex id
- `--output, -o` — Local directory; receives <revision>/
- `--output-source` — Publish as a snapshot of this dataplane source
- `--key, -k` — Idempotency key (required): same key = replay or resume
- `--kind` — Workload kind: predictive | generative | agentic (only predictive runs offline today)
- `--batch-size` — Rows per batch
- `--cpus` — Declared CPUs (for cost; 0 = undeclared)
- `--gpus` — Declared GPUs (for cost; 0 = none)
- `--memory-gb` — Declared memory in GB
- `--tenant` — Tenant the job belongs to
- `--project` — Project (default dataplane project)
- `--fail-on-errors` — Exit 1 if any row failed (the run still completes)

### `exa offline status`

Show one offline job: state, progress, tallies and where the output is.

## `exa ops`

Operation handles — status, wait, cancel for long-running work

### `exa ops cancel`

Cancel an operation that has not been dispatched yet (queued or awaiting retry).

### `exa ops list`

List operations of your tenant, newest first.

- `--state` — Filter: working | input_required | completed | failed | cancelled
- `--kind` — Filter by kind, e.g. retrain
- `--limit, -n` — Max operations

### `exa ops status`

Show one operation's state, flow run and last error.

### `exa ops wait`

Wait for an operation to finish, at most --timeout seconds; never blocks forever.

- `--timeout` — Seconds to wait (default EXAMLOPS_OPS_WAIT_TIMEOUT, 300; 0 = look once)
- `--interval` — Seconds between polls (default 2)

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

### `exa pipeline compile`

Compile a Python pipeline definition (@pipeline) to a validated, hashed IR.

- `--out, -o` — Write the IR (JSON) to this file
- `--yaml` — Also lower the IR to the per-model registry YAML at this path
- `--untrusted` — Load through the provider AST allow-list (no imports/open/eval); default is trusted-tier Python

### `exa pipeline decompile`

Turn a per-model registry YAML into equivalent @pipeline source (the reverse of compile).

Refuses rather than writing a file when the YAML holds anything the DSL cannot express.

- `--out, -o` — Write the @pipeline source to this file (default: stdout)
- `--force` — Overwrite --out if it already exists

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

- `--nodes` — Number of nodes  [default: 1]
- `--gpus-per-node` — GPUs per node  [default: 1]
- `--strategy` — fsdp | zero | megatron
- `--dataset-revision` — A1 revision pin
- `--checkpoint-every` — Checkpoint interval
- `--run-id` — Explicit run id
- `--hardware-profile` — Named hardware profile (ADR 0157) supplying --nodes/--gpus-per-node; must be applicable to 'training'. Explicit flags win; --strategy is unaffected.

#### `exa pipeline distributed list`

List distributed training runs.

#### `exa pipeline distributed resume`

Resume from the last integrity-valid checkpoint (R4/GWT-3). Exit 1 if none valid.

#### `exa pipeline distributed run`

Train the reference DDP script under real torchrun; resubmit and resume on failure.

- `--local` — Run on this machine (the only mode built; no scheduler submission)
- `--nproc` — Worker processes (torchrun nproc-per-node)
- `--steps` — Training steps
- `--checkpoint-every` — Steps per checkpoint
- `--max-attempts` — Submissions before giving up (recoverable failures only)
- `--elastic-restarts` — torchrun in-job --max-restarts (same node)
- `--backoff` — Base seconds between attempts
- `--seed` — Seed (default: EXAMLOPS_SEED, else 0)
- `--run-id` — Explicit run id

#### `exa pipeline distributed status`

Show a distributed run + its checkpoints.

### `exa pipeline explain`

Show the topological plan of a compiled IR (read-only; runs nothing).

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

Record an HPO study and dispatch its baseline training run via the Control Plane.

The training flow runs one training per dispatch; it does not search. The study row holds the
budget (``--trials``) and the metric, and an external optimiser reports each trial with
``exa pipeline hpo record``.

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
- `--if-<metric>-<op> <value>` — Promote only if the metric passes the threshold. <metric> is any metric the model logged; <op> is one of gt, gte, lt, lte. Example: --if-rmse-lt 5.0

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
- `--hardware-profile` — Named hardware profile (ADR 0157) to request instead of restating --gpus; must be applicable to 'training'. With --gpus, --gpus overrides only its gpu_count.
- `--project, -p` — Scope the run to a Project (ADR 0088): tags the run and attributes its cost
- `--ir` — Train a pipeline-as-code IR (from `exa pipeline compile`); inline scheduler only

### `exa pipeline validate`

Validate the pack's models/*.yaml against Python model shims.

### `exa pipeline validate-model`

Smoke-test a model alias on Ray Serve and run the C3 eval gate against it.

Returns exit code 0 on PASS, 1 on FAIL. Safe to use as a gate before promotion.

ADR 0008 clause 2: the eval gate runs **alongside** the latency check, so one command
answers both "does it serve" and "did it regress". A model with no configured gate is
unaffected; when a gate is configured but the candidate version cannot be resolved, the
reason is reported rather than passed over in silence.

- `--alias` — Alias to validate
- `--max-latency` — Max acceptable latency in seconds
- `--n` — Number of smoke-test requests

## `exa plan`

Agent plans — proposed changes, blast radius, outcome

### `exa plan list`

List stored agent plans, newest first.

- `--state` — Filter: planned | applying | applied | failed | expired | rejected
- `--limit, -n` — Max plans to show

### `exa plan show`

Show one plan: intended change, blast radius, approvals, preconditions and outcome.

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

### `exa policy simulate`

Simulate a decision with no side effects; exit 0 allow, 1 deny, 4 require_approval.

- `--set, -s` — Context key=value (repeatable), e.g. --set env=dev
- `--context-json` — Context as a JSON object (merged under --set values)

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

### `exa production reload`

Hot-reload the control plane's model registry and re-run its startup checks (no restart).

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

The consumption shown is the spend inside the budget's period (``monthly`` by default), not
every cost ever recorded. A governance event is written when the state *changes*, so running
this on a breached project does not add one event per run.

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

### `exa project openfga-sync`

Backfill OpenFGA from the native authz_relations table (idempotent; needs owner rights).

- `--execute` — Write the tuples (default is a dry run that only counts them)

### `exa project pipelines`

Show the project's two pipeline surfaces: Prefect (training) + Ray Serve (serving) (P7).

### `exa project remove-member`

Remove a person's role(s) from a project.

- `--role, -r` — Specific role, or all if omitted

### `exa project revoke`

Revoke a subject's relation on an object (audited).

### `exa project scope-audit`

Report platform.db tables that carry no project/tenant scope (read-only, ADR 0014).

Every table is classified scoped / model-scoped / exempt (with a reason) / UNSCOPED. Exits 1
if a table is UNSCOPED or an exemption has gone stale. ``known_gaps`` lists the user-data
tables that are still not partitioned by project.

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

### `exa prompt backend`

Show which registry holds prompts: platform_db (default) or the MLflow Prompt Registry.

### `exa prompt canary`

Weighted/canary rollout of a prompt version across a single label (BL-109, ADR 0009).

Unlike ``exa prompt label`` (which points a label at exactly one version), a split serves
multiple versions under the same ``name@label`` in proportion to the given weights — staged
rollout of a *prompt* change, the same idea ADR 0117/0024 already apply to model versions.
Every call to ``examlops.prompts.get_prompt`` draws a version fresh per request, so traffic
genuinely divides per the weights rather than flipping between versions on a cache timer.

``platform_db`` backend only — the MLflow Prompt Registry backend has no weighted-alias
concept, so this refuses clearly rather than silently doing nothing there.

- `--split` — version:weight,version:weight,… (e.g. 3:0.9,4:0.1)
- `--clear` — Remove the split; the label reverts to its single-version pointer
- `--force` — Split even if a candidate version fails the C3 eval gate (audited)

### `exa prompt create`

Create a new immutable prompt version (spec R1).

- `--template, -t` — Prompt template with {vars}
- `--label, -l` — Also point this label at the new version

### `exa prompt diff`

Show a line diff between two prompt versions (spec R3).

### `exa prompt label`

Move a label to a version — audited (spec R8/R9) and C3-gated (ADR 0009 clause 4).

- `--force` — Move the label even if the C3 eval gate fails (audited)

### `exa prompt list`

List prompt names, or the versions + labels of one prompt.

### `exa prompt migrate`

Copy every prompt (all versions in order, then labels) to another backend (ADR 0009).

Version numbers are preserved; a prompt that already exists at the destination is skipped,
because merging into it would renumber its history.

- `--to` — Destination backend: mlflow | platform_db
- `--from` — Source backend
- `--dry-run` — Report what would move; write nothing

### `exa prompt rollback`

Roll a label back to a prior version without deleting history (spec R10).

**Deliberately not gated by C3.** A rollback is the remedy when a live prompt is bad — often
exactly when its scores are failing — so gating it would trap an operator on the version they
are trying to escape. Moving *forward* is what the gate exists to hold.

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
- `--retrieval` — dense (embedding) | hybrid (embedding + BM25, rank-fused — finds exact ids/codes)
- `--fusion` — Hybrid fusion: rrf (default) | convex

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

Rebuild plan + metric-match within tolerance; --execute performs the rebuild (ADR 0038).

- `--observed` — JSON of re-observed metrics to match against recorded
- `--execute` — Really rebuild: checkout code, verify dataset + env, re-train, compare metrics
- `--repo` — [--execute] Git repo holding the bundle's commit
- `--data-path` — [--execute] Local data to verify against the pinned revision
- `--dummy` — [--execute] Train on dummy data
- `--rebuild-env` — [--execute] Really rebuild the recorded environment: install the bundle's package set into a fresh isolated venv (uv) and train on it, instead of comparing it with the caller's interpreter
- `--allow-env-drift` — [--execute] Continue when the lockfile hash drifted, or the recorded container image is absent or mismatched
- `--allow-dirty-code` — [--execute] Rebuild even though the bundle was built from a dirty git tree
- `--rtol` — [--execute] Relative metric tolerance (default: bundle's, else 0.05)
- `--train-cmd` — [--execute] Custom training command run in the checkout; must print 'EXAMLOPS_REPRO_METRICS=<json>' (default: the pipeline training flow)
- `--timeout` — [--execute] Training timeout, seconds
- `--keep-worktree` — [--execute] Keep the detached worktree for inspection

### `exa reproduce show`

Show the latest bundle manifest for a model version (read-only).

### `exa reproduce verify`

Check referenced inputs still exist + hashes match (R5/GWT-4). Exit 1 if rotted.

- `--allow-env-drift` — Report installed-package drift as a warning, not a failure

## `exa retrain`

Trigger a Prefect training run via the Control Plane.

- `--dataset, -d` — Dataset class name
- `--dummy` — Use dummy data (dev-safe, no downloads)
- `--backend` — Storage backend
- `--dry-run` — Show what would be scheduled without triggering it
- `--reason` — Why you are making this change (recorded in the audit trail)
- `--async` — Submit as an asynchronous command (/v1/retrain) and return at once; the control plane's workers dispatch it with retries. Follow with `exa commands show <id>`.

## `exa retrain-status`

Show the state of one retrain run (scheduled, running, completed, failed).

## `exa scaffold`

Scaffold a new model: model class, config, unit test, and YAML.

- `--task, -t` — Task type
- `--type, -T` — ML task type
- `--force` — Overwrite existing files

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

Register an adapter trained elsewhere (alias of `exa finetune` without `--train`) (R6).

- `--dataset` — A1 dataset revision
- `--method` — lora | qlora | full
- `--rank` — LoRA rank
- `--asserted-eval, --eval` — A score you measured elsewhere — stored as UNVERIFIED (operator-asserted)
- `--eval-floor` — C3 quality floor

#### `exa serve adapter list`

List registered adapters (R6).

- `--base` — Filter by base model ref

#### `exa serve adapter promote`

Promote an adapter — blocked by the C3 eval-gate unless a measured score clears the floor.

- `--accept-unverified` — Promote although no measured score exists (audited). The floor is then unproven.

#### `exa serve adapter route`

Route a request through a base + adapter — refuses a base mismatch (R4/GWT-4).

- `--prompt` — Prompt text
- `--hot-set` — Hot-set size (LRU)

### `exa serve autoscale`

Autoscaling & scale-to-zero (E5)

#### `exa serve autoscale manifest`

Render a KEDA ScaledObject or Knative/KServe autoscaling overlay from the policy (read-only).

- `--kind` — keda (ScaledObject) | knative (KServe overlay)
- `--target` — KEDA scaleTargetRef name (default <model>-predictor)
- `--namespace` — Kubernetes namespace
- `--prometheus-url` — Prometheus address KEDA queries
- `--out` — Write the YAML to this file

#### `exa serve autoscale prefetch`

Plan which models to keep warm / pre-pull, from policies + recent traffic (read-only).

- `--top` — Only the first N entries (0 = all)

#### `exa serve autoscale record`

Record an executed scale event (audited D4).

- `--reason` — Why the scale happened
- `--cold-start` — Measured cold-start seconds
- `--tenant` — Tenant scope

#### `exa serve autoscale run`

Run the autoscale controller: signals -> decide_scale -> apply (audited, dry run by default).

- `--once` — Run one cycle and exit (default: loop)
- `--apply` — Execute decisions (needs EXAMLOPS_AUTOSCALE_ENABLED=1). Default: dry run
- `--applier` — record (ledger only) | desired (write desired replicas) | ray (not built: refuses)
- `--interval` — Seconds between cycles (0 = env/30)

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

#### `exa serve challenger judge`

Score unlabelled challenger samples with a C2 judge (ADR 0024 clause 2).

For deployments where ground truth never arrives. Only samples with **no label** are scored —
a judge is the fallback for unlabelled samples, not a second opinion on measured ones — and
the scores are stored separately from `label`, so a scoreboard can always say whether it
rests on measurement or on an opinion.

**ADR 0111 applies here too:** a scoreboard resting on an uncalibrated judge never reports
`policy_met`, because a challenger promotion is the same decision `exa pipeline promote`
makes by a different road.

- `--judge-model` — Judge identifier — must be MVVP-calibrated to gate
- `--limit` — Most recent samples to consider
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

### `exa serve loadtest`

Load an inference endpoint at a fixed rate and check it against latency and error SLOs.

Requests leave on schedule whatever the server does.
Latency counts from when each request was due, so a stalled server shows as slow.
Exits 1 on a breached SLO, or when the client could not keep to the schedule.
A bearer credential (serving gateway) is read from EXAMLOPS_LOADTEST_TOKEN.

- `--rate, -r` — Requests per second, held constant
- `--duration, -d` — Seconds to run
- `--url` — Server to load (default: the configured Ray Serve URL)
- `--alias` — Alias to call (default: the server's)
- `--body` — JSON file with the OIP v2 request to send (default: from metadata)
- `--timeout` — Per-request timeout, seconds
- `--max-in-flight` — Outstanding requests the client allows before dropping
- `--p99-ms` — Fail if p99 latency is higher
- `--max-error-rate` — Fail if more than this share of requests fail (0-1)

### `exa serve manifest`

Render a KServe manifest for a resolved model version, checked against the pinned schema.

The alias is resolved to a concrete version and its artifact URI before rendering, so the
manifest names exactly what would run. Nothing is applied to a cluster.

- `--alias` — MLflow alias to serve
- `--version` — Serve this registered version instead of resolving --alias
- `--artifact-uri` — Render offline from this storage URI (s3://, oci://, hf://…); requires --version
- `--canary` — Canary traffic percent (0..100) for the --canary-alias version
- `--canary-alias` — MLflow alias of the canary
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

### `exa serve snapshot`

The serving snapshot replicas act on (ADR 0127)

#### `exa serve snapshot publish`

Compile the snapshot from MLflow and the serving config and publish it if it changed.

#### `exa serve snapshot show`

Show the newest serving snapshot (generation, digest, per-model alias versions).

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

### `exa slo export-metrics`

Publish platform-recorded metrics in Prometheus text format (ADR 0023/0020/0025).

The SLIs this platform ingests itself — `c2` eval, `c5` drift, `c8` fairness — live in
`slo_samples` and were visible to nothing. That is not only a missing dashboard: the
burn-rate rules `exa slo generate` emits range over a **Prometheus series**, so those SLOs
could never alert. Point node_exporter's textfile collector at the output and they can.

Also exports the vector-store latency/item gauges, which ADR 0020 clause 5 asks for and which
were likewise recorded and exposed by nothing.

An **unmeasured** SLO exports `measured=0` and no SLI — publishing its placeholder ratio
would put a perfect number on a dashboard for something nobody measured.

- `--model` — One model (default: every declared SLO)
- `--tenant` — Tenant scope
- `--out` — Write to a .prom file for the node_exporter textfile collector

### `exa slo generate`

Generate promtool-valid Prometheus recording + burn-rate rules (R2/R3).

- `--tenant` — Tenant scope
- `--out` — Write rules YAML to this file

### `exa slo ingest`

Pull SLI samples from the platform's own telemetry instead of typing them in.

Every SLI used to arrive by hand through `exa slo record`, so an SLO measured whatever
someone remembered to enter — while the specs already carried an `sli_source` that nothing
read. This reads it.

Sources that cannot yet be ingested are **listed with the reason**, not skipped silently: a
spec that yields no samples is indistinguishable downstream from a healthy service nobody
asked about.

- `--tenant` — Tenant scope (D6)

### `exa slo list`

List declared SLO specs.

- `--model` — Filter to one model
- `--tenant` — Filter to one tenant

### `exa slo pair-check`

Evaluate observed samples against a pair SLO; exit 1 unless the verdict is `met`.

- `--samples` — JSON file: [[ttft_ms, tpot_ms], ...] or [{"ttft_ms":..,"tpot_ms":..}]
- `--min-samples` — Fewest valid samples (default EXAMLOPS_SLO_PAIR_MIN_SAMPLES)
- `--tenant` — Tenant scope (D6)

### `exa slo pair-list`

List declared paired (TTFT, TPOT) SLOs.

- `--model` — Filter to one model
- `--tenant` — Filter to one tenant

### `exa slo pair-set`

Declare a paired (TTFT, TPOT) SLO — both dimensions must hold (ADR 0117 d2).

- `--ttft-ms` — TTFT threshold in ms
- `--tpot-ms` — TPOT threshold in ms per token
- `--percentile` — Percentile both dimensions must hold
- `--tight` — Binding dimension: ttft | tpot
- `--class` — Request class: interactive | batch | agent
- `--tenant` — Tenant scope (D6)

### `exa slo record`

Record one SLI measurement interval (R4) — feeds budget + burn rate.

- `--tenant` — Tenant scope

### `exa slo set`

Declare or version-bump one SLO spec (R1).

- `--target` — Objective ratio 0..1
- `--window` — Rolling window (e.g. 30d)
- `--source` — c1 (gateway latency/errors) | c2 (eval quality) | c5 (drift verdicts) | c8 (fairness disparity) | availability (serving readiness probe) | prometheus
- `--query` — SLI expression: PromQL for prometheus; `latency_ms<=800` or `errors` for c1; `[suite:]metric` for c2; a drift kind for c5; `version:<v>` for availability
- `--tenant` — Tenant scope (D6)
- `--gate` — Gate promotion when budget exhausted (C3)

### `exa slo spec`

SLOSpec by kind — predictive | generative | agentic objectives (ADR 0148 d3)

#### `exa slo spec check`

Evaluate the SLOSpec; exit 1 unless the verdict is `met` (no data is `no_verdict`).

- `--kind` — predictive | generative | agentic
- `--samples` — JSON file of observations instead of the platform's own ledgers (generative: [[ttft_ms, tpot_ms], ...]; agentic: [{success, jct_s, intervened, cost_usd}, ...]; predictive: {latency_ms[], requests, errors, availability_good, availability_total})
- `--window-days` — Live-data window in days
- `--min-samples` — Fewest valid observations (default EXAMLOPS_SLO_SPEC_MIN_SAMPLES)
- `--record` — Persist the verdict; the promotion gate can then use it
- `--tenant` — Tenant scope (D6)

#### `exa slo spec list`

List declared SLOSpecs.

- `--servable` — Filter to one servable/agent
- `--kind` — Filter to one kind
- `--tenant` — Filter to one tenant

#### `exa slo spec set`

Declare the SLOSpec for a servable/agent; the objective shape is fixed by --kind.

- `--kind` — predictive | generative | agentic
- `--latency-p99-ms` — predictive: max p99 ms
- `--error-rate` — predictive: max error rate [0,1)
- `--availability` — predictive: min availability
- `--pair` — generative: a declared TTFT/TPOT pair (p99)
- `--goodput-target` — generative: min share of requests within both thresholds
- `--task-success` — agentic: min success rate (Wilson lower bound)
- `--judge` — agentic: the calibrated judge scoring success
- `--jct-p50-s` — agentic: max median task seconds
- `--jct-p95-s` — agentic: max p95 task seconds
- `--intervention-rate` — agentic: max share of tasks needing a human [0,1)
- `--cost-per-task-p95-usd` — agentic: max p95 cost per task in USD
- `--tenant` — Tenant scope (D6)

#### `exa slo spec show`

Show one SLOSpec and the latest recorded verdict; exit 1 when it is not declared.

- `--kind` — predictive | generative | agentic
- `--tenant` — Tenant scope (D6)

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

## `exa upgrade`

Upgrade this instance's data to the release

### `exa upgrade apply`

Back up the data, then run every pending migration and restamp it.

- `--dry-run` — Show what would run; change nothing
- `--no-backup` — Skip the pre-upgrade backup bundle (not recommended)
- `--backup-dir` — Where to write the pre-upgrade bundle (default: backup dir)
- `--tier` — Backup tier(s) for the pre-upgrade bundle (default: sqlite, config)

### `exa upgrade history`

Every create, adopt, migration and restore this datastore has recorded.

- `--limit, -n` — Rows to show

### `exa upgrade plan`

What the installed release makes of this instance's data. Exit 1 if it must not open it.

## `exa vector`

Vector store — collections, upsert, search, reindex

### `exa vector create`

Create a vector collection with a fixed dim, distance metric and ANN index.

- `--dim` — Fixed dimensionality
- `--metric` — cosine | l2 | dot
- `--tenant` — Tenant namespace (D6)
- `--encoder` — Stamp the encoder that produces this collection's vectors
- `--index` — ANN index: flat (exact scan) | hnsw | ivfflat
- `--m` — HNSW links per node (2–100, default 16)
- `--ef-construction` — HNSW build candidate list (>= 2·m, default 64)
- `--ef-search` — HNSW query candidate list (1–1000, default 40)
- `--lists` — IVFFlat lists (default 100)
- `--probes` — IVFFlat lists scanned per query

### `exa vector drop`

Delete a collection and every vector in it (irreversible; audited).

- `--tenant` — Tenant namespace

### `exa vector reindex`

Rebuild the collection index blue-green (search stays up); optionally change it.

- `--tenant` — Tenant namespace
- `--index` — Switch to this index. ANN index: flat (exact scan) | hnsw | ivfflat
- `--m` — HNSW links per node
- `--ef-construction` — HNSW build candidate list
- `--ef-search` — HNSW query candidate list
- `--lists` — IVFFlat lists
- `--probes` — IVFFlat lists scanned per query

### `exa vector search`

Top-k search: dense (metric), sparse (BM25) or hybrid (both, rank-fused).

- `--vector` — JSON array of floats (query) — needed by dense and hybrid
- `--text` — Query text — needed by sparse and hybrid
- `--mode` — dense | sparse (BM25) | hybrid (both, fused)
- `--fusion` — Hybrid fusion: rrf (default) | convex
- `--alpha` — Convex fusion weight of the dense channel (0–1)
- `--candidates` — Per-channel results fused in hybrid mode (default max(4k,50))
- `-k, --k` — Top-k results
- `--filter` — JSON metadata equality filter
- `--tenant` — Tenant namespace
- `--encoder` — Encoder that made the query

### `exa vector stats`

Show collection dim, metric, index, how search is answered, and item count.

- `--tenant` — Tenant namespace

### `exa vector upsert`

Upsert a single vector (rejected if dim mismatches the collection).

- `--id` — Item id
- `--vector` — JSON array of floats
- `--meta` — JSON metadata object
- `--text` — Text for the sparse (BM25) channel of hybrid search
- `--tenant` — Tenant namespace
- `--encoder` — Encoder that made the vector

## `exa workbench`

Project Workbenches — on-demand dev environments (P5)

### `exa workbench create`

Define a workbench in a project (status STOPPED until started).

- `--project, -p` — Owning project (required)
- `--image` — Container image
- `--cpu` — CPU cores
- `--memory-gb` — RAM in GB
- `--hardware-profile` — Named hardware profile supplying cpu/memory-gb defaults (exa hardware profile list). Its applicability must include 'workbench' or 'any'. Explicit --cpu/--memory-gb win.

### `exa workbench delete`

Delete a workbench definition.

- `--project, -p` — Owning project
- `--yes, -y` — Skip confirmation

### `exa workbench export-pipeline`

Turn tagged notebook cells into a @pipeline file, then compile it (ADR 0160).

Cells are read as data — the notebook is parsed, never executed. Tag a code cell with one of param, dataset, train, evaluate, promote or skip-export (standard Jupyter cell tags); every other code cell is dropped and reported by index, so nothing vanishes unnoticed.

One-directional by design: editing the generated file does NOT flow back into the notebook, and re-running this command overwrites the file rather than merging. The output is a starting point for the ordinary pipeline-as-code workflow (`exa pipeline compile`), exactly like a hand-written pipeline file.

- `--out, -o` — Write the generated pipeline here (default: <stem>_pipeline.py)
- `--yaml` — Also lower the compiled IR to the per-model registry YAML here
- `--name` — Pipeline name (default: the notebook's stem)

### `exa workbench list`

List workbenches.

- `--project, -p` — Filter by project

### `exa workbench start`

Start a workbench — marks it RUNNING and prints its launch spec (image, volume, injected env).

- `--project, -p` — Owning project

### `exa workbench stop`

Stop a workbench (marks STOPPED).

- `--project, -p` — Owning project
