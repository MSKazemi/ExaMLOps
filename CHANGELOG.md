# Changelog

All notable changes to ExaMLOps are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [0.44.0] - 2026-07-29

### Added

- **feat(agent): procedure-write review-queue for Skipper memory (SM3, ADR 0034).** Instead of an
  inline HITL interrupt on every `record_procedure`, procedure writes can be queued for **batch**
  operator review — extending the Phase-11 approval-gate pattern to agent memory. New
  `skipper.memory_review` (SQLite-backed `enqueue`/`list_pending`/`approve`/`reject`; approving
  commits the procedure to the memory store, rejecting drops it) + `python -m skipper.memory_admin
  review list|approve <id>|reject <id>`. When `AGENT_MEMORY_REVIEW_QUEUE=1`, `record_procedure`
  enqueues and returns a review id instead of writing directly. **Off by default** — inline HITL
  stays the default path. The queue lives in its own SQLite file (`AGENT_MEMORY_REVIEW_DB`),
  separate from the memory store, so only approved procedures ever reach it. 6 unit tests.

### Fixed

- **fix(make): `make skipper-test` install list also omitted `langchain-openai`** (like
  `langgraph`/`langchain` in 0.42.0) → `test_llm` failed to collect. Added it; the full agent
  suite (141 tests) now passes.

## [0.43.0] - 2026-07-29

### Added

- **feat(finops): live grid carbon-intensity provider `grid-live` (ADR 0074).** A new `carbon`
  provider that fetches the *current* grid carbon intensity (gCO2/kWh) from an operator-configured
  endpoint (ElectricityMaps / WattTime / a national-grid API) instead of a static factor, so carbon
  figures track when the grid is clean vs dirty. New `examlops.finops.grid_intensity` module
  (endpoint-agnostic JSON parse, ~5-min TTL cache, optional `{zone}` + bearer token via
  `EXAMLOPS_GRID_INTENSITY_URL` / `_ZONE` / `_TOKEN`). **Graceful degradation**: with no endpoint,
  an unreachable/unparseable/non-positive reading, `grid-live` falls back to the static default —
  byte-identical to `green-ai-default` offline — so carbon accounting never breaks. An explicit
  `grid_intensity_g_per_kwh` input always overrides the live signal. `exa finops carbon estimate
  --provider grid-live`. 19 unit tests; existing carbon-provider math unchanged.

## [0.42.0] - 2026-07-29

### Added

- **feat(agent): hard intent-gate on Skipper memory retrieval (SM3, ADR 0034).** `recall_memory`
  now consults a pure, dependency-free gate (`skipper.memory_gate.retrieval_allowed`) before
  touching the store: it **blocks** a recall when the query asks for *current* platform state
  (a temporal/live marker like "current"/"now"/"latest" co-occurring with a state noun like
  "version"/"drift"/"cost"/"audit") and redirects the agent to the dedicated live tools, and
  refuses empty/too-short queries. Previously this was only a soft docstring hint — now it is
  enforced, so long-term memory can't surface stale answers as if they were live state. The gate
  is deliberately conservative (needs *both* a marker and a state noun), so genuine "procedure for
  handling drift" recalls still work. 13 unit tests; existing memory tests green.

### Fixed

- **fix(make): `make skipper-test` was missing `langgraph`/`langchain` core in its install list**,
  so agent-test collection failed with `ModuleNotFoundError: langgraph.types`. Added them.

## [0.41.0] - 2026-07-29

### Added

- **feat(synth): synthetic-only promotion gate (A7 spec R5/GWT-5, ADR 0042).** Closes the A7
  governance loop: a model trained **only on synthetic data** can now be refused promotion.
  `examlops.promotion_gates` resolves a model's training dataset revisions from its A2 lineage
  (`training_dataset_revisions`) and reports `synthetic_only_training(model)` by composing the
  BL-001 `is_synthetic_only` primitive. Wired two ways: (1) `exa pipeline promote` blocks a
  synthetic-only model when `EXAMLOPS_SYNTHETIC_ONLY_GATE` is enabled (mirrors the C6 SLO / C8
  fairness gates; `--force` overrides, audited as `promotion_blocked_by_synthetic_only` /
  `synthetic_only_gate_override`); (2) the self-driving autopilot passes `synthetic_only` in the
  `autopilot_promote` policy context so a D5 policy rule (`when: synthetic_only == True → deny`)
  can forbid auto-promotion. Fail-open on missing lineage (unknown provenance never blocks).
  6 unit tests.
- **feat(usecase): installable use-case packs via entry points (platform-usecase Stage 5, ADR
  0094).** The platform loader (`examlops.usecase`) now discovers the active pack generically
  from the `examlops.usecase_packs` entry-point group — a new precedence rung between
  `EXAMLOPS_USECASE_DIR` and the bundled default — so a pip-installed pack is found with no env
  var and no code change, while the platform still names no concrete use-case (fail-open on a
  broken pack). The SEANERGYS pack is graduated to an installable `exa-pack-seanergy`
  (`usecases/seanergy/pyproject.toml` + `exa_pack_seanergy.pack_root` shim; `pip install -e
  usecases/seanergy`). Loader discovery fully unit-tested (6). Non-editable wheel data-packaging
  is a build-host follow-up.

## [0.40.0] - 2026-07-29

### Added

- **feat(cli): `--reason` change-provenance + early access-scope hints on mutating commands
  (exa CLI next-gen N5).** Governance-critical mutations now accept `--reason "<why>"`, recorded
  in the audit trail (`audit_events.details.reason`) so a reviewer sees *why* a change was made,
  not just what and by whom. Applied to `exa retrain`, `exa drift baseline|reset`, `exa drift
  input baseline|reset`, `exa serve traffic`, `exa approvals approve` (`reject` already had it) via
  a shared `examlops.cli._provenance` helper (`reason_option` + `audit_details` fold +
  `scope_hint`). Backward-compatible: the `reason` key is omitted when none is given. `exa retrain`
  also surfaces an early `CONTROL_PLANE_TOKEN` hint when the credential is missing, so the
  requirement shows *before* the request is rejected.
- **feat(cli): `--output md|html` structured export (exa CLI next-gen N3).** Extends the central
  `_output` format dispatch (which already did json/yaml/csv) with Markdown-table and
  HTML-table renderers, and adds `md`/`html` to the global `OutputFormat` enum — so **every**
  structured command (`exa status`, `exa doctor`, and all others) can emit Markdown for
  reports/PRs or HTML for embedding, with zero per-command changes. HTML cell values are escaped
  (no injection from data); Markdown escapes pipes and flattens newlines; non-tabular data
  degrades to a fenced JSON block / `<pre>`.

## [0.39.0] - 2026-07-29

### Added

- **feat(synth): synthetic data generation — `exa data synth` (Next-Gen 40 · A7, ADR 0042).**
  Completes the flagship Next-Gen 40 (the last unimplemented feature). Fit a generator to a real
  A1 dataset revision, generate provenance-flagged synthetic records, and gate them on **fidelity
  + privacy** so synthetic data can never pass as real (spec R1–R6). New `examlops.synth` package
  (`generators`/`metrics`/`gate`): `synth_fit`/`synth_generate`/`synth_evaluate`. **SDV is an
  optional `examlops[synth]` extra** — without it a dependency-free Gaussian-copula fallback
  (empirical marginals + rank-correlation copula for numerics, empirical frequencies for
  categoricals, bootstrap resampling for embeddings/list columns) keeps every subcommand — and the
  release gate — working offline, the same graceful-degradation pattern as A1 (lakeFS→content-hash).
  Fidelity (per-column KS/TV distance + correlation preservation) and privacy
  (distance-to-closest-record + exact-match memorisation signal) are both computed in the fallback
  path so the gate is **never a silent no-op**; a memorising generator is flagged and blocked
  (R3). Released datasets are recorded as **A1 revisions flagged `synthetic=true`** with an A2
  lineage edge to the source revision + generator config (R4); `is_synthetic_only`/
  `synthetic_proportion` primitives let a D5 policy forbid synthetic-only promotion (R5). New CLI
  `exa data synth fit|generate|evaluate`; additive `dataset_revisions.synthetic/source_revision/
  generator` columns (idempotent migration) + `synthetic_datasets` gate-record table. Guide
  `docs/guides/synthetic-data.md`; 19 unit tests (GWT-1..5 + fail-closed gate + fallback
  determinism), 96% package coverage.

## [0.38.0] - 2026-07-22

### Added

- **feat(backup): whole-platform backup & recovery — one tiered bundle (ADR 0095, extends Phase 0
  item 0.9).** The original single-DB `exa backup` (a `platform.db`-only snapshot) is now the SQLite
  tier of a **tiered bundle** that captures every data store: all platform SQLite DBs (`platform.db`,
  `approvals.db`, `skipper_memory.db`, `agent_memory.db`), the on-disk **config** tree (with secrets
  *key-ids only* — KEK stays out-of-band), **Postgres** (MLflow + Prefect via `pg_dump -Fc`),
  **MinIO** buckets (`mlflow-artifacts` + `examlops-projects` via boto3 mirror), and use-case
  **content**. New `examlops.backup` package (`sqlite_tier`/`postgres_tier`/`objects_tier`/
  `config_tier`/`bundle`/`remote`/`retention`/`schedule`/`auto`); the four legacy functions
  (`create_backup`/`verify_backup`/`restore_backup`/`list_backups`) and `exa backup
  create|verify|restore` are preserved **byte-for-byte**. New CLI: `exa backup create --all
  [--with-postgres|--with-objects|--with-content] [--push] [--strict]`, `verify-bundle`,
  `restore-bundle --tier`, `schedule [--once]`, `prune`, `pull`, `list --remote`, `status`.
- **feat(backup): graceful degradation** — any heavy tier whose tool/endpoint is unavailable is
  recorded `skipped` (not failed); the bundle still succeeds. The default control-plane profile
  (`sqlite` + `config`) has zero external deps. `--strict` promotes a skip to a hard failure (CI/DR).
- **feat(backup): Compose `backup` sidecar** (opt-in `backup` profile + `Dockerfile.backup` with
  `postgresql-client`) runs `exa backup schedule` inside the stack (reaches Postgres + MinIO), with
  retention rotation and off-site S3 replication (`EXAMLOPS_BACKUP_S3_URI`). Host systemd-timer/cron
  documented for non-Docker installs.
- **feat(backup): auto-backup before risky ops** — a fast, best-effort control-plane bundle is taken
  before `backup restore`/`restore-bundle`, `secrets rewrap`, and (opt-in via
  `EXAMLOPS_BACKUP_ON_PROMOTE`) an autopilot promotion.
- **chore(backup): `examlops[backup]` extra** (boto3, lazily imported); `make dr-drill` now covers
  the whole-platform bundle round trip. New env vars `EXAMLOPS_BACKUP_DIR|TIERS|INTERVAL|RETAIN|
  S3_URI|PG_DBS|BUCKETS|ON_PROMOTE`. Guide `docs/guides/backup-restore.md` rewritten; ADR 0095.

## [0.37.0] - 2026-07-20

### Added

- **feat(providers): notebook/CLI/dashboard-authored, per-project, AST-sandboxed calculation
  providers (ADR 0074).** Author the Python behind any calculation (FinOps `cost`/`carbon`, `drift`,
  `promotion`, `llm_*`) from a **Jupyter notebook**, the CLI, or the **dashboard** — per-project, with
  no pip-install. `examlops.providers.sandbox` AST-allowlist gate (rejects `import`/`eval`/`exec`/
  `open`/`getattr`/dunder-access/`globals`; `Provider`/`ProviderMeta`/`math` pre-injected);
  `providers.authoring` per-project store (`$EXAMLOPS_PROVIDERS_DIR/<project>/<domain>/<name>.py`) +
  `register_from_source`/`save_provider`/`set_active_provider`/list/read/delete; `exa providers
  author|authored|show|activate|rm|validate`; dashboard **Providers** card on each project (code
  editor + Validate + Activate/Delete, `project.manage`-gated + audited); FinOps
  `estimate_cost/carbon_via_provider(project=)` honour the project's active provider. JupyterHub
  notebooks now mount the repo + shared provider store so `import examlops` works; starter notebook
  `docs/notebooks/manage-providers.ipynb`. Guide `docs/guides/authored-providers.md`.
- **feat(dashboard): project workspace is now editable end-to-end.** Create/start/stop/delete a
  project **workbench (notebook)** from the UI (own persistent volume, injected project connections);
  edit **quota** (CPU/mem/storage/GPU), **budgets**, and the isolated **namespace** via a new
  `PUT /api/v1/projects/{name}`; namespace surfaced as a badge.
- **feat(dashboard): editable serving traffic split** from the MLOps console (per-alias weights,
  sum-to-100 gate) via `PUT /api/platform-data/traffic-rules/{model}` — `exa serve traffic` parity.
- **feat(dashboard): editable drift actions** — Set-baseline / Reset / auto-retrain enable-disable
  per model on the Drift page (`exa drift baseline|reset|auto-retrain` parity); fixed the no-op
  Drift Refresh button.

### Fixed

- **fix(dashboard): `GET /api/containers` 500 (Services tab).** The hardened docker-socket-proxy
  forbids the `/images` endpoint, so reading `c.image` raised 403 → 500 and the Services tab was
  blank. Image name is now read from the container-list attrs instead.
- **fix(cli): read-phase socket timeouts wrapped as `ClientError`.** `_client._send` caught
  `HTTPError`/`URLError` but a bare read-phase `TimeoutError` leaked past every caller, crashing MCP
  read tools (and intermittently reddening the surface-contract gate). Now degrades gracefully.
- **fix(ci): `sanity:check-structure` unblocked.** The GitLab structure gate still asserted
  `test -d pipelines/models`, a directory retired by the ADR 0094 platform/use-case split (models
  moved to `usecases/seanergy/models/`), so **every** pipeline failed at the sanity stage — tests and
  deploy never ran. The check now verifies the use-case pack's model dir.

### Added (pre-existing, this cycle)

- **docs(hpc): new guide `docs/guides/hpc-training-workflow.md` — run a Prefect `training_flow` on a
  real Slurm/Flux cluster end-to-end** (scheduler × transport axes, resource env vars, `local` vs
  `ssh` transport, the governed `exa hpc --cluster` path, and a verified JPCP × PM100 example on the
  live `lxp` Flux instance). Cross-linked from `hpc-fleet.md`.

### Fixed

- **fix(hpc): real-scheduler (`slurm`/`flux`) training now honours `--dummy`.** The Prefect flow's
  generated `run.sh` wrapper dropped the dummy flag, so every real-HPC run trained on the full
  dataset — there was no fast smoke-test path on a cluster. `slurm_submit_task` now threads
  `is_dummy` through and forwards `--dummy` to `slurm_train_script.py` (parity with mock mode and the
  CLI flag). `pipelines/pipeline_generator.py`; regression tests in `tests/unit/test_pipeline.py`.
- **fix(hpc): `LocalExecutor.put`/`get` no longer raise `shutil.SameFileError`** when the Flux/Slurm
  worker runs *on* the compute node and the flow's local job dir coincides with the remote workdir
  (a supported "worker on the login node" topology). Same-path stages are now a no-op.
  `platform/infra/slurm-adapter/executor.py`; regression tests in `tests/unit/test_executor.py`.

### Changed

- **fix(dashboard): dashboard Documentation page moved from `/docs` to `/documents` (route collision).**
  FastAPI serves its Swagger UI from an explicit `/docs` route, which is matched before the React SPA
  catch-all — so the dashboard's own Documentation page (also routed at `/docs`) was permanently
  shadowed by Swagger and unreachable in the browser. The documentation surface now lives at
  `/documents`: SPA route, sidebar nav, command palette, and global-search nav target updated; the
  backend content API moved `/api/docs/{tree,content}` → `/api/documents/{tree,content}`
  (`routers/docs.py` prefix). `/docs` now belongs solely to the Swagger UI. Ray Serve's own `/docs`
  is unaffected. Docs corrected (`docs/guides/interfaces.md`, `docs/dashboard/auth.md`) — the Swagger
  UI is at `/docs`, not `/api/docs` as previously stated. Tests: `backend/tests/test_docs.py` (7)
  green against the new routes.

### Fixed

- **fix(dashboard): Documentation page was empty ("No results") in the deployed container.** The
  `/documents` router (`routers/docs.py`) locates the docs set via `EXAMLOPS_DOCS_ROOT` or by
  walking up from its own file for a parent holding both `README.md` and `docs/`. In the dashboard
  image neither held: the env var was set only in CI, and the docs `COPY` lines in
  `Dockerfile.dashboard` were commented out — so `_ROOT` resolved to `None`, the tree came back
  empty, and the page rendered `No results for ""`. Fixed on two fronts so docs render in **every**
  deployment topology: (1) docker-compose now sets `EXAMLOPS_DOCS_ROOT=/repo` (the repo is already
  bind-mounted there in dev); (2) the image now bakes `README.md` + `mkdocs.yml` + the whole `docs/`
  tree, so the router auto-detects `/app` as root when no bind mount exists (K8s/PVC). Verified in a
  built image: with no env var, the router resolves `/app` → 13 sections / 100 files. The Docker
  frontend builder was bumped `node:20-alpine` → `node:24-alpine` (current LTS; Vite 7 / rolldown
  require ≥20.19). The sidebar nav + command-palette labels were also finalized `Docs` → `Documents`
  to match the route and stay clear of Swagger's `/docs`. Frontend `npm run build` + 276 tests green;
  `backend/tests/test_docs.py` (7) green.

- **fix(dashboard): `/docs` + `/redoc` rendered blank under the strict CSP.** FastAPI's built-in
  Swagger UI / ReDoc pages load their JS+CSS from `cdn.jsdelivr.net`, but the F16 / ADR-0053
  `SecurityHeadersMiddleware` sets `Content-Security-Policy: … script-src 'self' …`, so the browser
  refused the CDN scripts and the API-docs page stayed empty. The docs are now re-served from
  **same-origin vendored assets** under `/static` (Swagger UI 5.17.14 + ReDoc 2.1.5, `docs_url`/
  `redoc_url` disabled and re-registered via `get_swagger_ui_html`/`get_redoc_html`), so they render
  under the strict CSP and work offline on the HPC/lxp deploy. `/openapi.json` is unchanged.
  Regression guard: `backend/tests/test_openapi_docs.py` (no CDN reference, assets served).

### Added — Enterprise-readiness Phase 5 (started)

- **feat(autopilot): predictive pre-emptive drift forecasting + root-cause classifier (item 5.2).** New
  `examlops.forecast` + `exa drift forecast <model>` turns the reactive autopilot predictive: it fits a
  trend to a model's recent prediction drift (rolling z vs baseline) and projects **when** it will breach
  the critical threshold, so a retrain can fire *before* the degradation window instead of after. Adds the
  missing "decide" stage — `classify_drift_cause` distinguishes input-distribution vs prediction-shift vs
  data-volume vs concept drift from the input/prediction z-scores + volume ratio — so the response can be
  targeted. Pure/offline-testable; `test_forecast.py` (13).
- **feat(mcp): agent-callable `fleet_simulate` tool (item 5.5 slice).** The Fleet Digital Twin is now an
  MCP read tool, so a governed fleet copilot can compose what-if projections (placements/cost/carbon/queue)
  into previewed, HITL-gated plans without touching live state — the read-only planning primitive item 5.5
  builds on. Registered read-only (never mutating). `test_cli_mcp.py` +2.
- **feat(exchange): signed cross-institution packages (item 5.6).** New `examlops.exchange` +
  `exa exchange pack|verify|inspect|import` — a portable `.novapack` bundle (stdlib zip) for sharing
  models/pipelines/providers/policy-packs across institutions on the D3 signing substrate. Every package
  carries a per-file SHA-256 set + a signature over the bundle digest; **verify-before-import** refuses to
  extract anything whose signature or integrity fails (tampered file, forged signature, wrong key). Packing
  without a signing key fails closed. `test_exchange.py` (8). The marketplace discovery surface (MCP/A2A
  listing, Projects scoping) layers on top.
- **feat(fleet): FleetScape server-side tile aggregation for the 3D/NOC heatmap (item 5.4).** New
  `examlops.fleetscape` + `exa fleet heatmap` reduce the fleet registry to a positioned tile grid with a 0–1
  health per tile (down=0 … idle=1) + per-cluster and fleet rollups — the compact, painted-server-side model
  the bundled WebGL rack/GPU heatmap and NOC video-wall render (degrading to the existing SVG grid over the
  same tiles where WebGL is unavailable). Pure over injected nodes → `test_fleetscape.py` (5). The WebGL/
  video-wall front-end itself is the browser-side follow-up.
- **feat(fleet): Digital Twin & What-If Studio simulation engine (item 5.1, flagship).** New
  `examlops.fleet_twin` + `exa fleet simulate` — a *pure* projection of the live fleet: apply a scenario
  (submit N jobs, add GPUs, shift a cluster's grid-carbon/price) and get back where each job places, the
  projected GPU-hours / cost / carbon, and the queue depth — plus the delta vs the current baseline. It
  reuses the production placement scoring fn + capacity model (and any `--optimize` provider incl. the new
  carbon-aware ones), so the projection matches real scheduler behaviour, and touches nothing live. The 3D
  NOC view (5.4) will render this state. `test_fleet_twin.py` (6, incl. capacity depletion, queue overflow,
  carbon-steered placement, cost/carbon rollup).
- **feat(placement): carbon- and cost-aware fleet placement (item 5.3).** Three new built-in placement
  providers — `carbon-aware`, `cost-aware`, `carbon-cost-balanced` — factor a cluster's grid carbon
  intensity (`carbon_intensity` gCO₂e/kWh) and GPU cost (`cost_per_gpu_hour`) into the scoring fn on top
  of least-loaded headroom, so the fleet steers jobs to the greenest/cheapest cluster that fits. Selected
  via the existing `exa hpc place --placement-provider …` / `EXAMLOPS_PLACEMENT_PROVIDER` (zero CLI change,
  ADR 0077). Degrades to least-loaded byte-for-byte where no carbon/cost signal is present.
  `test_carbon_aware_placement.py` (5).

### Added — Enterprise-readiness Phase 3 (started)

- **feat(observability): HA long-term metrics + tail-sampling collector (item 3.3).** New
  `otel-collector-config.yml` — an OpenTelemetry Collector with **tail-based sampling** (keep every error +
  slow (>1s) trace, sample the healthy 5%) so the 48h Tempo isn't firehosed, plus traces→Tempo and
  metrics→Prometheus-remote-write pipelines targeting Mimir/Thanos on object storage for unlimited retention.
  Prometheus gains a documented env-driven `remote_write` block. `test_otel_collector_config.py` (4) guards
  the structure. Standing up Mimir/Thanos + the collector container is the operator deploy step.
- **feat(observability): Prometheus service-discovery from the fleet registry (item 3.1).** New
  `examlops.prometheus_sd` + `exa hpc prometheus-sd` generate Prometheus `file_sd` scrape targets
  (`node_exporter` on every node + NVIDIA `DCGM-exporter` on GPU nodes) directly from the authoritative
  `hpc_clusters`/`hpc_nodes` registry, each labelled with cluster/scheduler/tenant — so a node joining the
  registry auto-adds its scrape targets instead of a hand-edited `prometheus.yml`. `test_prometheus_sd.py`
  (8). The node-agent exporter DaemonSet itself is the operator deploy step.
- **feat(reporting): offline cost/carbon/project reports (item 3.5).** New `examlops.reporting` +
  `exa report generate [--format html|pdf|text] [--project N] [--out FILE]` assembles FinOps cost
  (per-model GPU-hours + $), Green-AI carbon (kg CO₂e), and project consumption into a shareable
  report. HTML + text render dependency-free; PDF uses WeasyPrint when installed and **degrades to
  HTML** (with a notice) when not. Each section is fail-open — a broken source degrades only itself.
  `test_reporting.py` (5).

### Added — Enterprise-readiness Phase 4 (started)

- **feat(config): `exa env --validate` cross-service coherence check (item 4.2 slice).** New
  `examlops.config_validate` cross-checks the effective environment (130+ vars) and `exa env --validate`
  fails CI/startup on incoherence: a backend selected without its endpoint (`EXAMLOPS_COORDINATOR=redis`
  sans `EXAMLOPS_REDIS_URL`, `EVENT_PUBLISHER=nats` sans broker, `DB_BACKEND=postgres` sans DSN), OIDC
  issuer without a JWKS, malformed URLs, and weak/placeholder secrets (warnings). Pure over an injected env
  dict → testable. `test_config_validate.py` (9). The full versioned `/api/v1` gateway + one pydantic-settings
  schema remain the larger 4.2 build.
- **refactor(data): per-domain `examlops.data.*` facades + COMPLETE platform_db public-API migration (items 4.5/4.1).**
  New `examlops.data` package splits the 236-helper `platform_db` monolith into **18 owned per-domain modules**
  (audit/drift/hpc/projects/finops/serving/events/admission/coordination/secrets/autopilot/evaluation/
  data_assets/governance/prompts/registry/agent/gateway), each a **dynamic `__getattr__` proxy** (PEP 562) that
  resolves to the current `platform_db` helper — a transparent, non-destructive owned surface that keeps
  monkeypatching `platform_db.<fn>` working through the facade. **Every public-API call site migrated** (a
  scripted 76-file sweep + attribute-style swaps), driving the coupling ratchet **99 → 0**; the guard is pinned
  at 0 so no new code can reach into the monolith's public surface. `test_data_facades.py` (21) + the surface
  contract harness. Physically relocating each helper body into its module is now a functionally-inert follow-on.
- **test(arch): cross-surface contract harness (items 4.5/4.8).** `test_surface_contract.py` boots the SDK
  and MCP surfaces against one ephemeral data plane and asserts they return **identical** results for shared
  operations (e.g. `sdk.place` and the `hpc_place` MCP tool choose the same cluster with the same reason), so
  a change that breaks surface parity fails CI. It also pins the item-4.6 contract: every zero-arg MCP read
  tool must return a `{ok: bool, error?: str, …}` envelope. (3 cases.)
- **test(arch): data-layer coupling ratchet (item 4.1).** ~96 modules reach directly into `platform_db`
  instead of the semver'd `examlops.sdk` facade. A full migration is a tracked refactor; this guard stops
  the coupling from *growing* — `test_platform_db_coupling_ratchet.py` fails CI if a new non-data-layer
  module imports `platform_db` (route it through the SDK), and enforces the baseline only ever ratchets
  DOWN as call sites migrate. The data layer (`platform_db.py`, `storage/`, `sdk/`) is exempt.
- **feat(i18n): shared HPC-unit formatter (item 4.7 slice).** New `examlops.units` — one canonical, locale-aware
  formatter for GPU-hours, bytes (IEC), SI quantities, cost, carbon, and durations, so every surface renders
  quantities identically instead of hand-formatting per console (the F19 i18n claim's formatter half). European
  locales flip decimal/grouping style. `test_units.py` (6). The literal-string lint gate + complete EN/IT across
  the 27 React consoles is the frontend-host follow-up.
- **feat(api): keyset (cursor) pagination primitive (item 4.3).** New `examlops.pagination` — constant-cost,
  stable-under-writes cursor pagination (`WHERE key < :cursor ORDER BY key DESC LIMIT n+1`) to replace
  linear `OFFSET` scans on fleet-scale list views (DataGrid/fleet/queue/registry). Opaque URL-safe cursors,
  a hard `MAX_PAGE_SIZE=500` cap, and `seek_clause`/`fetch_limit`/`paginate` helpers. Walking the cursors
  reconstructs the full set with no gaps/dupes even as the tail is fetched incrementally. `test_pagination.py`
  (7). Wiring it into the list endpoints + DataGrid virtualization is the frontend follow-up.
- **feat(sdk): canonical result-envelope contract (item 4.6, foundation).** New `examlops.sdk.Result` +
  `ok()`/`err()` — one typed `ok`/error shape for every programmatic surface (CLI JSON, MCP, agent,
  dashboard BFF) so callers stop guessing bespoke dicts. `to_dict()` emits the exact historical wire
  format (`{"ok": bool, "error"?: str, **data}`), so the MCP `_err` helper now routes through it
  **byte-identically** (zero wire change) — the first adopter; others convert incrementally. `test_result_envelope.py` (4).

### Added — Enterprise-readiness Phase 2 (started)

- **feat(security): OpenFGA authorization-model export (item 2.5 — RBAC → OpenFGA).** New
  `examlops.authz.openfga` emits the OpenFGA authorization model (both `.fga` DSL and JSON API shape) that
  mirrors the D6 relation model — `owner ⊇ editor ⊇ viewer` over `project` → model/pipeline/dataset/… objects
  with parent inheritance — plus a `relation_tuples()` export of the live `authz_relations`, so RBAC can be
  lifted out of the shared SQLite into a dedicated OpenFGA service with an *equivalent* (non-drifting) model
  instead of a hand-authored one. `test_openfga_export.py` (9). Running OpenFGA + tuple sync is the infra step.
- **feat(security): A2A `securitySchemes` on the agent card (item 2.2 slice).** The Skipper A2A Agent Card now
  declares how a caller authenticates — an `openIdConnect` scheme when `EXAMLOPS_OIDC_ISSUER` is set (pointing
  at the IdP's `.well-known/openid-configuration`), else a `bearer` scheme — closing the audit finding that
  the card lacked `securitySchemes` entirely and implied an unauthenticated agent. Pairs with the 2.1 OIDC
  validator; route-dependency enforcement on the live Skipper WS/REST/MCP routes is the runtime-wiring step.
- **feat(security): OIDC/OAuth2 access-token validation + identity propagation (item 2.1).** New
  `examlops.oidc` accepts IdP-issued **RS256** tokens instead of the shared control-plane token / HS256
  secret: `verify_bearer`/`verify_token` check the signature against the issuer's JWKS (URL or inline) plus
  issuer/audience/expiry, then derive a real per-user **subject + tenant** `Identity` whose `.actor`
  (`tenant/subject`) threads into `EXAMLOPS_ACTOR` + the audit trail. Off by default (`EXAMLOPS_OIDC_ISSUER`
  unset → single-tenant keeps working); fail-closed on every bad-token path. `test_oidc.py` (10, using a
  locally-generated RSA key + JWKS — no IdP needed). Route-dependency wiring into the control plane /
  dashboard / Skipper is the integration step.
- **feat(security): external WORM anchor for audit checkpoints (item 2.4).** The D4 hash-chain makes
  tampering *within* `platform.db` detectable, but a full-DB rewrite could re-chain a forged history.
  New `examlops.audit_worm` anchors each `exa audit checkpoint` to an append-only, **itself-hash-chained**
  WORM log (`EXAMLOPS_AUDIT_WORM_PATH`; O_APPEND local file for dev, an S3 Object-Lock path or Rekor log in
  prod). `exa audit verify-worm` checks the anchor chain **and** that every DB checkpoint is anchored —
  divergence in either store is flagged. No-op when unconfigured. `test_audit_worm.py` (6, incl. tamper +
  unanchored-checkpoint detection).
- **test(security): cross-tenant isolation conformance (item 2.6).** `test_tenant_isolation_conformance.py`
  seeds two tenants into every tenant-scoped store (secrets, SLO specs, policy bundles) and proves tenant A
  can neither read nor list tenant B's data — same-path secrets resolve to the caller's own tenant, a
  B-only secret is `SecretNotFound` for A, and every scoped `list_*` is single-tenant. A regression that
  widens or drops a `WHERE tenant=?` clause fails this gate loudly. (5 cases.)
- **feat(secrets): envelope encryption with per-secret key_id + online KEK rotation (item 2.3).** The
  secrets store gains a KEK **keyring** (`EXAMLOPS_SECRETS_KEYS=key_id:fernet_key,…` +
  `EXAMLOPS_SECRETS_ACTIVE_KEY`): new secrets are wrapped under the active key with its `key_id` stored
  per row (additive migration), old ciphertext keeps decrypting under its own key, and `rewrap_secrets`
  / `exa secrets rewrap [--dry-run]` re-encrypts everything under the active key so a retired key can be
  dropped — all without downtime, plaintext never leaving the process. `DASHBOARD_SECRET_KEY` is
  demoted from *primary* KEK to a legacy decrypt-only fallback. `test_secrets_envelope.py` (6); the 10
  existing secrets tests stay green (back-compat).

### Added — Enterprise-readiness Phase 1 (started)

- **feat(k8s): zero-downtime rolling upgrades + additive-migration guard (item 1.10).** The chart's
  Deployments use `RollingUpdate` with `maxUnavailable: 0` + readiness probes, so cutover is
  readiness-gated (no capacity dip) and `helm rollback` is instant. The invariant that makes that safe —
  **additive-only schema migrations** (expand/contract: `CREATE TABLE IF NOT EXISTS` + nullable/defaulted
  `ADD COLUMN`, never `DROP`/`RENAME` in one release) — is now enforced by `test_migrations_additive.py`
  (3), so a destructive change that would break an old replica mid-rollout fails CI. Runbook in
  production-hardening.md.
- **feat(k8s): enterprise Helm reference topology (item 1.1).** New `platform/infra/helm/examlops` chart
  deploys the control-plane (HPA 3→10 + PDB), dashboard, and agent as **stateless multi-replica** tiers
  behind a TLS ingress — no `container_name` pins, no hostPath, every pod non-root / read-only-rootfs /
  all-caps-dropped and topology-spread across nodes. State stays out of the chart: HA Postgres
  (CloudNativePG) + distributed MinIO are referenced via values; secrets come from an existing Secret
  (never templated). Renders + passes `helm lint` + `kubectl apply --dry-run` (12 manifests); `make
  helm-validate` gates it. Compose is now explicitly dev-only. Chart README documents the full wiring.
- **feat(agent): in-loop circuit-breaker + resilient SSE streaming (items 4.4, 1.7).** `AgentCircuitBreaker`
  (`examlops.agentops`) wires the existing anomaly detection into a live guard: `guard(step)` aborts the loop
  (`CircuitBreakerTripped`) the moment a runaway repeat-loop, step blow-up, all-errors burst, or cost overrun
  appears — so an agent/autopilot can't loop forever or burn unbounded GPU-hours (not just post-hoc detection).
  `examlops.streaming` adds the resilient SSE core (item 1.7): an `EventRing` with resume cursors (Last-Event-ID
  replay of only the missed gap + a truncation signal for full-refresh) and exponential `backoff_delay` with
  jitter to avoid reconnect thundering-herds. `test_agent_circuit_breaker.py` (6) + `test_streaming.py` (6).
- **feat(agent): HA Postgres checkpointer seam (item 1.8).** Single-node Skipper keeps conversation state in a
  SQLite LangGraph checkpointer, so N replicas can't share it. `AGENT_CHECKPOINT_BACKEND=postgres` (+
  `AGENT_POSTGRES_DSN`/`DATABASE_URL`) now selects a shared Postgres checkpointer so replicas survive a host
  loss and load-balance; it degrades to SQLite when unset or the driver/DB is unavailable. The pure backend
  selector (`skipper.checkpoint_backend`) is tested (6); the LangGraph `PostgresSaver` construction is a
  reviewable seam not runtime-verified in the sandbox (needs the extra + a live Postgres), like the 0.1
  Postgres backend.
- **feat(hpc): non-blocking async job wait (item 1.4).** `examlops.hpc_poll.poll_until_complete` replaces the
  adapters' blocking `time.sleep` wait (which pins a worker for a job's whole up-to-24h life) with an async
  poller that `await`s between polls — so one worker shepherds many jobs concurrently — with a per-poll
  callback (progress/heartbeat), a hard timeout, and a sync-or-async status_fn. Injectable sleep/clock →
  `test_hpc_poll.py` (5) runs with zero real waits. Wire behind the `SchedulerAdapter` seam.
- **feat(coordination): externalized locks / idempotency / rate-limit seam (item 1.2).** The control
  plane kept dedup/idempotency/rate-limit state in process memory, so a second replica double-fired
  retrains and re-processed duplicate webhooks. New `examlops.coordination` `Coordinator` seam:
  `try_lock`/`unlock` (leader election), `first_seen` (idempotency dedup), `allow` (fixed-window rate
  limit). The `db` default is backed by `platform_db` (`coord_locks`/`coord_idempotency`/`coord_rate`)
  so it already coordinates across every **process** sharing `platform.db`; a `redis` backend
  (`EXAMLOPS_COORDINATOR=redis`) is the cross-host HA path — a loud skeleton until Redis is wired.
  `test_coordination.py` (8, incl. an 8-replica leader-election exactly-one check).
- **feat(admission): durable admission-control queue with per-tenant fair-share (item 1.5).** Every
  trigger (drift/autopilot/API/webhook) can `admission.submit(...)` instead of dispatching straight to
  Prefect; a worker `drain`s it under a **global concurrency cap** + **per-tenant max-min fairness**, so a
  fleet-wide drift event or one noisy tenant can't starve everyone's cluster share. New `admission_queue`
  table + `claim_next_admission` (fair-share atomic claim under a RESERVED lock, crashed-item TTL reclaim),
  `examlops.admission` facade (`submit`/`worker_step`/`drain`/`stats`), `exa admission submit|stats`.
  `EXAMLOPS_ADMISSION_MAX_RUNNING` (4) / `EXAMLOPS_ADMISSION_PER_TENANT` (2). `test_admission.py` (9, incl.
  the fair-share-beats-flood property + an 8-thread concurrent-cap check).
- **feat(events): NovaFabric event backbone — transactional outbox + publisher seam (item 1.3, foundation).**
  New `event_outbox` table + `examlops.events`: domain code calls `events.publish(topic, payload)` (or
  `enqueue_event(..., conn=)` inside an existing txn so the event commits atomically with the domain
  write); a relay (`events.relay_once` / `exa events relay`) claims unpublished rows under a visibility
  lease and publishes each **exactly once** to the configured `EventPublisher`, marking published/failed
  (failures retained, never dropped). Publisher swappable via `EXAMLOPS_EVENT_PUBLISHER` — default `log`
  is dependency-free; `nats`/`kafka`/`redis` are loud skeletons until wired (same degrade-gracefully
  pattern as the StorageBackend + provider seams). `exa events relay|stats|publish`; first producer wired
  (autopilot cycle-complete). Replaces the O(models×replicas) polling + in-process realtime singleton the
  audit flagged. `test_event_outbox.py` (7, incl. a 4-relay concurrent exactly-once check).
- **perf(hpc): SQL-side fleet capacity aggregation + TTL cache (item 1.9, partial).** New
  `platform_db.aggregate_node_capacity` does a `GROUP BY cluster, state` over the
  `ix_hpc_nodes_cluster_state` index instead of loading every `hpc_nodes` row into Python to sum
  (O(nodes) memory → a per-cluster rollup at fleet scale). `hpc_capacity.capacity_summary` wraps it in
  a short TTL cache (`EXAMLOPS_HPC_CAPACITY_TTL`, default 30s) so a dashboard/NOC-wall refreshing many
  widgets collapses to one query per window; a fresh `record_node_snapshot` invalidates it.
  `test_node_capacity_aggregation.py` (4) proves parity with the canonical `node_capacity`. The async
  bulk `squeue`/`flux jobs` poller half of 1.9 needs live schedulers (deferred).

## [0.37.0] — 2026-07-18

### Fixed / Security — Enterprise-readiness Phase 0 completion wave

- **feat(autopilot): distributed cycle lease closes Phase 0 item 0.12.** A single-row, TTL-based
  `autopilot_lease` (`claim_autopilot_lease`/`release_autopilot_lease`) means only one autopilot cycle
  runs at a time across overlapping cron fires / agent replicas; a crashed holder's lease auto-expires,
  and only the owner can release it. `run_cycle` acquires before the scan (skips + audits if held) and
  releases in a `finally`. Defense-in-depth atop the already-atomic `claim_drift_trigger`.
  `EXAMLOPS_AUTOPILOT_LEASE_TTL` (default 900s). `test_autopilot_lease.py` (7, incl. 8-racer exactly-one).

- **feat(db): central write_retry coverage + retry-exhaustion metric/log (item 0.4).** Every mutating
  `platform_db` helper is auto-discovered by source inspection at import and wrapped in `write_retry`
  (99 helpers), skipping reads and the two that already self-retry (audit hash-chain, atomic drift claim).
  Self-maintaining — new writers are protected the moment they land. Retry exhaustion is now loud, never
  silent: `retry_call` gained an `on_exhausted` hook; `write_retry` logs a WARNING (Loki signal) and
  increments a process counter (`write_retry_exhaustions()`) before re-raising, so a lost write can never
  vanish inside a fire-and-forget caller. `tests/unit/test_write_retry_exhaustion.py` (6).
- **fix(security): fail-closed authz + remove forgeable audit-checkpoint dev-key (item 0.7).** `authz.check()`
  now DENIES + alerts on a backend error (never accidentally allows); `authz._audit()` no longer silently
  swallows dropped security events; `exa audit checkpoint` refuses (exit 1) when no signing key is configured
  instead of falling back to the well-known `examlops-dev-key` (a forgeable checkpoint gives zero
  tamper-evidence). The multitenancy default-deny flip remains deferred to Phase 2.2 (needs identity
  propagation). `test_authz` +2, `test_audit_trail` +1.
- **feat(config): `EXAMLOPS_CONFIG` path override + hermetic config resolution.** `load_config()` resolves its
  file via `config_path()`, honoring `EXAMLOPS_CONFIG` so containers/CI can pin a config and tests never read
  the developer's real `~/.config/examlops/config.toml`. doctor/config/env display the effective path.
- **test(ci): version-consistency gate (item 0.10).** `tests/unit/test_version_consistency.py` asserts root
  `pyproject` == cli `pyproject` == CHANGELOG latest release, and that the version is ≥ the latest git tag —
  killing the `0.33.0` / `0.35.0` / `v0.36.0` drift this release reconciles. All surfaces now read `0.37.0`.
- **perf(observability): drop the version label from hot-path predict metrics (item 3.2/QW5).** `version` was a
  label on `examlops_predict_requests_total` + `examlops_predict_latency_seconds` — an unbounded cardinality
  source (fresh series per promotion × model × alias × status) that would OOM Prometheus at fleet scale. Removed;
  bounded `alias` kept. Per-version visibility preserved as the VALUE of a new `examlops_model_version` gauge.
  `test_metric_cardinality.py` is a source-AST label-budget lint that fails the build if a banned label returns.
- **fix(security): compose hardening — scoped Docker socket + no insecure defaults (items 0.6 + 0.8).**
  The dashboard no longer bind-mounts the raw `/var/run/docker.sock` (root-equivalent host takeover);
  a new `docker-socket-proxy` holds the socket read-only and exposes only `CONTAINERS`+`POST` (EXEC/
  IMAGES/VOLUMES/NETWORKS/SWARM/SECRETS/AUTH all denied), reached via `DOCKER_HOST` (no code change —
  `docker.from_env()` honors it). Insecure literals are gone: `CONTROL_PLANE_TOKEN` no longer defaults
  to `changeme` (empty → fail-closed 503); Grafana admin password + anonymous access are env-driven for
  a secure prod override. The control plane honors `CONTROL_PLANE_ALLOWED_HOSTS` (Host-header scoping).
  Gated by `test_compose_security.py` (4) + guide `docs/guides/production-hardening.md`.
- **feat(backup): consistent `platform.db` backup/restore + tested DR drill (item 0.9).** New
  `examlops.backup` module + `exa backup create|list|verify|restore` take an online, WAL-safe SQLite
  snapshot (SQLite's native backup API — a torn-state-proof alternative to `cp`) with a verifiable
  manifest (sha256 + per-table row counts + audit-chain head). Restore is guarded (refuses to clobber a
  non-empty DB without `--force`) and re-verifies integrity + the audit hash chain after restoring.
  `make dr-drill` + `test_backup_restore.py` (6) run the full backup→wipe→restore round trip in CI (RPO =
  last backup, RTO = restore time). Runbook `docs/guides/backup-restore.md` covers Postgres/MinIO too.
- **feat(observability): real Alertmanager route tree + delivery heartbeat (item 3.4/QW3).** Replaces the single
  black-hole receiver with a severity/cluster/service route tree → PagerDuty (critical) + Slack (warning &
  critical) receivers, inhibition (critical mutes sibling warning; an outage mutes its symptom alerts), and an
  always-firing `Watchdog` rule routed to an external dead-man's-switch that proves the whole
  Prometheus→Alertmanager→receiver path. Secrets are file-based (gitignored, absent ⇒ receiver no-op); Prometheus
  stamps `cluster`/`tenant` external labels. Validated by `amtool`/`promtool` + `test_alerting_config.py` (7).

### Added — Dashboard edit parity (connections · projects · storage)

- **feat(dashboard): bring the CLI's config-setup edit capabilities into the dashboard.** The web
  dashboard was read-heavy (27 of ~40 routers GET-only, incl. connections), so operators had to drop to
  the CLI to create a connection, bind storage, or tear down a project. New write routers close that gap
  by calling the **same `examlops.*` code paths the CLI uses** (not raw-sqlite mirrors), so the dashboard
  can never drift from the CLI and secrets flow through the same store.
  - **Connections** (`routers/connections.py`): `POST` create (s3/uri/dataplane, optional secret written
    through `examlops.secrets` — CLI/serving-compatible, only `hasSecret` returned), `POST /{name}/test`
    reachability probe, `DELETE /{name}`. New `connection.manage` capability; every write audited.
  - **Projects** (`routers/projects.py`): `DELETE /{name}` (cascades membership/resource/storage/pipeline
    rows), `DELETE /{name}/members/{subject}`, `POST /{name}/storage` (provision + bind a connection via
    the shared `ensure_project_storage` / `bind_project_connection` helpers). All `project.manage`, audited.
  - **Frontend**: `lib/connections.ts` + `lib/projects.ts` mutation hooks; `ProjectDetail.tsx` gains a
    New-connection modal, per-row Test/Delete, member Remove, a Provision/Bind-storage modal, and a
    Danger-zone Delete-project — all capability-gated (viewers see read-only, admins see the controls).
  - Enforcement stays at the BFF (F15): viewers get 403 + a human `deny_reason`; no secret value ever
    reaches the browser. Tests: +15 backend (`test_connections_workbenches.py`, `test_projects_writes.py`),
    +10 frontend (`connections.test.ts`, `projects_writes.test.ts`). Design:
    `design/superpowers/specs/2026-07-17-dashboard-edit-parity-design.md`.

### Added — Enterprise-readiness Phase 0.1 (StorageBackend seam · scaffold)

- **feat(storage): land the `StorageBackend` repository seam (item 0.1, scaffold).** New
  `examlops.storage` package puts a dialect-neutral engine seam in front of the datastore so it can
  become Postgres (multi-writer, HA, per-tenant isolation) without touching call sites — the audit's
  single highest-leverage move. Ships the `StorageBackend` protocol, a fully-tested `SqliteBackend`
  (byte-identical current behaviour via `resilience.db`), a `PostgresBackend` **skeleton** (complete
  dialect helpers — `now_expr`/`upsert_sql`/param style — but `connect` fails loudly without
  `psycopg`+DSN and is **not yet runtime-verified**), and `get_backend()` (`EXAMLOPS_DB_BACKEND`).
  `tests/unit/test_storage_backend.py` (7). Migrating the ~221 `platform_db` helpers onto the seam and
  finishing the Postgres driver against a live database is the tracked follow-on.

### Fixed — Enterprise-readiness Phase 0 (foundation / unblock)

- **feat(data): telemetry retention/TTL prune (item 3.6 / QW9).** The per-inference `drift_snapshots` and
  `input_snapshots` tables grew unbounded. New `platform_db.purge_telemetry(days, dry_run, vacuum)` deletes
  rows older than a retention window and optionally VACUUMs — deliberately **excluding** the tamper-evident
  audit log (hash chain) and FinOps cost history. Exposed as `exa data retention-prune --days N [--dry-run]
  [--vacuum]` (audited). `tests/unit/test_purge_telemetry.py` (4 cases).
- **perf(bridge): offload per-inference SQLite writes off the event loop (item 0.11 / QW10).** The
  SeanerBUS bridge made three synchronous `platform.db` writes (drift snapshot, input-embedding snapshot,
  audit event) directly on the asyncio loop for every inference, head-of-line-blocking the hot path while
  they took the write lock. They're now bundled into `_persist_inference_telemetry` and run in a single
  `asyncio.to_thread` hop. `tests/unit/test_seanerbus_bridge.py` (+2 cases).
- **perf(db): index `hpc_nodes(cluster, state)` (Phase 0 bonus win).** Fleet capacity/availability
  queries filter node snapshots by cluster and state; `ix_hpc_nodes_cluster_state` avoids a full scan at
  fleet scale. Covered by `tests/unit/test_schema_once.py`.
- **fix(security): verify SSH host keys instead of blind AutoAdd (Phase 0 SSH-hardening).** The HPC SSH
  transport used `paramiko.AutoAddPolicy`, trusting any host key on first contact — trivial to MITM.
  `SSHExecutor._connect` now loads the operator's system + `~/.ssh/known_hosts` and defaults to
  `RejectPolicy`; auto-add is an explicit dev-only opt-in via `EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS=1`.
  `tests/unit/test_executor.py` (+4 cases).
- **fix(security): control plane rejects placeholder tokens (item 0.8 / QW6).** An unset
  `CONTROL_PLANE_TOKEN` already failed closed (503); a *weak* placeholder like `changeme` — the kind
  that ships in examples/compose files — was still accepted, leaving write endpoints effectively
  unauthenticated. New `_token_is_usable()` also rejects a set of well-known placeholders; `_require_token`
  and the startup self-check (`token: weak`) and `auth_configured` now use it. `tests/test_weak_token.py`
  (8 cases). (Grafana/MinIO compose-default hardening tracked as an infra follow-up.)
- **perf(db): run the schema bootstrap once per process (item 0.5 / QW8).** `init_db()` is called
  defensively by ~165 helpers and re-ran the 100+-table `CREATE TABLE IF NOT EXISTS` script (plus column
  migrations) on every call, churning a write lock on hot read paths. A process-level `_INITIALIZED_PATHS`
  sentinel now short-circuits after the first call per `PLATFORM_DB` path (`:memory:` never cached;
  `force=True` re-runs for tests that drop tables). `tests/unit/test_schema_once.py` (3 cases); full unit
  suite green (1460 passed).
- **fix(db): harden every dashboard SQLite connection (item 0.2 / QW1, dashboard tier).** The dashboard
  is a separate app that can't import the core package; its ~44 bare `sqlite3.connect(...)` call sites
  (30 files) bypassed WAL + `busy_timeout` and produced "database is locked" 500s under concurrency. New
  `platform/services/dashboard/backend/dbconn.py` `connect()` helper carries the same hardening; every
  call site now routes through it, guarded by a dashboard-local `test_no_bare_sqlite_connect.py` (311
  backend tests green, ruff clean).
- **fix(observability): bound trace sampling to 5% by default (item 0.2 / QW4).** A manually-built
  `TracerProvider` samples every trace (`ParentBased(ALWAYS_ON)`), so enabling tracing across the fleet
  firehoses Tempo. `setup_tracing` now builds its sampler via `_build_sampler()`, defaulting to
  `parentbased_traceidratio` at 0.05 (children follow the root decision), clamped to [0,1] and
  overridable through the standard `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` env vars.
  `tests/unit/test_trace_sampler.py` (5 cases).
- **fix(db): ban bare `sqlite3.connect` in the core package (item 0.2 / QW1).** `doctor.py`'s DB check
  now uses the hardened `examlops.resilience.db.connect` (WAL + `busy_timeout`) like every other access,
  and a new CI guard `tests/unit/test_no_bare_sqlite_connect.py` fails the build on any bare
  `sqlite3.connect(` in `examlops` (only the resilience adapter is exempt). The dashboard tier — a
  separate app that can't import the core package — is hardened through its own local `dbconn.connect`
  helper plus a matching guard, removing the "database is locked" 500s under concurrent access.
- **fix(autopilot): cooldown TOCTOU + per-cycle retrain storm cap (item 0.12 / QW7).** The auto-retrain
  cooldown was a read-then-check-then-stamp on a value loaded at cycle start, so two overlapping
  autopilot / `exa drift trigger` cycles could both pass the check and double-fire a retrain. New
  `platform_db.claim_drift_trigger(model, cooldown_s)` performs the check-and-stamp as a **single
  conditional UPDATE** (atomic claim); both the autopilot loop and the drift-trigger prediction+concept
  paths now claim before triggering, so at most one caller wins per cooldown window. Adds a per-cycle
  retrain cap (`EXAMLOPS_AUTOPILOT_MAX_RETRAINS`, default 10) so a fleet-wide drift event can't launch an
  unbounded retrain storm. `tests/unit/test_claim_drift_trigger.py` proves exactly-one-winner under 16
  concurrent claimers. (Distributed cycle-lease is a noted follow-up; correctness is already guaranteed.)
- **fix(audit): hash-chain race under concurrent writers (item 0.3 / QW2).** `write_audit_event`
  read the current chain head and appended the next link on a *deferred* transaction, so two
  writer processes could chain off the same parent and fork the tamper-evident chain
  (`verify_audit_chain` → prev_hash mismatch). New `platform_db._immediate_write()` holds an
  IMMEDIATE (RESERVED) lock across the head-read + append, and the append runs under `write_retry`
  so a lost lock race re-runs the whole transaction instead of corrupting or dropping the event.
  Proven by `tests/unit/test_audit_chain_concurrency.py` (8×12 barrier-synchronized writers → one
  contiguous, valid chain). Restores audit tamper-evidence under the multi-writer reality.

### Added — Project Anatomy (Increment 2: storage · pipelines · unified view)

- **feat(projects): per-project MinIO storage (P6, ADR 0091).** Each project gets a stable location
  `s3://$EXAMLOPS_PROJECTS_BUCKET(=examlops-projects)/<project>/{artifacts,datasets,cache}/`. New
  additive `project_storage` table + helpers (`ensure_project_storage`, `get_project_storage`,
  `bind_project_connection`, `set_project_usage`, `refresh_project_usage`, `projects_bucket`,
  `project_experiment`); `exa project storage <name> [--bind-connection <conn>] [--refresh]`. Training
  routes into the project prefix via a per-project MLflow experiment (`project/<name>`) —
  `pipeline_generator` sets it automatically, fail-open. `minio-init` provisions the projects bucket.
  Binding a P2 connection sets the bucket from the connection; the secret is never copied.
- **feat(projects): two pipeline surfaces (P7, ADR 0092).** A project exposes one **Prefect** training
  surface (deployments tagged `project:<name>`) and one **Ray Serve** serving surface (models where
  `resolve_project == <name>`, joined with traffic split), aggregated over existing state. New additive
  `project_pipelines` registry keyed `(project, kind ∈ prefect|rayserve)` + `upsert_project_pipeline` /
  `get_project_pipelines`; `exa project pipelines <name>`.
- **feat(projects): unified anatomy view (P8, ADR 0093).** `get_project_full` now assembles
  `storage` + `connections` (secret-safe: `has_secret` only) + `pipelines` alongside
  resources/members/quota/budget/consumption, every source fail-open. `exa project show` renders the
  full pane; dashboard `GET /api/v1/projects/<name>` returns the extended anatomy with Storage +
  Pipelines cards on the project detail page.
- **test:** `tests/unit/test_project_anatomy.py` (16), dashboard `test_projects_anatomy.py` (3),
  frontend `projects.test.ts` (+3: `storageUsagePct`, `bytesToGb`, `pipelineToken`).
- **docs:** new guide `docs/guides/project-anatomy.md`; architecture `docs/architecture/project-workspace.md`
  updated to shipped status.

### Added — Projects & Workspaces cross-cutting (agent + dashboard)

- **feat(mcp): project tools.** `examlops.mcp.tools` now exposes 5 agent-callable Projects tools —
  reads `project_list`, `project_detail`, `project_cost`; gated writes `project_assign_model` and
  `project_add_member` (both behind `_agent_write_gate` + `EXAMLOPS_MCP_ALLOW_WRITES`, audited). An
  agent can now inspect and curate the workspace graph under the same least-privilege policy as the
  other write tools. `tests/unit/test_mcp_project_tools.py` (7 tests).
- **feat(dashboard): Connections + Workbenches surfaces.** New `routers/connections.py` (read-only
  list — never returns a secret value, only `hasSecret`; creation stays CLI-only so credentials go
  through the D7 secrets client) and `routers/workbenches.py` (viewer list + admin RUNNING/STOPPED
  status flip, audited, `project.manage`-gated). The frontend renders both as project-scoped sections
  on the project detail page (`lib/connections.ts`, `lib/workbenches.ts`, `ProjectDetail.tsx`).
  Backend `tests/test_connections_workbenches.py` (5 tests); frontend 7 new vitest cases (263 green).
- **test(projects): P1–P5 integration guard.** `tests/unit/test_projects_e2e.py` walks one workspace
  lifecycle end-to-end — create → named connection (secret to the secrets client, only a ref in the
  DB) → member grant → workbench (connection env-injection) → cost attribution → budget breach →
  project resolution — so the five increments stay wired together.

### Added — Next-Gen 40 dashboard surface + public docs

- **feat(dashboard): Next-Gen 40 console (`/nextgen`).** New `pages/NextGen.tsx` + `lib/
  nextgen.ts` render the `/api/nextgen/*` data: a summary KPI row plus federated-run (E7),
  device-pool, placement-decision, and governed-cloud-burst (E8) tables. Honest privacy
  labels (DP off → "no DP", secure-agg off → "updates visible"), colour-blind-safe status
  tokens for placements/bursts, and per-panel EmptyStates so it degrades when a feature is
  unexercised. Wired into the sidebar nav + command palette. tsc + eslint clean; 6 new vitest
  cases (256 frontend tests green); production build verified.
- **feat(dashboard): Next-Gen 40 read API (`/api/nextgen/*`).** New `routers/nextgen.py`
  surfaces the newest Next-Gen 40 platform.db tables to the dashboard: federated runs +
  detail (E7), device pools / placement decisions / burst events (E8), autoscale config +
  scale events (E5), distributed runs (E6), inference-gateway config (E4), feature views
  (A3), and a `/nextgen/summary` roll-up. Viewer-gated and fail-open (empty result, never a
  500, when a table/DB is absent) — same contract as the other platform-data routers. 9 tests.
- **docs(nextgen-40): public tour tutorial + full guide navigation.** New `docs/tutorials/
  nextgen-40-tour.md` — a hands-on, offline-runnable walkthrough with command examples
  verified against the live CLI. All ~40 feature guides + the generated CLI reference are
  wired into the mkdocs nav under new **Next-Gen 40** and **Tutorials** sections.
- **docs(nextgen-40): regenerated CLI reference** (`docs/reference/cli-generated.md`) covering
  every Next-Gen 40 command group (federated, hardware, feature, assets, reproduce, embedding,
  distributed, policy, …) from the live Typer tree.

### Added — Projects & Workspaces roadmap P2–P5 (RHOAI-inspired, ADRs 0087–0090)

Completes the Projects initiative: all four roadmap features shipped on top of the P1 unified
workspace. Additive · test-backed (88 project/authz tests) · back-compat preserved.

- **feat(projects): P2 Named Connections (ADR 0087)** — `examlops.connections` package: reusable,
  project-scoped data connections (S3/URI/dataplane). Non-secret config inline; credentials only in
  the D7 secrets client via `secret_ref` (never in the DB); registers as a project resource. CLI
  `exa connection create|list|show|test|delete` (test = read-only reachability probe, never prints
  secrets).
- **feat(projects): P3 project-scoped serving & pipelines (ADR 0088)** — `examlops.project_scope`
  pure helpers; `ModelInfo.project` on `GET /models`; Prefect deployments tagged `project:<name>`;
  optional model-YAML `project:` field; `exa pipeline run --project` (attributes cost via P1).
- **feat(projects): P4 project FinOps & monitoring (ADR 0089)** — `examlops.project_finops`: direct
  `model_costs.project` cost + carbon attribution (closes finops.py:102) and budget/quota breach
  detection → governance audit event. CLI `exa project cost|budget` (budget exits 1 on breach).
- **feat(projects): P5 Project Workbenches (ADR 0090)** — `examlops.workbenches`: on-demand,
  project-bound dev environments that inject the project's Named Connections (P2) as env vars; spawn
  behind a runtime seam (JupyterHub/Docker). CLI `exa workbench create|list|start|stop|delete`.
- ADRs 0087–0090 flipped Proposed→Accepted. Deferred (need a live runtime): Prometheus `project`
  metric label, dashboard Connections/Workbenches tabs, actual JupyterHub spawn.

### Added — Next-Gen 40 wave 13 (serving/infra: heterogeneous HW & hybrid HPC↔cloud) — **NEXT-GEN 40 COMPLETE (40/40)**

- **E8 — heterogeneous hardware & hybrid HPC↔cloud (ADR 0041).** `examlops.hardware` — a
  workload declares its device need **neutrally** (`accelerator` ∈ nvidia|amd|intel-gaudi|tpu|cpu
  + capability tags + optional `target` hpc|cloud + `engine`), and `place()` selects the
  **best-available compatible** device across HPC + cloud pools. A **portability check** gates the
  engine against the accelerator's backend (cuda/rocm/ipex/xla/cpu) and **rejects** an
  incompatible placement with a clear error — never silently scheduled (R4/GWT-2). **Honest
  fallback**: an unavailable accelerator falls back to another engine-compatible one, flagged; a
  sub-1.0 fraction on a vendor without MIG gets a whole device, flagged (R3/GWT-3). Cheapest
  eligible pool wins. **Governed cloud bursting** (`plan_burst`) is opt-in and residency-checked —
  `no-egress`/`eu-only`→non-EU is **blocked + audited**, data movement explicit (R5/GWT-4; D4/D6).
  Device type + region flow into cost + carbon via `device_accounting` (R6/GWT-5). Pure-Python;
  degrades to CPU + mocked pools with no vendor SDK. CLI `exa hardware add-pool|pools|place|
  portable|burst|decisions`. New `device_pools` / `placement_decisions` / `burst_events` tables.
  Guide `docs/guides/heterogeneous-hardware.md`. 13 tests. ADR 0041 Accepted.

### Added — Next-Gen 40 wave 12 (training: federated & privacy-preserving)

- **E7 — federated & privacy-preserving training (ADR 0040).** `examlops.federated` — trains
  across sites that **cannot share raw data**: a coordinator drives rounds, each site trains
  locally, and only **signed model updates** (never raw data/PII) are aggregated via **FedAvg**
  (sample-weighted mean), **FedProx**, or a Byzantine-robust **trimmed mean** (`robust` drops
  the per-coordinate min+max; R2/GWT-6). Optional **differential privacy** tracks an honest
  (ε, δ) budget by basic composition and claims **nothing** when off (R3/R5/GWT-3/GWT-5);
  optional **secure aggregation** hides per-site updates from the coordinator (R4/GWT-7).
  Sites authenticate + sign (D3); unauthorized/unsigned participation is **rejected and audited**
  (D4; R6/GWT-4) — the loop never aggregates a site it can't attribute. Pure-Python and testable
  with no Flower/Opacus/mTLS. CLI `exa federated init|round|budget|status`. New `federated_runs`
  / `federated_sites` / `federated_rounds` tables. Guide `docs/guides/federated-training.md`.
  12 tests.

### Added — Next-Gen 40 wave 11 (serving: inference gateway & KV routing)

- **E4 — inference gateway & KV-cache-aware routing (ADR 0039).** `examlops.inference_gateway`
  — routes LLM requests to the engine replica most likely to already hold their KV/prefix
  cache (**affinity**), falling back to **load-aware** routing (queue depth + KV utilization
  + latency) when there is no affinity, and to plain **round-robin** by default (cache-aware
  is opt-in; R1/R2/R3). SLO-breaching replicas are avoided (`slo_latency_ms`; R6/GWT-5) and
  unhealthy replicas excluded, always with a never-route-to-nothing fallback. Optional
  prefill/decode **disaggregation** (`disaggregated_route`) splits pools with identical
  output (R4/GWT-4). `prefix_key` hashes system prompt + session + RAG context. Routing is a
  **pure function** — `measure_hit_rate` proves cache-aware beats round-robin (GWT-1) with no
  engine/GPU/K8s. Persisted routing events feed hit-rate/decision metrics. CLI
  `exa serve routing set|simulate|stats`. New `inference_gateway_config` / `routing_events`
  tables. Guide `docs/guides/inference-gateway.md`. 11 tests.

### Added — Next-Gen 40 wave 10 (LLMOps: structured output & reasoning)

- **B8 — structured output & reasoning ops (ADR 0035).** `examlops.structured` — two gateway
  (B2)/engine (E2) capabilities. **Structured output**: `generate_structured` validates a
  generation against a JSON Schema and on the rare invalid output **repairs** (coerce scalar
  types, drop unknown props, fill required) and retries before failing explicitly — so agent
  tool-call args and RAG citations are **guaranteed valid** (R1/R2/GWT-1/GWT-2). Uses
  `jsonschema` when installed, degrades to a built-in validator. Every attempt is metered
  (valid/repaired/failed, R8). **Reasoning ops**: `ReasoningBudget.enforce` cuts thinking
  off at the per-request limit (R4/GWT-4); `account_reasoning` records reasoning vs output
  tokens/cost **separately** for C1/FinOps (R5/GWT-5); `capture_reasoning_trace` stores
  traces **redacted** (D8), **TTL'd**, and **tenant-scoped** (R6/GWT-6). CLI
  `exa gateway schema test` + `exa gateway reasoning account|budget|stats`. New
  `reasoning_usage` / `reasoning_traces` / `structured_output_events` tables. Guide
  `docs/guides/structured-output-reasoning.md`. 10 tests.

### Added — Next-Gen 40 wave 9 (LLMOps: embedding lifecycle)

- **B6 — embedding lifecycle & reindexing (ADR 0043).** `examlops.embeddings` — governs the
  embeddings the B3 cache / B4 RAG / B5 vector store rely on. **Versioned encoders**
  (content-addressed `encoder_id` over name/version/dim/metric/normalization; R1). A
  **compatibility guard** refuses any similarity comparison across different `encoder_id`s
  (`EncoderMismatchError`, never silently computed; R3/GWT-2). **Blue-green reindex**: build
  a staging index → re-embed → **verify recall** against a floor → **atomic switch**,
  retaining the old index until confirmed then pruning it; a below-floor recall **aborts**
  and keeps the old index (R4/R5/GWT-3/GWT-4). After a switch, input-embedding **drift
  baselines are rebaselined** (C5, since the space changed; R6/GWT-5). Encoder changes are
  audited (D4) and per-tenant collections reindex independently (D6). CLI
  `exa embedding register|list|set-encoder|reindex|status`. New `encoders` /
  `embedding_collections` / `reindex_jobs` tables. Pure-Python (no encoder model/GPU/vector
  DB). Guide `docs/guides/embedding-lifecycle.md`. 10 tests.

### Added — Next-Gen 40 wave 8 (training: distributed & fault-tolerant)

- **E6 — distributed & fault-tolerant training (ADR 0032).** `examlops.distributed` —
  multi-GPU/multi-node training (FSDP / DeepSpeed ZeRO / Megatron) launched through the
  phase-23 scheduler abstraction with **automatic sharded checkpoint/resume**.
  `launch_distributed` derives a torchrun/elastic rendezvous from the scheduler node list
  (degrades to localhost/mock — CPU gloo in CI) and emits the real `torchrun` command;
  strategy is selectable (R2/GWT-4). `write_checkpoint` writes an **integrity-hashed**
  sharded checkpoint keyed by run, linked to MLflow/A1/A2 (R3/R5). `resume_from_checkpoint`
  returns the **last integrity-valid** checkpoint so a resubmitted job resumes from
  step/epoch + optimizer state rather than restarting — and **refuses a corrupt** one,
  falling back to the previous valid checkpoint (R4/GWT-3/GWT-5). Failures/resumes are
  audited (D4), resume count tracked, per-run cost recorded (R7). CLI
  `exa pipeline distributed launch|checkpoint|resume|status|list`. New `distributed_runs` +
  `training_checkpoints` tables. Pure-Python (no GPU/torch/scheduler needed). Guide
  `docs/guides/distributed-training.md`. 11 tests.

### Added — Next-Gen 40 wave 7 (governance: policy-as-code)

- **D5 — policy-as-code governance (OPA/Rego) (ADR 0029).** `examlops.policy_engine` — a
  `PolicyEngine` seam over structured input (`PolicyInput{subject, resource, action,
  context}`) evaluating governed decisions with `{allow, reasons}`. Layers **on top of** the
  existing ADR-0079 YAML engine rather than replacing it: `YamlPolicyEngine` (default)
  delegates to `examlops.policy.decide`; `RegoPolicyEngine` shells to the `opa` binary when
  present + selected (`EXAMLOPS_POLICY_ENGINE=opa`) and **degrades** to YAML otherwise.
  **Fail-closed** on engine error for security-critical decisions (supply_chain/deploy/
  budget/tenancy), monitor/fail-open for the rest (R4). **Domain gates** encode built-in
  default-deny a bundle can only tighten: `supply_chain_gate` (unsigned artifact → deny,
  GWT-4), `budget_gate` (over-budget GPU → deny, GWT-3), `card_gate` (model-card
  completeness floor). **Signed, versioned, per-tenant policy bundles** (`sign_bundle`/
  `verify_bundle` via the D3 HMAC key; base `policy.yaml` + `policy.<tenant>.yaml` overlay;
  tamper-detected). Every decision audited (D4) with tenant scope. CLI `exa policy eval`
  (structured, `--dry-run` default) + `exa policy bundle sign|verify|list`. New
  `policy_bundles` table. Guide `docs/guides/policy-as-code.md`. 14 tests.

## [0.35.0] — 2026-07-16

### Added — Next-Gen 40 wave 6 (LLMOps: fine-tuning)

- **B7 — fine-tuning / PEFT / multi-LoRA serving (ADR 0044).** `examlops.finetuning` — a
  PEFT/LoRA (and full-FT) workflow producing **versioned, signed, eval-gated,
  lineage-linked adapters** served multi-LoRA on a shared base. `finetune(base, method,
  dataset_rev)` (`lora`/`qlora`/`full`) registers a first-class adapter recording base ref,
  rank, training dataset revision, and eval score — signed with the D3 HMAC key (degrades
  to unsigned+marked), A2 lineage-linked, cost-recorded. `promote_adapter` enforces a **C3
  eval-gate** (below-floor adapters cannot be promoted; R2). `MultiLoRARouter` loads a base
  **once** and serves many adapters selected per-request by id, with an **LRU hot set**
  bounding memory (R5) and **refusal** of an adapter whose recorded base ref ≠ the serving
  base (R4). CLI `exa finetune <base>` + `exa serve adapter add|list|promote|route`. New
  `lora_adapters` table. Pure-Python (no HF PEFT/TRL/GPU/engine needed to register, gate,
  or route). Guide `docs/guides/fine-tuning.md`. 11 tests.

### Added — Next-Gen 40 wave 5 (feature store / train-serve consistency)

- **A8 — signed reproducibility bundles (ADR 0038).** `examlops.reproducibility` — a signed
  **bundle** manifest captures every input to a model version (code commit, A1 dataset
  revision, A3 feature-view versions, resolved env = uv.lock sha256 + image digest,
  hyperparameters, scheduler resources + hardware, RNG seeds, A2 lineage). `build_bundle`
  hashes the canonical manifest and signs it with the D3 HMAC key (degrades to **unsigned +
  marked** when no key — honesty applies to signing too), versioned + audited (D4).
  `reproduce` documents the rebuild plan and metric-matches re-observed metrics within a
  **documented relative tolerance**, never claiming bit-exactness (GPU/kernel
  non-determinism is called out; R4/GWT-3). `verify_bundle` checks referenced inputs still
  exist and hashes match — flagging a rotted bundle (purged dataset, moved commit,
  changed lockfile; R5/GWT-4). `technical_evidence` shapes it for D1 docs / D2 evidence.
  CLI `exa reproduce build|run|verify|list` (`verify` exits 1 on rot — CI gate). New
  `repro_bundles` table. Guide `docs/guides/reproducibility-bundles.md`. 11 tests.
- **A4 — declarative asset-centric pipelines (ADR 0036).** `examlops.assets` — an
  asset-centric layer over the existing Prefect orchestration. Datasets/features/models are
  declared as **assets** via an `@asset(deps=[...])` decorator (or `declare_asset`); the
  platform builds the asset DAG (coincident with the A2 lineage graph), tracks each asset's
  materialized version against the **upstream versions it was built from**, and reports
  staleness transitively (R3). `materialize(name)` rebuilds **only** the target + its stale
  ancestors, dependencies-first (R4 — selective/incremental). Engine behind an
  `AssetOrchestrator` seam (thin-over-Prefect default / Dagster); `exa pipeline run` is
  untouched (R2). Each materialization emits OpenLineage (A2), is policy-governed (D5 —
  a deny blocks the run), and audited (D4). CLI `exa assets declare|list|status|materialize|
  source-changed|graph`. New `assets` + `asset_materializations` tables. Pure-Python (no
  Prefect/Dagster needed to declare, compute freshness, or run in-process). Guide
  `docs/guides/asset-pipelines.md`. 11 tests.
- **A3 — feature store & train/serve consistency (ADR 0017).** `examlops.feature_store` —
  one feature-view definition serves both training (offline, point-in-time) and inference
  (online, low-latency), eliminating train/serve skew. Reference target is Feast (MinIO
  offline + Redis online); implemented with **identical semantics in pure Python** over
  `platform.db` so it works and is fully testable with no Feast/Redis. Guarantees:
  single definition (R1, values projected to the view's declared features); **zero skew**
  (R2 — the online value is exactly the latest offline value as-of the entity's event
  time); **point-in-time** historical retrieval with no future leakage (R4/R5); A1 dataset
  revision pin (R4); materialization + freshness/staleness monitoring vs TTL (R6). CLI
  `exa feature apply|list|ingest|materialize|get|skew|freshness`. New `feature_views` /
  `feature_records` (offline log) / `online_features` (materialized snapshot) /
  `feature_view_materializations` tables. Guide `docs/guides/feature-store.md`. 10 tests.

### Added — Next-Gen 40 wave 4 (governance evidence + GPU sharing)

- **E3 — GPU sharing & fractional allocation (ADR 0030).** `examlops.gpu_sharing` —
  capability-aware mechanism selection (`select_mechanism`: MIG hardware-isolation →
  time-slice soft-isolation → whole-GPU) with **honest fallback** (a sub-1.0 fraction on a
  non-fractional cluster rounds up to a whole GPU and surfaces the wasted capacity — never
  silently faked); first-fit-decreasing `bin_pack` of fractional asks onto whole GPUs;
  `fractional_gpu_hours` accounting; isolation-level surfacing. New `gpu_allocations` table.
  CLI `exa hpc gpu-share plan|pack|accounting`. Pure-Python (no GPU required). 10 tests.
- **A6 — Croissant dataset metadata & structured model cards (ADR 0037).** `examlops.cards`
  — `croissant_record`/`validate_croissant` (JSON-LD dataset card mapping the real FData
  columns pclass/mbwidth/embedding, license + provenance, validated against a pinned spec);
  `build_model_card` auto-populates a structured governance card from live data (intended
  use + risk class from D1, dataset revision from A1, metrics from C2 eval, fairness from
  C8, lineage from A2) with explicit **"not provided"** for gaps — never fabricated (R4);
  `card_completeness` scores the card for the D5/C3 promotion gate. New `dataset_cards` +
  `model_card_records` tables (versioned). CLI `exa cards dataset|model|completeness`.
  Feeds D1 docs + D2 evidence. Guide `docs/guides/cards.md`. 10 tests.
- **E5 — autoscaling & scale-to-zero (ADR 0031).** `examlops.autoscale` — a **pure**
  `decide_scale` function (current replicas + observed metric + policy + clock → decision)
  that scales toward the replica count meeting a target metric, scales to zero on idle
  (respecting a warm pool), and resists thrashing with stabilization + cooldown windows;
  GPU-fraction-aware (E3). Scale events are audited (D4) via `apply_scale`; measured
  cold-start times surface to C6 SLOs (`cold_start_seconds`); scale-to-zero windows feed
  FinOps (`scale_to_zero_savings`). Same logic can drive a KEDA/Knative generator or an
  in-process controller — no cluster required. New `scale_events` table + richer policy
  columns on the Phase-24 `autoscale_config` stub (backward-compatible helper). CLI
  `exa serve autoscale set|simulate|status|savings|record`. Guide
  `docs/guides/autoscaling.md`. 11 tests.

## [0.34.0] — 2026-07-16

### Added — Next-Gen 40 wave 3 (C-track observability complete: C4–C8; D1/D2/D4 governance)

- **D4 — Immutable, tamper-evident audit trail (ADR 0028).** `write_audit_event` now
  hash-chains every event (`prev_hash` + `hash = SHA256(prev_hash ‖ canonical(event))`);
  DB-level triggers make `audit_events` append-only (UPDATE/DELETE blocked, R3);
  `verify_audit_chain` recomputes the chain and identifies the first broken link (R2);
  `sign_audit_checkpoint` signs the chain head (D3 HMAC, R5); `export_audit_events` gives
  read-only archival export (R4). Events carry actor + tenant + resource (R7). New
  `audit_checkpoints` table + `prev_hash`/`hash`/`tenant` columns (idempotent migration).
  CLI `exa audit verify|checkpoint|checkpoints|export` (bare `exa audit` log view preserved).
  Fully backward-compatible (1234/1234 suite green). Guide `docs/guides/audit-trail.md`. 10 GWT tests.
- **D2 — NIST AI RMF control backbone (ADR 0027).** `examlops.governance` — versioned
  `catalogue.yaml` (Govern/Map/Measure/Manage + GenAI Profile, 10 controls with EU-AI-Act +
  ISO/IEC-42001 crosswalk); `load_catalogue`, `validate_mapping` (CI gate: every control
  references a real evidence collector), `governance_report` (satisfied/partial/gap per
  control from **live** evidence — a missing source is always a gap, never a false pass),
  `crosswalk`. Shares D1's evidence-collector layer (one evidence pass, two frameworks;
  D1 gained `data_governance`/`risk_management`/`changes` collectors). Reports versioned,
  audited (D4), tenant-scoped (D6). CLI `exa governance catalogue|validate|report|crosswalk`.
  Evidence coverage, not certification. Guide `docs/guides/governance-nist-rmf.md`. 10 GWT tests.
- **D1 — EU AI Act compliance tooling (ADR 0012).** `examlops.compliance` —
  `classify_system` (risk tier + intended purpose), `generate_technical_file` (Annex-IV
  document assembled from **live** evidence across A2/C2/C8/D3/C5/C6/D4, flagging missing
  sections rather than omitting them, versioned + disclaimer on every surface),
  `check_art12_logging` (audit-trail coverage report), and a validated conformity state
  machine (`draft→documented→assessed→declared`). Reusable **control→article→evidence**
  `FRAMEWORK` (shared with D2/NIST RMF). In-scope-but-unclassified models are blocked from
  promotion (R2, `--force`-overridable + audited). New `compliance_systems` +
  `technical_files` tables. CLI `exa compliance classify|technical-file|art12|status|
  declare|framework`. Not legal advice. Guide `docs/guides/eu-ai-act-compliance.md`. 10 GWT tests.
- **C8 — Fairness & subgroup performance monitoring (ADR 0025).** `examlops.fairness` —
  `slice_metrics` (per-slice accuracy/error/selection-rate/TPR/FPR; Fairlearn `MetricFrame`
  when installed, pure-Python otherwise), `fairness_disparity` (demographic-parity diff,
  equalized-odds diff, selection-rate/accuracy range), `fairness_gate` for C3. Min-sample
  noise guard excludes tiny slices (R3). Disparity above threshold gates promotion under
  `EXAMLOPS_FAIRNESS_GATE_ENABLED` (audited, `--force`-overridable). New `fairness_config` +
  `fairness_samples` tables. CLI `exa fairness config|slice|report`. Tenant-scoped (D6),
  audited (D4). Guide `docs/guides/fairness.md`. 10 GWT tests.
- **C7 — Shadow deployment & champion-challenger (ADR 0024).** `examlops.champion_challenger`
  — `enable_shadow`/`challenger_status`/`maybe_promote`; isolated shadow inference
  (`run_shadow` swallows shadow crashes so production is unaffected; `shadow_context`
  + `guard_write`/`ShadowWriteError` make shadows side-effect-free); champion-vs-challenger
  scoreboard scored with the phase-24 Welch t-test (`analysis/ab_stats`, pure-Python
  z-approx fallback) as labels arrive; promotion proposed via C3 only on a significant win
  (Δ≥min, p<α, N≥min) with **no C6 SLO regression**. New `challenger_config` +
  `challenger_samples` tables. CLI `exa serve challenger enable|status|promote|disable|list`.
  Audited (D4), tenant-scoped (D6). Guide `docs/guides/shadow-champion-challenger.md`. 10 GWT tests.
- **C6 — Model-quality SLOs/SLIs & burn-rate alerting (ADR 0023).** `examlops.slo` —
  declarative OpenSLO-style specs (`slo_specs`, versioned + per-tenant); `generate_rules`
  emits promtool-valid Prometheus recording rules + multi-window/multi-burn-rate
  Alertmanager rules (Google-SRE 2-window: fast burn pages, slow burn tickets);
  `slo_status`/`budget_exhausted` compute live SLI, remaining error budget, and burn rate
  from `slo_samples`. Budget exhaustion gates promotion (C3) when
  `EXAMLOPS_SLO_GATE_ENABLED` is set (audited, `--force`-overridable). CLI `exa slo
  set|apply|list|status|generate|burn|record`. Guide `docs/guides/slos.md`. 11 GWT tests.
- **C5 — Advanced drift: concept / label-free perf / data-quality (ADR 0022).**
  `examlops.drift_advanced` — `detect_concept_drift` (mean-shift z-test on realized
  error as delayed labels arrive → `drift_kind=concept`), `estimate_performance`
  (CBPE-like label-free accuracy estimate; large drop **warns**, never force-retrains),
  and `profile_inference` (schema/null/range/cardinality profile + A5 bad-payload fold →
  `drift_kind=data_quality`). New unified `drift_events` (drift_kind discriminator) +
  `perf_estimates` tables. Concept-CRITICAL is auto-retrain consumable (cooldown-aware,
  wired into `exa drift trigger`). CLI `exa drift concept|estimate|profile|events`.
  13 GWT tests. Degrades to pure-Python stats (Evidently/River/NannyML/whylogs optional).
- **C4 — AgentOps: agent trace & tool-call analytics (ADR 0021).** `examlops.agentops` —
  `record_session`/`SessionRecorder` aggregate a Skipper run into additive `platform_db`
  tables (`agent_sessions`, `agent_tool_calls`); `tool_success_rate`, and `detect_anomalies`
  for reasoning loops / step blowups / cost overruns / error bursts. Tool args are stored
  **PII-redacted** (D8) as a hash digest — never raw; dangerous-tool calls
  (`trigger_retrain`/`promote_model`/…) are audited to `audit_events` (D4); tenant-scoped
  (D6). CLI `exa agentops tools|sessions|replay|anomalies`. Guide `docs/guides/agentops.md`.
  13 GWT tests. Degrades to pure SQLite (no OTel collector required).

### Added — Projects & Workspaces (Increment 1, RHOAI-inspired, ADR 0086)

Elevates the partial ADR-0084 `projects` primitive into ExaMLOps's single canonical *workspace*,
consolidating four fragmented grouping primitives (projects / namespaces / `authz_relations` / F15
tenant claim) into one key. Additive · test-backed (GWT) · back-compat preserved.

- **feat(projects): unified data model** — generic `project_resources(project,kind,ref)` membership
  (kind ∈ model|pipeline|serving_endpoint|connection|dataset|storage) with model dual-write to
  `project_models`; people membership + permissions via the existing D6 `authz_relations`
  (owner⊇editor⊇viewer, no new ACL table); additive `model_costs.project` column (idempotent
  `_COLUMN_MIGRATIONS`) completing per-project cost attribution; `get_project_consumption` unions
  `project_resources`∪`project_models`∪`namespace_models`; `get_project_full` anatomy.
- **feat(projects): CLI** — `exa project assign --kind`, `members`/`add-member`/`remove-member`,
  full-anatomy `show`, and `use`/`current` active-project context (`_config.active_project`,
  `EXAMLOPS_PROJECT` precedence).
- **feat(projects): dashboard** — `routers/projects.py` (`/api/v1/projects` list + `/{name}` anatomy
  + admin writes), `PROJECT_MANAGE` capability, `projectsConsole` flag, and a React Projects console
  (`Projects.tsx`/`ProjectDetail.tsx` + `lib/projects.ts`).
- **docs(projects):** guide `docs/guides/projects-workspaces.md`; CLAUDE.md phase-41 row + commands;
  `docs/reference/commands.md` Projects section. Design set: SoA dossier
  `design/vision/library/rhoai-soa-projects.md`, Vision Card (GO 24/25), ADRs **0086–0090**, specs
  **P1–P5**, plan `.claude/plans/projects-workspace/`. Roadmap: P2 Named Connections · P3
  project-scoped serving/pipelines · P4 project FinOps/monitoring · P5 workbenches.

## [0.33.0] — 2026-07-16

### Added — Next-Gen 40 wave 2 (RAG cluster + guardrails)

Four more Next-Gen 40 features, same discipline (additive · GWT-tested · CI-gated · graceful
degradation). The B-track RAG cluster (B3+B5+B4) plus the D8 safety layer it depends on.

- **B5 — Vector DB & embedding store (ADR 0020).** `examlops.vector_store` — `VectorStore`
  seam; `SqliteVectorStore` (persistent, dependency-free, Python-computed cosine/l2/dot)
  default fallback + lazy `PgVectorStore`; fixed-dim collections (rejection on mismatch),
  metadata-filtered top-k, per-tenant isolation (D6), blue-green reindex (B6 hook),
  index/latency metrics. `exa vector create/upsert/search/reindex/stats`. Guide
  `docs/guides/vector-store.md`.
- **B3 — Semantic caching (ADR 0018).** `examlops.semantic_cache` — cosine cache over
  pluggable embeddings (local-embedder/Redis prod, token-hash fallback), namespace isolation
  (`tenant :: model | params`), temperature/no-cache/side-effect bypass, TTL + max-size
  eviction, measured savings; `bind_to_gateway` wires the B2 hooks. `exa gateway cache stats`,
  `exa gateway chat --cache`. Caching section in `docs/guides/model-gateway.md`.
- **B4 — RAG pipeline & retrieval ops (ADR 0019).** `examlops.rag` — ingest
  (chunk→embed→index into B5, versioned against A1) + query
  (embed→retrieve→rerank→assemble→generate) citing chunks; pluggable reranker, RETRIEVER span
  (C1), per-tenant isolation, gateway-routed generation (B2) with optional B1 prompt, D8
  guardrail seam over untrusted content; `context_precision/recall` for C2. `exa rag
  ingest/query/list`. Guide `docs/guides/rag.md`.
- **D8 — Guardrails / safety / PII defense (ADR 0026).** `examlops.guardrails` — injection
  block, PII detect+redact (Presidio fallback = regex), secret-leak defense (D7), toxicity
  moderation, per-tenant tool allow-list; `off/monitor/enforce` modes with fail-closed
  enforcement; every block/redact audited (D4). `exa guardrails test/check-tool/stats`. Guide
  `docs/guides/guardrails.md`.

### Fixed

- **`exa project` refactor completion.** Restored a dropped `list_project_models` import
  (F821 that failed the ruff CI gate) and its `active_project`/`set_active_project` +
  `PROJECT_MANAGE` companions, so `exa project show/compose` work and the CI gate is green.

## [0.32.0] — 2026-07-16

### Added — Next-Gen 40 implementation wave (MVP slice: A1→A2 · C1→C2→C3 · B1→B2 · E2→E1, + governance/security roots)

Twelve Next-Gen 40 features moved from design → implementation, each additive, test-backed
(GWT specs), CI-gated (ruff), and guarded by graceful degradation (every optional external
dependency falls back to a local/pure-python path that works with no service). All new state
lives in additive `platform_db` tables.

- **C1 — GenAI observability (ADR 0006).** `examlops.telemetry.genai` — OpenTelemetry GenAI
  semconv spans (`gen_ai.*` + `examlops.cost.usd`), token/cost recording, content-capture gated by
  `EXAMLOPS_GENAI_CAPTURE_CONTENT` with a D8 redactor seam. No-op when `OTEL_SDK_DISABLED`.
  `exa genai check|cost`. Guide `docs/guides/genai-observability.md`.
- **A5 — Data contracts & quality (ADR 0005).** `pipelines/contracts` — pure-pandas
  `DataContract`/`QualityResult` (pandera-optional), check builders, and `validate_request()` for a
  4xx inference gate. `exa data validate`. Guide `docs/guides/data-quality.md`.
- **B1 — Prompt management (ADR 0009).** `examlops.prompts` — immutable versioned prompt templates
  + moving labels (dev/staging/prod), `render()` with variable validation (treats vars as data),
  30s-TTL cache + last-known-good fail-safe. `exa prompt create|list|show|diff|label|rollback`.
  Guide `docs/guides/prompt-management.md`.
- **D7 — Secrets management (ADR 0011).** `examlops.secrets` — OpenBao → Fernet-local → env
  resolution (fail-fast), tenant path-scoping, `scan_text` secret detection. `exa secrets
  set|get|rotate|list|scan` + a `sanity:secret-scan` CI job. Guide `docs/guides/secrets.md`.
- **D6 — Fine-grained RBAC & multi-tenancy (ADR 0014).** `examlops.authz` — relationship model
  (`owner⊇editor⊇viewer`), default-deny, hierarchical project objects, `EXAMLOPS_MULTITENANCY`
  flag (off ⇒ single-tenant compat). `exa project grant|revoke|access`. Guide
  `docs/guides/rbac-multi-tenancy.md`.
- **D3 — ML supply-chain security (ADR 0013).** `examlops.supplychain` — model artifact signing
  (HMAC fallback / Sigstore seam), CycloneDX AI-BOM, enforce/warn verify-before-load gate.
  `exa models sign|verify|bom`. Guide `docs/guides/supply-chain-security.md`.
- **E2 — Optimized inference engines (ADR 0016).** `examlops.engines` — `InferenceEngine` protocol
  (vLLM default / SGLang, lazy GPU import) + dep-free `EchoEngine`; per-model `engine:` block +
  validation (CI guard); `exa models quantize` → new signed + BOM'd version (D3); speculative-decode
  telemetry to C1. `exa models engine list|validate`. Guide `docs/guides/llm-serving-engines.md`.
- **A2 — OpenLineage & provenance graph (ADR 0004).** `examlops.lineage` — fail-open `emit_lineage`
  (Marquez POST + `platform_db` dual-write), OpenLineage-schema events with `examlops.` facets.
  `exa models lineage --graph|--impact`. Guide `docs/guides/lineage.md`.
- **C2 — Continuous eval + LLM-as-judge (ADR 0007).** `examlops.evaluation` — deterministic
  evaluators + temperature-0 `LLMJudge` (records judge model+prompt), `Suite` runner, request-hash
  sampling, judge calibration; idempotent `eval_suite_results`. `exa eval run`. Guide
  `docs/guides/evaluation.md`.
- **C3 — Eval regression gate (ADR 0008).** `examlops.evaluation.gate` — `max_drop` regression +
  `min` floor, block/warn, higher/lower-is-better; `exa eval gate set|show|run`; `exa pipeline
  promote --force` override with block-gate refuse + D4 audit.
- **B2 — Model gateway & routing (ADR 0010).** `examlops.gateway` — OpenAI-compat `GatewayClient`
  with weighted routing + failover, typed errors, per-tenant/project virtual keys (allow-list +
  budget, hash-only storage, audited), per-call C1 span + FinOps cost, B3 cache hook, last-resort
  degrade. `exa gateway key issue|list|revoke` + `chat`. Guide `docs/guides/model-gateway.md`.
- **E1 — Kubernetes-native serving (ADR 0015).** `examlops.serving_backends` — `ServingBackend` seam
  (`RayServeCompose` default / `KServeK8s` via `EXAMLOPS_SERVING_BACKEND`), `registry_to_kserve`
  generates schema-valid InferenceService/LLMInferenceService from the model registry, canary
  rollout, D3 verify-before-load. `exa serve manifest|backend`. Guide `docs/guides/kubernetes-serving.md`.

## [0.31.0] — 2026-07-15

### Added

- **Next-Gen 40 · A1 — Data & dataset versioning (ADR 0003).** ExaMLOps now pins the *data* a run
  used, like it already pins code (git SHA) and models (MLflow versions). Every run resolves an
  immutable `DatasetRevision` before training and records it — enabling reproducibility, rollback, and
  EU AI Act Art. 10 data-governance evidence. Two strategies behind one value object: **lakeFS** commit
  ids when `EXAMLOPS_LAKEFS_ENDPOINT` is set, else a **deterministic content hash**
  (`sha256(sorted file digests ‖ schema)`), order-independent and stable across runs. Resolution is
  **fail-open** — a revision hiccup records `revision_id="unknown"` and warns rather than failing a
  run. New `dataset_revisions` `platform_db` table (idempotent on `(backend, dataset, revision_id)`);
  `exa data snapshot|list|diff|checkout`; `exa pipeline run --dataset-revision <rev>` pins a run
  (exit-non-zero if the revision was never recorded) and tags the MLflow run
  (`dataset_revision`/`dataset_backend`/`dataset_uri`). First feature of the 40-feature Next-Gen
  roadmap to move from design → implementation. Guide `docs/guides/data-versioning.md`; 28 new tests
  (spec GWT-1..5). Backward compatible — default runs simply record the resolved revision.
- **Self-driving MLOps autopilot (INC-9; ADR 0085).** Closes the loop to A3 maturity:
  detect drift → policy gate → retrain → MLflow-Staging metric gate → policy gate → promote.
  `exa autopilot run [--dry-run] [--model]` / `enable` / `disable` / `status`. Kill-switch is
  **disabled by default** (`EXAMLOPS_AUTOPILOT_ENABLED` env takes precedence over the `autopilot_config`
  table). Two policy gates — `autopilot_trigger` (before retrain) and `autopilot_promote` (before
  promotion); a `require_approval` decision emits a `human_approval_required` audit event instead of
  acting. New `autopilot_runs` + `autopilot_config` tables. 46 new tests (external calls injectable/
  monkeypatched, so no MLflow/Ray needed).
- **ExaMLOps Projects — Docker resource envelopes (ADR 0084).** RHOAI-inspired Projects ported to the
  Docker/Compose footprint: `exa project create/list/show/set-quota/assign-model/compose/archive/
  delete` with per-project CPU / memory / storage / GPU quotas, rendered into a Docker Compose fragment
  with per-service limits. New `projects` + `project_models` tables.
- **Programmable MLOps — drift & promotion providers (INC-5).** The `examlops.providers` substrate now
  covers two more domains: `drift` (`z-score` default) and `promotion` (`threshold` default), resolved
  via `resolve_drift_score_fn` / `resolve_promotion_eval_fn` and wired into `exa drift` / `exa pipeline
  promote`. Byte-identical defaults; swap the formula with zero core edits.
- **Programmable MLOps — LLMOps calculation providers (INC-5b; ADR 0083).** Four new provider domains —
  `llm_cost` (token-rate), `llm_cache` (hit-savings), `llm_routing` (least-cost), `rag_quality`
  (retrieval-lite) — bringing `exa providers list` to **9 domains** and pre-registering defaults so the
  F10-LLMOps backends compute through `get_provider` from their first commit.
- **Programmable MLOps — extension trust tiers (INC-6; ADR 0081).** Structural guards on the
  plugin/sandboxed-expression boundary: an AST-walk gate rejects unsafe constructs in sandboxed
  modules, a `SAFE_FUNCTIONS` allow-list is the review seam for the trusted tier. Guide
  `docs/guides/provider-security-trust-tiers.md`; 12 new security tests.
- **Programmable MLOps — least-privilege agent writes (INC-4; ADR 0082 layer 2).** Beyond the coarse
  `EXAMLOPS_MCP_ALLOW_WRITES` switch, every mutating MCP tool (`trigger_retrain`, `hpc_approve_cluster`)
  now passes the `agent_write` policy (ADR 0079) via `_agent_write_gate(action_kind, context)` before
  acting — so an operator can author "agents may retrain, nothing else" as a `policy.yaml` rule.
  `require_approval` is treated as *disallowed for an agent* (no human at the tool-call boundary); a
  `deny` is refused before any control-plane call. Policy unavailable never blocks (the write-gate has
  already applied). Backward compatible: no policy ⇒ allow.
- **Programmable MLOps — policy-as-code decision point (INC-3; ADR 0079).** Governance is now
  declarative: author rules in `~/.config/examlops/policy.yaml` (`action` + optional `when` condition
  + `effect` = `allow`/`deny`/`require_approval`) and every mutating op consults
  `examlops.policy.decide(action, context)` before acting. `exa retrain` is the first gate — a `deny`
  rule blocks it, a `require_approval` rule forces the confirmation. Conditions are evaluated with the
  **sandboxed** `simpleeval` tier (trust tier T2, ADR 0081 — no `eval`/`exec` of config; attribute/
  dunder access rejected). Every decision is written to `audit_events` (EU AI Act Art. 12 alignment),
  and audit failure never blocks the op. New `exa policy list` / `exa policy test <action> --set k=v`
  to inspect and dry-run rules. **Backward compatible:** no policy file (or no matching rule) ⇒
  `allow`, so existing Phase-29 confirms and the approval gate are unchanged; policies are additive
  constraints. Guide `docs/guides/programmable-mlops.md`.
- **Programmable MLOps — stable `examlops` Python SDK facade (INC-2; ADR 0078).** A small, typed,
  semver'd public surface (`examlops.status()`, `examlops.place()`, `examlops.list_providers()`,
  `examlops.resolve_provider()`, `examlops.__version__`, `examlops.api_version()`) that wraps existing
  internals so the CLI, MCP tools, and third-party code drive **one** code path. `exa status` and
  `exa hpc place` now render *through* the SDK (no duplicated logic). Returns typed objects
  (`PlatformStatus`/`ServiceHealth`/`PlacementResult`), not bare dicts; all heavy imports are lazy (no
  import cycle). Side benefit: `sdk.status()` degrades gracefully on any transport failure (socket
  reset/timeout), so `exa status` no longer crashes when the control plane is unreachable. Anything not
  exported is `_private` (SemVer + ≥1-minor deprecation window; Hyrum's-Law hygiene — small surface).
- **Programmable MLOps — pluggable fleet placement (INC-1 of the programmable-MLOps program; ADR 0077).**
  Placement scoring is now a **provider**, generalizing the finops provider substrate (ADR 0074) to its
  first non-finops domain — proving one uniform extension model. Change *how the fleet places jobs*
  (carbon-aware, cost-aware, fair-share) with **zero core edits**: a declarative formula in
  `~/.config/examlops/providers.yaml` (`placement:` block, sandboxed via `simpleeval`), a `pip`-installed
  plugin under the `exa.providers.placement` entry-point group, or the built-in **`least-loaded`** default
  (byte-identical to the legacy `headroom_score`). `choose_cluster` gains an optional injected scorer
  (`hpc_placement_providers.resolve_placement_score_fn`), wired into `exa hpc place --placement-provider`,
  `exa pipeline run --cluster auto`, and the `hpc_place` MCP tool. New `exa providers list [--domain]`
  surfaces every domain's providers (builtin/plugin/config + load status). A cluster's declared scalar
  `capabilities` (e.g. `carbon_intensity`) pass through to formulas. Graceful degradation: a broken
  provider falls back to `least-loaded`. Guide: `docs/guides/programmable-mlops.md`; design:
  `design/adr/0076`–`0082`, `design/vision/{ideas,library,futures,specs}/`; plan:
  `.claude/plans/programmable-mlops/`.

- **HPC fleet discovery — auto-detect the scheduler and enumerate resources (Phase 35a, read-only).**
  New `exa hpc` command group answers "which scheduler runs here, what nodes/GPUs does it have, and are
  they online" *without connecting a workload*: `exa hpc detect [host]` probes a candidate login node with
  side-effect-free commands and **suggests** a configuration (scheduler + transport + env), `exa hpc nodes`
  lists nodes with CPUs/memory/GPUs and a normalized state (idle/allocated/mixed/down/drain), and
  `exa hpc gpus` lists GPU devices (model/memory/utilization/online) — including the unmanaged `lxp-gpu01`
  via `nvidia-smi`. Backed by a **pluggable** probe registry (`platform/infra/slurm-adapter/discovery.py`:
  `FluxProbe`/`SlurmProbe`/`NvidiaSmiProbe` + `register_probe`) so new backends (PBS/LSF/k8s/cloud) drop in
  without touching the CLI. Flux uses `flux resource list` (state-grouped, hostlist-expanded); Slurm uses
  `sinfo`/GRES; a real scheduler reporting count-only GPUs is enriched with device detail from `nvidia-smi`.
  `--save --cluster <name>` persists the inventory to the new additive `hpc_nodes` table
  (`record_node_snapshot`/`get_node_snapshot`, latest-wins). Discovery only *proposes* — turning a cluster
  into one exaMLOps will schedule on is the sysadmin-approval step (Phase 35b). Design/plan in
  `.claude/plans/hpc-fleet-integration/`.
- **HPC cluster registry + sysadmin-approval-gated connect (Phase 35b).** A discovered cluster is now
  *registered* — never auto-connected. `exa hpc connect <host> --name <n>` probes the host and writes a
  cluster to the registry in state **PENDING**; `exa hpc clusters` lists all clusters and their state; and
  a sysadmin **approves** (`exa hpc approve <n>`) or **rejects** (`exa hpc reject <n> --reason ...`) before
  any job may be scheduled on it. Two sources of truth by design (decision): **`clusters.yaml`** holds the
  human-editable connection definition (scheduler/transport/host/ssh — keys referenced by path, never
  inlined; default `~/.config/examlops/clusters.yaml`, override `EXAMLOPS_HPC_REGISTRY`), and the additive
  **`hpc_clusters`** table holds governance state + who requested/approved + last capabilities. Re-probing
  an approved cluster never silently de-authorizes it. Every connect/approve/reject writes an
  `audit_events` row. `examlops.hpc_registry` resolves an ACTIVE cluster into the `EXAMLOPS_HPC_*` env the
  Phase 23 adapter already reads (`resolve_env`) and refuses non-ACTIVE clusters (`require_active`).
  **Dashboard (Facility console):** new `GET /api/v1/facility/fleet` (viewer) lists clusters + state, and
  admin-only `POST .../fleet/{name}/approve|reject` flip state + audit — surfaced as an in-console Fleet
  table with approve/reject actions for admins. New env: `EXAMLOPS_HPC_CLUSTER` (default target),
  `EXAMLOPS_HPC_REGISTRY` (registry path).
- **HPC placement, live queue & preflight (Phase 35c).** exaMLOps now *collaborates* with the schedulers
  to place training runs: `exa hpc place --gpus N` scores every ACTIVE cluster by matching headroom
  (filter to what *can* satisfy the ask → prefer most-idle) and explains the choice; `exa pipeline run
  --cluster <name>` pins a cluster (refuses non-ACTIVE — the approval gate holds on the scheduling path)
  and `--cluster auto --gpus N` lets placement choose, resolving the winner into the `EXAMLOPS_HPC_*` env
  the Phase 23 adapter reads. `exa hpc queue --cluster <n>` shows the live scheduler queue (`squeue` /
  `flux jobs`, normalized), `exa hpc jobs` lists tracked submissions from `hpc_jobs`, and `exa hpc
  preflight <cluster> --gpus N` runs fail-fast pre-submit checks (transport reachable → scheduler responds
  → requested resources exist), exiting 1 on any failure so it doubles as a CI gate. Placement lives in
  the pure, offline-tested `examlops.hpc_placement` (`choose_cluster`/`ResourceAsk`) with a single
  swappable scoring function; queue/preflight parsers extend the pluggable discovery probes.
- **HPC capacity/cost + agent (MCP) surface (Phase 35d).** `exa hpc capacity` joins each ACTIVE cluster's
  node inventory (`hpc_nodes`) with its consumed GPU-hours (`hpc_jobs`) to report total/idle GPUs,
  utilization %, GPU-hours used, and cost (at `GPU_COST_PER_HOUR`) — carbon stays in `exa finops carbon`
  (pure `examlops.hpc_capacity`). The fleet is now agent-callable via MCP: read-only tools `hpc_clusters`,
  `hpc_nodes`, `hpc_place`, `hpc_jobs` (always exposed) plus the mutating, audited `hpc_approve_cluster`
  (registered only under `EXAMLOPS_MCP_ALLOW_WRITES`) — so an agent can answer "which clusters are online,
  how many free GPUs, where should this run?" while approval stays human-gated. **Phase 35 (HPC Fleet)
  complete: discover → approve → place → account, across SLURM/Flux/unmanaged, pluggable for the future.**
  Guide: `docs/guides/hpc-fleet.md`; design/impl log `.claude/plans/hpc-fleet-integration/`.

- **Pluggable calculation providers — carbon & FinOps become swappable (ADR 0074).** A new general,
  reusable substrate `examlops.providers` (Strategy/Provider pattern + Python entry-point plugins +
  declarative YAML formulas) lets a user or sysadmin change *which formula and coefficients* the platform
  uses for carbon accounting — **without editing core code** — three ways: (1) override coefficients in
  `~/.config/examlops/finops.yaml`, (2) author an inline formula (`provider: expression`, evaluated safely
  with `simpleeval`), or (3) `pip install` a Python plugin under the `exa.providers.carbon` entry-point
  group. Ships three built-in carbon providers — `green-ai-default` (default; reproduces the original
  math **byte-for-byte**, zero behaviour change), `codecarbon-like` (GPU+CPU+RAM component energy), and
  `ccf-like` (Cloud Carbon Footprint shape). New CLI: `exa finops carbon providers` (list built-ins +
  plugins with methodology/uncertainty/status, `-o json`), and `--provider`/`--pue`/`--gpu-tdp` on
  `carbon estimate`/`record`. Resolution: `--provider` → `EXAMLOPS_CARBON_PROVIDER` → config → default;
  bad plugin/config degrades to the default, never crashes. Two documented trust tiers (Python plugin =
  trusted; YAML expression = sandboxed). Modelled on the Green Software Foundation Impact Framework.
  `simpleeval` added as the optional `[finops]` extra (lazy). New guide `docs/guides/finops-providers.md`;
  design in `.claude/plans/finops-plugins/`. The substrate is domain-agnostic — reused next for cost.
  Provenance: `carbon_records` gains an additive `provider` column (migration-guarded); `exa finops carbon
  record` persists which provider produced a figure, and the dashboard FinOps console reports the
  provider(s) used and surfaces the active provider's own methodology + uncertainty. Example plugin under
  `examples/exa-carbon-plugin/`; Makefile `finops-providers` / `finops-plugin-example` targets.
- **Pluggable cost rate cards — the substrate's second consumer (proves generality).** HPC cost
  (GPU/CPU-hours → USD) now runs through the same provider registry under a `cost` domain:
  `flat-rate` (default; reproduces `gpu_hours × rate (+ cpu_hours × rate)` byte-for-byte with the existing
  `GPU_COST_PER_HOUR`/`CPU_COST_PER_HOUR` env defaults) and a `tiered-example` volume-discount rate card.
  `exa models cost --record` computes cost via the active provider; select a rate card with `[finops.cost]`
  in `finops.yaml` / `EXAMLOPS_COST_PROVIDER` / an `exa.providers.cost` plugin / an inline formula. New
  `exa finops cost providers`. Same substrate, no substrate change — carbon and cost share it.

### Fixed

- **Platform-wide feature audit — correctness, safety and CI-gate fixes.** A systematic audit of the
  shipped features surfaced and fixed the following:
  - **Dashboard type-checking (CI gate):** `bff.aggregate()` now accepts `Mapping` (covariant) instead of
    `dict` so view-source registries type-check, and `selfobs.Metrics.reset()` resets fields explicitly
    instead of the unsound `self.__init__()` call. `make typecheck`/CI mypy is green again.
  - **Dashboard storage tests (CI gate):** `test_storage.py` (the MinIO/S3 gallery layer) failed under the
    current `moto`/`aiobotocore` versions (`'MockRawResponse' object has no attribute 'raw_headers'`). A
    scoped `moto`→`aiobotocore` compatibility shim in the dashboard test conftest adapts moto's sync stubber
    output to the async `AioAWSResponse` shape, restoring real coverage; the `--ignore=tests/test_storage.py`
    workaround was removed from `make preflight`.
  - **Dashboard SSE cross-tenant leak:** `/api/v1/stream` derived the caller's tenant with
    `getattr(claims_dict, "tenant")`, which on a dict always returns `None` and silently disabled the
    per-tenant event filter. Now reads `claims.get("tenant")`.
  - **`exa mcp` `list_models`:** picked the "latest" version by lexicographic string compare (reporting
    `"9"` as newer than `"10"`); now compares numerically. Also hardened against missing keys per the
    module's structured-envelope contract.
  - **`exa pipeline promote`:** crashed with a `ValueError` when a run logged a `NaN`/`Infinity` metric
    (MLflow serialises these as strings); now coerces to float and refuses to promote on a degenerate
    metric. Added a confirmation prompt before the live Production-alias write (auto-yes under
    `--yes`/`--json`/CI), matching the Phase-29 safety pattern its peers already follow.
  - **`exa mcp trigger_retrain`:** agent-initiated retrains now write a `retrain_triggered` audit event,
    closing the audit gap versus `exa retrain` and `hpc_approve_cluster`.
  - **`exa serve traffic`:** rejects negative weights (previously `--production 150 --canary -50` passed the
    sum-to-100 check and persisted a nonsensical rule).
  - **`exa config set` (fallback TOML writer):** escapes `"`/`\`/control chars so tokens or paths containing
    them round-trip through `tomllib` instead of writing an unparseable config (only the no-`tomli_w` path).
  - **`exa docs --out`:** creates missing parent directories and reports write errors cleanly instead of an
    unhandled `FileNotFoundError`.
  - **`exa approvals reject`:** sends `{"reason": ""}` instead of `{"reason": null}` when `--reason` is
    omitted, consistent with the audit record.
  - **Dashboard `/auth/login` brute-force gate:** the one credential-checking endpoint now enforces a
    per-client rate limit (10/min → 429 with `Retry-After`), reusing the existing F16 `RateLimiter`.
  - **Control-plane webhooks:** a non-string `ref` in a GitLab/GitHub push webhook body (e.g. `{"ref": null}`)
    crashed the handler with an uncaught 500 (`AttributeError` on `ref.endswith`); it now returns a clean skip.
  - **Ray Serve routing:** `POST /predict/JPCP` (canonical uppercase name) 404'd because the hot set is keyed
    on the lowercase MLflow name; lookups are now case-tolerant. Non-numeric features now return `422` (client
    error) instead of an opaque `500` that also polluted the error-rate metric.
  - **Ray Serve auto-reload:** a transient MLflow blip during a background poll no longer evicts a healthy
    model from rotation — the last-known-good entry is kept unless the alias is genuinely gone.
  - **HPC Flux adapter:** completed jobs whose eventlog serialises `"status": 0` (valid JSON, with a space)
    were misclassified `FAILED`; the eventlog is now parsed as JSON. Flux queue parsing uses a `|` delimiter
    so job names with spaces don't shift the state/node columns, and `list_nodes` synthesises *distinct* node
    names when Flux reports a count but no nodelist (identical names collided on the `hpc_nodes` key).
  - **HPC discovery parsing:** `expand_hostlist` now expands multi-dimensional hostlists (`rack[1-2]node[3-4]`)
    instead of leaving the second bracket literal; `_to_mb` rounds KB memory instead of truncating to 0/1 MB.
  - **Skipper `platform_ops`:** drift-baseline access is guarded (missing `std`/`mean` keys no longer raise
    `KeyError`), and the auto-retrain cooldown subtraction is timezone-robust (handles naive + aware
    timestamps) rather than latently crashing on a format mismatch.
  - **Skipper `memory_admin`:** `main()` now closes the SQLite connection it opens (was leaked every run).
  - **Ray Serve hot-set data race:** the background reload poller and request threads both mutate/iterate
    `_hot`/`_version_cache`; a `threading.RLock` now guards the multi-step sequences (snapshot-before-iterate
    in `/health`/`/models`/alias-scan, the LRU eviction loop, reload swaps) so a poll can no longer trigger
    `RuntimeError: dictionary changed size during iteration`. Slow MLflow loads stay outside the lock.
  - **Dashboard frontend — live container logs never authenticated:** `Services` read the JWT from a
    non-existent `localStorage` key (`auth_token`) so every log stream went out unauthenticated and silently
    401'd; it now uses `getToken()` (the `dashboard_auth` blob). The reconnect poll interval is also tracked in
    a ref and cleared on unmount (was leaking + could `reload()` from an unmounted component).
  - **Dashboard frontend — expired session never cleared on realtime pages:** a 401/403 on the SSE stream
    (`useRealtime`) looped "reconnecting" forever; it now clears auth and reloads like `apiFetch`. `AuthGate`
    also proactively logs out on token expiry via a timer instead of only reacting to the next API 401.
  - **Dashboard frontend — pipeline poll timeout could never fire:** the `Datasets` status-poll effect listed
    `pipelineStatus` in its deps and set it inside the interval, so every status transition reset the
    `count >= 60` 5-minute cap; it's now keyed on `pipelineId` only.
  - **Dashboard frontend — URL-sanitiser reliability + coverage:** `safeUrl` no longer shares a `g`-flagged
    regex with `sanitizeMarkdown` (a stateful `.test()` could skip a match); the Docs markdown link/image
    renderers now route `href`/`src` through `safeUrl`. Announcer timer is cleared on unmount.
  - **Blocking I/O off the event loop:** several `async` handlers ran blocking work directly on the loop
    thread, so one slow dependency could freeze the whole process. `list_containers` now offloads the blocking
    Docker SDK call to an executor; the control-plane GitLab/GitHub webhooks run `_record_push_event` (blocking
    SQLite + optional CI-trigger/auto-retrain HTTP) via `asyncio.to_thread`; and the dashboard BFF DB sources
    run their `sqlite3` queries via `asyncio.to_thread` so `aggregate()`'s per-source timeout is actually
    effective under a slow/locked `platform.db`.
  - **Promotion lifecycle consistency:** `promote_task` no longer archives the previous Production version
    when another live alias (Canary/Staging) still points at it — that produced a version carrying both
    `@Canary` and `@Archived`, which alias-based serving would still route as Canary.
  - **Control-plane `/status` hardening:** each concurrent service ping's `Future.result()` now has a hard
    deadline (a ping that hangs below the socket layer can no longer pin a worker thread past its budget).
  - **Dashboard approvals proxy:** the list endpoint no longer forwards the raw Control-Plane error body to
    the client (logged server-side instead), avoiding upstream-internals disclosure.

## [0.28.1] — 2026-07-10

### Fixed

- **v0.28.0 pipeline was red on the blocking GitLab `ruff` gate.** The new F11 (copilot),
  F22 (collaboration) and F24 (self-observability) dashboard backend modules and their tests shipped
  with unsorted import blocks (ruff `I001`) and unformatted code that `ruff check` / `ruff format --check`
  reject — leaving the release un-deployable to lxp. All import ordering and formatting auto-fixed; no
  behavior change.
- **`make preflight` was not portable across machines.** `ci-infra` assumed bare `ruff`/`pip` on `PATH`
  (Error 127 locally) — now prefers `.venv/bin/ruff` when present; the syntax gate excludes
  `node_modules`/`.venv` so a Docker-vitest `node_modules` artifact can't break it.
- **Dashboard NOC wall carbon tile.** Default a missing carbon `uncertainty` to `0` before labelling
  (null-safety).

## [0.28.0] — 2026-07-10

### Fixed

- **`exa docs` dropped every flag from the generated reference.** The command-tree walker filtered
  options with `isinstance(param, click.Option)`, but Typer's `TyperOption` subclasses
  `click.Parameter` (not `click.Option`) in current Typer/Click — so **all** flags were silently
  omitted from the whole reference. Now filtered on the version-stable `param_type_name == "option"`
  discriminator (framework-auto `--help`/completion meta-options excluded). The regenerated
  `docs/reference/cli-generated.md` now documents 143 commands and 136 flags; `make docs-cli`
  regenerates it. Regression test added.
- **Missing declared dependencies in `examlops` (the `exa` CLI).** `httpx` (imported at module load
  via `examlops.resilience`) is now a declared runtime dependency; `numpy`/`scipy` (lazily imported by
  `examlops.analysis.ab_stats` for `exa serve ab analyze`) moved to a new optional `analysis` extra,
  and the command now degrades with a clear install hint instead of a traceback when they are absent.

### Added

- **Dashboard collaboration & workflow (F22 / ADR 0073).** Collaboration primitives on platform
  entities — the final dashboard-nextgen feature (F1–F25 roadmap complete). Backend `collab.py` (pure
  `extract_mentions`/`sanitize_comment` — strips HTML + `javascript:`, R6): `add_comment` sanitizes,
  extracts @-mentions, persists tenant-scoped, and audits (R1/R5); `list_comments` is tenant-scoped
  (F15); `entity_activity` merges comments with the entity's audit events into one trail (R5);
  `create_snapshot`/`get_snapshot` mint and resolve a scoped, expiring, **read-only** shareable view
  token (R2/GWT-3). The dashboard owns two additive tables (`entity_comments`, `share_snapshots`) — it
  never alters the CLI's `platform_db` schema. Viewer-gated `routers/collab.py`: comments (GET/POST),
  `/activity`, and snapshot create/resolve; tenant + author from `principal_from_claims` (F15); @-mentions
  publish `event.mention` on the F8 bus (F12). Frontend `lib/collab.ts` (hooks + pure `extractMentions`/
  `snapshotShareUrl`), `components/CommentThread` (sanitized render via F16, mention preview) +
  `ShareSnapshotButton` (copies a frozen-view link), adopted on the model detail page. 11 backend + 6
  frontend tests; new guide `docs/guides/dashboard-collaboration.md`; documented in dashboard architecture
  + API ref. Approval discussion threads (R4), external share pages, and Notion/issue-tracker push behind
  a flag (R6) deferred.
- **Dashboard personalization, workspaces & onboarding (F21 / ADR 0072).** Per-user personalization,
  dependency-free and `localStorage`-backed. Pure `lib/prefs.ts`: `togglePinInList`/`pushRecentInList`
  (dedup + cap) plus `usePrefs` (default landing page + density, R3), `useWatchlist` (pin/unpin entities,
  R4), and `useOnboarding` (run-once tour state, R5). `lib/glossary.ts` (`GLOSSARY` + `searchGlossary`).
  `components/OnboardingTour` — a self-hosted first-run multi-step tour that completes once and is
  replayable; `components/HelpDrawer` — a `?`-triggered contextual drawer with a searchable glossary
  (R6), focus-trapped (F18); `components/PinButton` — star an entity onto the watchlist (R4). New
  `pages/Preferences` (`/preferences`) preference center: default landing, density, language (F19
  switcher), watchlist, and a replay-tour control; a **Preferences** nav entry. The app Home route now
  honours `defaultLanding` (R3) — fresh users keep Overview. 14 tests; new guide
  `docs/guides/dashboard-personalization.md`; documented in dashboard architecture. BFF UI-state
  (cross-device), the drag-drop widget grid + persona layouts (R1/R2), and F8/F12 watchlist
  notifications (R4) deferred.
- **Dashboard responsive, multi-device & NOC wall (F20 / ADR 0069).** Form-factor support, dependency-
  free. Pure `lib/responsive.ts`: `matchBreakpoint` (laptop/desktop/wide/ultrawide, R1), `isKioskMode`
  (`?kiosk=1`), `nextRotationIndex` + `useRotator` (wrap-around auto-cycle, R2), `popOut`/`popOutFeatures`
  (detach a live panel into its own window, R5), and jsdom/SSR-safe `useMediaQuery`/`useBreakpoint`. A
  **NOC/wall kiosk** at `/noc` — fixed full-screen, dark, big-font, auto-rotating curated slides (spend /
  alerts / carbon via the pure `buildNocSlides` in `lib/noc.ts`, degrading to "—" not an error page) with
  a live clock and no navigation chrome; it renders inside the authed app so an unattended wall never
  drops to a login (long-lived viewer token, R2) and always offers an Exit link. `components/PopOutButton`
  pops a Grafana/log panel into its own window (R5). A `@media print` stylesheet hides `.no-print` chrome
  (nav rail, copilot launcher, pop-out) for clean governance (F14) / finance (F13) reports (R6). 12 tests
  (6 pure/hook responsive + 3 NOC + 2 pop-out + print); new guide `docs/guides/dashboard-responsive-noc.md`;
  documented in dashboard architecture. Full PWA (offline shell, R4), touch/tablet density (R3), and
  richer pop-out wiring deferred.
- **Dashboard internationalization & localization (F19 / ADR 0071).** A dependency-free i18n layer:
  externalized namespaced catalogs (EN + Italian), Intl-based locale-aware formatting, and a **shared
  HPC-unit formatter** reused across F6/F13. (The spec named react-i18next; we ship the same capability
  without the dependency to keep the CI node-modules cache valid.) Pure `lib/i18n.ts`: `CATALOGS`,
  `translate` (dot-path lookup + `{{var}}` interpolation + EN fallback + missing-key warn, R1),
  `translatePlural` (`Intl.PluralRules` one/other), `formatNumber/Percent/Date/RelativeTime` (Intl, R3),
  `formatHpcUnit` (SI-scales FLOPS/B·s⁻¹/W/Wh/gCO₂e; GPU-hours unscaled — the single formatter for F6/F13,
  R3), `formatInTz` + `utcTooltip` (user-TZ display with a UTC reference, R4), `detectLocale`/`localeDir`.
  `hooks/i18nContext.ts` (`useI18n`/`useT`) + `hooks/I18nProvider.tsx` (locale state, `localStorage`
  persistence, `<html lang/dir>` sync for RTL-safety, R5). `components/LocaleSwitcher` in the shell;
  first adopted on FinOps (GPU-hours via `formatHpcUnit`, headings via `t()`). 13 pure + 3 provider
  tests; new guide `docs/guides/dashboard-i18n.md`; documented in dashboard architecture. Hardcoded-string
  CI guard (R1), pseudo-localization test (R5), and full catalog coverage deferred.
- **Dashboard embedded copilot (F11 / ADR 0065).** A context-aware, guardrailed copilot in the shell
  that answers grounded questions and **proposes** (never executes) `exa` actions. It is a thin BFF proxy
  to the **existing** Skipper agent bridge — the same OpenAI-compatible `POST /v1/chat/completions` that
  `exa ask` uses — so no new model, no paid API. Backend `copilot.py` (all pure except the network call):
  `build_system_context` injects page/entity/filters and **frames page content as untrusted data, never
  instructions** (prompt-injection mitigation, R6); `extract_proposals` pulls `exa …` commands from the
  answer and flags `requiresApproval` for mutating subcommands (retrain/promote/approve/traffic/…, R5);
  `ask_copilot` calls the bridge with an injectable transport and degrades to a well-formed
  `_partial:["agent"]` envelope instead of a 500. Viewer-gated `POST /api/v1/copilot/ask` audits every
  query (`source=dashboard-copilot`, D4); there is deliberately **no execution endpoint**. Frontend
  `components/CopilotPanel.tsx` — a right-drawer launcher on every page (focus-trapped via F18), answers
  rendered through `sanitizeMarkdown` (F16), proposals shown as **copy-only** cards with an approval-gate
  badge (no run button), collapsible agent trace (R6); `lib/copilot.ts` derives grounding context from
  the route. 16 backend + 11 frontend tests; new guide `docs/guides/dashboard-copilot.md`; documented in
  dashboard architecture + API ref. Streaming over F8 WS, NL→in-app-view, and one-click approval-gate
  confirm deferred.
- **Dashboard accessibility — WCAG 2.2 AA substrate (F18 / ADR 0068).** Keyboard/screen-reader/contrast
  primitives so new surfaces inherit accessibility instead of retrofitting. Pure `lib/a11y.ts`:
  `oklchLuminance` (OKLCH→OKLab→linear-sRGB→WCAG relative luminance) + `oklchContrast`/`meetsAA`/
  `meetsAAA`, `prefersReducedMotion`, and `tabbableWithin`. `hooks/useFocusTrap(ref, active)` traps
  Tab/Shift-Tab in a dialog and **restores focus to the trigger** on close (R2) — adopted by the ⌘K
  CommandPalette. `hooks/announcer.ts` + `hooks/useAnnouncer.tsx`: a polite/assertive `aria-live` region
  mounted at the app root with `announce()` (clears-then-sets so repeats re-announce without focus theft,
  R3); a `RouteAnnouncer` announces page changes. `components/SkipLink` + `<main id="main" tabIndex=-1>`
  in Layout give a skip-to-content link (R2). A global `@media (prefers-reduced-motion: reduce)` rule
  neutralizes animation/transition (R5). CI guard: `lib/a11y.test.ts` audits the **real F3 OKLCH tokens**
  (day/night/high-contrast) meet AA — a dependency-free stand-in for axe-core (R4). 17 tests (11 pure +
  2 focus-trap + 3 announcer + 2 skip-link + doc-token audit); new guide
  `docs/guides/dashboard-accessibility.md`; documented in dashboard architecture. axe-core-in-CI (R7),
  full manual audit (R1), and trap adoption across all dialogs deferred.
- **Dashboard data grid & bulk operations (F17 / ADR 0061).** A reusable, dependency-free table
  primitive every list surface composes instead of hand-rolling `<table>`. Pure `lib/datagrid.ts`:
  `applySort` (stable multi-column), `applyFilter`, `computeFacets` (counts over filtered rows, R3),
  `queryRows` (filter → facets → sort → paginate into a `ListQuery`→`ListResult` envelope matching what
  a BFF list endpoint should serve, R2), `toggleSelection`, `toCsv` (RFC-4180 escaping, R6), and
  `encodeQuery`/`decodeQuery` (URL round-trip for shareable/deep-linkable views, R3). Component
  `components/DataGrid.tsx`: sortable headers (asc→desc→clear), facet filter chips with live counts,
  a `localStorage`-persisted density toggle (R1), row-selection checkboxes, and a bulk-action bar that
  requires an **inline confirm** before firing against the selected rows — never a silent mass mutation
  (R4). `Column.render` gives display-only formatting while `accessor` stays the sort/filter/export
  scalar (numeric columns sort numerically even when shown formatted). First adopted on the FinOps
  cost-by-model table (now sortable + CSV-exportable). 11 pure + 5 component tests; new guide
  `docs/guides/dashboard-data-grid.md`; documented in dashboard architecture. Virtualization,
  server-side query execution, and saved views (R5) deferred.
- **Dashboard feature flags & staged rollout (F25 / ADR 0070).** A self-hosted feature-flag layer
  completing the F23 client seam — flags evaluated **server-side** with context (tenant + role from
  F15 + deterministic percentage bucket); the client receives decisions, not rules. Backend
  `feature_flags.py`: the `FLAG_DEFS` registry (default + optional tenant/role/percentage targeting),
  `subject_bucket()` (stable SHA-256 0–99 bucket so a subject stays on one side of a rollout, R5),
  `evaluate()`/`evaluate_all()` (contextual decisions, R1/R3; admins bypass percentage gating), and
  `set_override()` (persists to `feature_flag_overrides`, audits to `audit_events`, R4). New endpoints:
  viewer `GET /api/v1/flags` (decisions), admin `GET /api/v1/flags/admin` (defs + state) and
  `POST /api/v1/flags/{name}` (override → audit + `event.flag_changed` on the F8 bus, R2). Frontend
  `lib/serverflags.ts` (`useFlagDecisions`, a `useFlag(name)` hook that prefers the server decision and
  falls back to the client default via pure `flagFallback`, `useSetFlag`) + admin `pages/Flags.tsx`.
  New admin **Flags** nav + lazy `/flags` route. 11 backend tests + 2 frontend tests; new guide
  `docs/guides/dashboard-feature-flags.md`; documented in dashboard architecture + API ref. Live-push
  into `useFlag`, CRUD targeting editor, and adoption telemetry deferred.
- **Dashboard alerting & incident surface (F12 / ADR 0062).** A unified alert inbox derived from the
  platform's own signals. Backend `alerts.py`: `active_alerts()` merges **drift** (z-score of latest
  prediction mean vs the drift baseline — ≥3σ critical, ≥2σ warn), **budget** (consumed cost >
  `cost_budget` → error), and **eval** (a failed metric in the latest eval run → warn) into
  severity-sorted, typed alert rows with per-severity counts (R1); `acknowledge()` audits an ack to
  `audit_events` (R3/D4). New viewer-gated `GET /api/v1/alerts` (BFF-composed) and
  `POST /api/v1/alerts/{id}/ack` (audits + publishes `alert.acked` on the F8 realtime channel).
  Frontend `lib/alerts.ts` (types + hooks + pure `severityToken`/`severityRank`/`inboxHeadline`) +
  `pages/Alerts.tsx` (severity-pill inbox + **Ack** button via a mutation). New **Alerts** nav + lazy
  `/alerts` route. 7 backend tests + 5 frontend tests; new guide `docs/guides/dashboard-alerts.md`;
  documented in dashboard architecture + API ref. Alertmanager merge + incident correlation (R2),
  silence/snooze (R3), on-call/escalation (R4), runbooks (R5), SLO board (R6) deferred.
- **Dashboard LLMOps console (F10 / ADR 0064).** An LLMOps surface over the LLM-serving substrate.
  Backend `llmops.py`: `endpoints()` lists the `llm_endpoints` registry (engine / HF model /
  tensor-parallel / dtype / enabled, R2); `eval_summary()` returns the **latest** eval run per model
  from `eval_runs`/`eval_results` with its metric results (value / baseline / passed) and a computed
  `passRate` (R1/C2). Missing tables degrade to empty payloads (R6). New viewer-gated endpoint
  `GET /api/v1/llmops/overview` (BFF-composed). Frontend `lib/llmops.ts` (types + hooks + pure
  `passRateLabel`/`evalTone`) + `pages/Llmops.tsx` (endpoint registry table, per-model eval cards with
  metric pass/fail badges). New **LLMOps** nav + lazy `/llmops` route. 6 backend tests + 2 frontend
  tests; new guide `docs/guides/dashboard-llmops.md`; documented in dashboard architecture + API ref.
  Prompt studio (R1), gateway routing (R2), semantic cache (R3), RAG-ops (R4), vector-DB views (R5)
  deferred.
- **Dashboard governance & compliance surface (F14 / ADR 0063).** An honest, evidence-based governance
  view over the shipped compliance backend. Backend `governance.py`: `nist_posture()` grades NIST AI
  RMF controls `satisfied`/`partial`/`gap` from real evidence — change-approval satisfied only if
  approval audit events exist, model documentation graded by card coverage — never false green (R1);
  `compliance_status()` reports per-model EU-AI-Act risk class + Annex-IV technical-file + provenance
  presence (R2); `audit_integrity()` computes a deterministic SHA-256 **hash-chain** over ordered
  `audit_events` and returns the tamper-evidence `headDigest` (R3); `model_card_coverage()` lists
  carded vs uncarded models with honest gaps (R5). New viewer-gated endpoint
  `GET /api/v1/governance/overview` (BFF-composed). Frontend `lib/governance.ts` (types + hooks + pure
  `postureToken`/`coverageLabel`/`digestShort`) + `pages/Governance.tsx` (posture list, EU-AI-Act
  table, card coverage, audit-integrity badge). Admin-only **Governance** nav + `/governance` route.
  8 backend tests + 3 frontend tests; new guide `docs/guides/dashboard-governance.md`; documented in
  dashboard architecture + API ref. Policy-as-code (R4), supply-chain/fairness (R5), approvals-2.0 +
  compliance-export PDF (R6) deferred.
- **Dashboard self-observability (F24 / ADR 0067).** The dashboard observing itself, with no
  third-party egress. Backend `selfobs.py`: a `Metrics` collector fed by `MetricsMiddleware` (request
  count, 4xx/5xx, 429 rate-limit hits, p50/p95 latency window), `dependency_health()` probing
  `platform.db` for the status page (R5), and `record_ui_action()` auditing UI actions to
  `audit_events` (R4/D4, degrades quietly). New viewer-gated endpoints `GET /api/v1/selfobs/status`
  (health + metrics) and `POST /api/v1/selfobs/action` (UI audit). Frontend `lib/telemetry.ts`:
  `scrubPii()` (pure, unit-tested — strips email/JWT/bearer/long-hex before any report leaves the
  browser, R1), `reportAction()` (audits via the backend, no third-party endpoint, R3), and
  `useSelfObsStatus()`; `pages/SelfObs.tsx` is the in-app **Status** page (KPI tiles reusing the F4
  `<KpiTile>`, dependency list) at `/status`. 8 backend tests + 5 frontend tests; new guide
  `docs/guides/dashboard-self-observability.md`; documented in dashboard architecture + API ref.
  GlitchTip (R1), OTel browser tracing (R2), self-hosted analytics (R3), web-vitals RUM (R4),
  Playwright synthetics (R6) deferred.
- **Dashboard FinOps & Green-AI surface (F13 / ADR 0066).** A cost + carbon page rendering the shipped
  phase 23/24 cost/carbon backend. Backend `finops.py`: view-shaped helpers over
  `model_costs`/`project_budgets`/`carbon_records` — `cost_rollup` (per-model GPU-hours + USD + totals,
  R1), `budget_status` (budget-vs-actual + `overBudget` flag, R2), `carbon_summary` (kWh + gCO₂e totals
  with a `methodology` string + ±30% `uncertainty` so there's no false precision, R3), `unit_economics`
  (cost-per-training-run, R4). New viewer-gated, partial-failure-safe endpoint
  `GET /api/v1/finops/overview` (BFF-composed). Frontend `lib/finops.ts` (types + hooks + pure
  `usd`/`budgetPct`/`carbonLabel`) + `pages/Finops.tsx` (KPI tiles reusing the F4 `<KpiTile>`, cost
  table, budget list with an over-budget pill, carbon methodology footnote). New **FinOps** sidebar
  entry + lazy `/finops` route (F23). 7 backend tests + 4 frontend tests; new guide
  `docs/guides/dashboard-finops.md`; documented in dashboard architecture + API ref. Sankey/drill (R1),
  burn forecast + overspend alert (R2), SCI/energy-mix trend (R3), waste reclaim (R5), chargeback
  export (R6) deferred.
- **Dashboard authz & multi-tenancy (F15 / ADR 0057) — completes Wave-0 P0.** A capability-based
  authorization affordance layer + tenant scoping over the existing 2-role JWT; the BFF stays the sole
  enforcement point. Backend `capabilities.py`: a `role → capability set` catalogue (viewer =
  `view`+`search`; admin adds `model.promote`/`approval.decide`/`retrain.trigger`/`config.write`/
  `secret.reveal`/`service.control`/…), `capabilities_for`/`can`/`deny_reason` (explains denials, R3),
  the `require_capability(cap)` BFF enforcement dependency (R2), `STEP_UP_CAPABILITIES` flagging
  step-up actions (R6), and tenant scoping (default-deny) via `tenant_visible`/`assert_tenant_access`
  (403 cross-tenant)/`scope_to_tenant` (R4). `GET /api/auth/me` now returns `tenant` + `capabilities[]`.
  Frontend `lib/capabilities.ts` (`useCapabilities()` from `/me` + pure `can`/`reason`/`requiresStepUp`),
  `<CapabilityGate capability>` (renders a control only when held, else a dimmed explained affordance —
  no silent dead control, R3), and a tenant badge in the shell (R4). 13 backend tests + 5 frontend
  tests; new guide `docs/guides/dashboard-auth-tenancy.md`; documented in dashboard architecture.
  OIDC/Keycloak (R1), OpenFGA (R2), tenant switcher (R4), token/session management (R5), step-up/MFA
  enforcement (R6) deferred.
- **Dashboard frontend engineering baseline (F23 / ADR 0052).** Typed errors, resilience, and
  code-splitting hardening the existing client. `lib/errors.ts`: `ApiError` (typed `status` + parsed
  RFC 7807 `Problem`) + `parseProblem()` (accepts problem+json, FastAPI `{detail}` string/validation
  array, or falls back) + `shouldRetry()` (never retry a 4xx) — `apiFetch` now throws `ApiError` and
  the `QueryClient` uses `shouldRetry` (R2). `components/ErrorBoundary.tsx`: catches render errors and
  shows a designed `EmptyState` fallback with a retry button instead of a white screen. `App.tsx`:
  heavy routes are `lazy()`-imported behind a `<Suspense>` skeleton (R6), the outlet is wrapped in the
  error boundary, and new surfaces (`/mlops`, `/facility`) are gated behind feature flags. `lib/flags.ts`:
  typed flag registry + `resolveFlag()` (localStorage → env → default) as the F25 read seam (R7). 16
  frontend tests (errors/flags/ErrorBoundary); new guide `docs/guides/dashboard-frontend-engineering.md`;
  documented in dashboard architecture. OpenAPI codegen + contract gate (R1/R3), Playwright E2E,
  Storybook visual + axe a11y (R4/R5), bundle budget (R6), preview deploys (R7) deferred.
- **Dashboard security hardening baseline (F16 / ADR 0053).** Backend `security.py`:
  `SecurityHeadersMiddleware` attaches a strict header set to every response — CSP with
  `frame-src`/`frame-ancestors` scoped to the Grafana embed origin (F5), HSTS,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy` (R1); and a
  `RateLimiter` + `rate_limit()` dependency (in-process fixed-window, per-client) returning **429** on
  flood, applied to the fan-out `GET /api/v1/search` endpoint (R7). Frontend `lib/sanitize.ts`:
  `sanitizeMarkdown()` strips `<script>`/`<style>`/framing tags, inline `on*=` handlers, and
  `javascript:`/`data:text/html` URIs; `safeUrl()` guards hrefs (R2) — wired into the Docs and Model
  README renderers as defense-in-depth (react-markdown already renders raw HTML inert; no unsanitized
  `dangerouslySetInnerHTML` in the tree). 7 backend tests + 9 frontend tests; new guide
  `docs/guides/dashboard-security.md`; documented in dashboard architecture. CSP nonce rollout, CSRF
  (R3), audited secret reveal (R4), PII redaction (R5), and CI SCA (R6) deferred.
- **Dashboard visualization system (F4 / ADR 0055).** A themed, dependency-free in-product chart
  toolkit — reusable colour-blind-safe primitives the domain consoles compose (deliberately not
  duplicating F5/Grafana's heavy time-series). `lib/viz.ts` holds the pure, unit-tested chart math
  (`thresholdTone` ok/warn/crit with higher-/lower-worse direction, `histogram`, `sparklinePoints`,
  `ciLabel`, `formatDelta`, `toneStatus`). Components (`components/viz/`): `<ChartFrame>` — the
  required a11y wrapper (aria-label + a keyboard-reachable `<details>` **data-table fallback**, F4
  R7/F18); `<KpiTile>` — threshold-coloured KPI + inline sparkline (F4 R2); `<Distribution>` —
  histogram (F4 R5); `<Uncertainty>` — dot-and-whisker CI/error-bar chart for A/B & eval (F4 R5,
  uncertainty shown, not point estimates alone). Colour is resolved through the F3 status tokens and
  always paired with a value/label. Wired into the Facility console (queue-depth threshold) as the
  first consumer. 11 pure-helper tests + 4 component tests; new guide
  `docs/guides/dashboard-visualization.md` (incl. when-to-use-F4-vs-F5); documented in dashboard
  architecture. `@xyflow` lineage/topology, brush-zoom, export deferred.
- **Dashboard command palette & global search (F2 / ADR 0056).** A ⌘K / Ctrl-K command palette for
  fuzzy navigation, role-scoped actions, and federated search. Backend `search.py` provides one
  `search()` that fans out across pages / models (reusing `mlops.registry_rows`) / HPC jobs
  (`hpc_jobs`) / audit events, returning typed, grouped, **ranked** (`score()`: exact > prefix >
  word-boundary > substring > fuzzy subsequence), entity-linked results; missing tables degrade to no
  results, never an error. New viewer-gated, partial-failure-safe endpoint `GET /api/v1/search?q=`
  (BFF-composed). Frontend `lib/commands.ts` (role-scoped registry with `exa`-CLI equivalents for
  GUI↔CLI parity), `lib/search.ts` (client-mirrored `fuzzyScore` + debounced/cancelable `useSearch`),
  and `components/CommandPalette.tsx` (⌘K toggle, ↑/↓/Enter/Esc, commands rank first + federated
  results below, mounted once in `Layout`). Admin-only commands hidden from viewers (F15). 8 backend
  tests + 5 frontend tests; new guide `docs/guides/dashboard-command-palette.md`; documented in
  dashboard architecture + API ref.
- **Dashboard facility console (F6 / ADR 0059).** A scheduler-neutral HPC operations view rendering
  the phase-23 scheduler abstraction (mock / Slurm / Flux). Backend `facility.py` provides pure,
  view-shaped helpers over the `hpc_jobs` table: `facility_overview()` (node/GPU allocation, queue
  depth, per-partition running/queued/GPU counts), `job_queue()` (waiting jobs longest-wait-first),
  and `job_detail()` (resources/timing + the `mlflow_run_id` cost link). Multi-cluster (F6 R6): every
  helper takes an optional cluster filter so the UI switcher rescopes all lists through one path;
  missing `hpc_jobs`/empty DB degrades to zeros, never a 500 (F6 R7). New viewer-gated,
  partial-failure-safe endpoints `GET /api/v1/facility/overview|queue|job/{id}` (BFF-composed).
  Frontend `lib/facility.ts` (types + hooks + pure `waitLabel`/`partitionTone`) +
  `pages/FacilityConsole.tsx` (KPI cards, partition list with colour-blind-safe "Backlog" pill,
  cluster switcher, queue table). New **Facility** sidebar entry + `/facility` route. 10 backend
  tests + 4 frontend tests; new guide `docs/guides/dashboard-facility-console.md`; documented in
  dashboard architecture + API ref.
- **Dashboard MLOps console (F9 / ADR 0060).** A read-only single pane for model registry, lifecycle
  health, and **guided promotion**, surfacing the shipped MLOps backend. Backend `mlops.py` provides
  pure, view-shaped helpers over `platform.db`: `registry_rows()` composes the union of models seen
  across `drift_snapshots`/`model_costs`/`traffic_rules`/`promotion_rules` into registry rows with a
  colour-blind-safe health token and `governed` flag; `promotion_check()` mirrors `exa pipeline
  promote` — allowed only with an **enabled** policy, else denied with explicit reasons (F9 R4), with
  the phase-11 approval step always surfaced; `model_detail()` composes cost/drift/traffic/promotion
  tabs. The MLflow uppercase↔lowercase name mapping lives in exactly one place (`display_name()` /
  `mlflow_name()`, F9 R2). New viewer-gated, partial-failure-safe endpoints `GET
  /api/v1/mlops/registry|model/{name}|promotion/{name}` (composed through the F8 BFF substrate).
  Frontend `lib/mlops.ts` + `pages/MlopsConsole.tsx`: a selectable registry grid beside a guided
  promotion panel; the pure `promotionVerdict()` helper keeps the UI verdict in lockstep with the
  backend. New **MLOps** sidebar entry + `/mlops` route. 10 backend tests + 5 frontend tests; new
  user guide `docs/guides/dashboard-mlops-console.md`; documented in dashboard architecture + API ref.
- **Dashboard Grafana embed layer (F5 / ADR 0054).** A themed, config-driven `<GrafanaPanel>` that
  embeds the provisioned Grafana dashboards inside the React pages. `lib/grafana.ts` holds a typed
  `GRAFANA_PANELS` registry (UIDs/panel-ids never hardcoded) and the pure `buildPanelUrl()` (builds a
  `d-solo` URL with kiosk/theme/time-range/template-vars; returns `null` when unconfigured). The
  component lazy-loads the iframe, shows a `<Skeleton>` while loading, and an accessible `<EmptyState>`
  fallback when Grafana isn't configured — a missing dashboard never leaves a broken frame. 8 tests;
  documented in the dashboard architecture. Wired end-to-end on the Overview page (a "Live Metrics"
  panel, theme-synced, base URL sourced from the health probe's Grafana service URL).
- **F1/F3 shared-primitive adoption across dashboard pages (complete).** All pages with async
  loading/empty states — Approvals, Drift, Pipelines, Audit, Services, Datasets, Overview, Models,
  ModelDetail, SeanerBus, and Docs — now use the designed `<Skeleton>` loading convention and
  `<EmptyState>` (accessible, CTA-ready) in place of ad-hoc `Loading…` text and bespoke
  empty/`animate-pulse` blocks — consistent, colourblind-safe, WCAG-2.2-AA states (ADR 0050/0051).
  (Jupyter and Config have no such states.)
- **Dashboard Backend-for-Frontend (BFF) layer — first slices (F8 / ADR 0058).** New
  `backend/bff.py` `aggregate()` substrate fans out to view sources concurrently with a per-source
  timeout and returns partial payloads (`_partial` lists failed/slow sources) instead of failing the
  page (F8 R2/R5). First view endpoint `GET /api/v1/overview` (viewer-gated) composes meta + traffic
  + drift + audit from `platform.db`.
  - **Realtime SSE gateway (R3–R7):** `backend/realtime.py` `EventBus` fans typed events
    (`job.*`/`drift.*`/`alert.*`/`deploy.*`/`approval.*`/`event.*`) to subscribers, tenant-filtered
    (no cross-tenant leakage) with bounded per-subscription queues and drop-oldest backpressure.
    `GET /api/v1/stream` is the viewer-gated multiplexed SSE endpoint (keep-alives + disconnect-aware).
  - **First publisher wired:** the approvals router emits `approval.approved`/`approval.rejected`
    onto the bus on a successful approve/reject, so open dashboards update live without polling.
  - **Client substrate (R5):** `frontend/src/lib/realtime.ts` — `parseSSEChunk()` (SSE frame parser,
    keep-alive-aware, remainder-carrying), `readEventStream()` (fetch-streaming reader, reassembles
    frames across chunks), `nextConnectionState()` (`live → reconnecting → polling`); the `useRealtime`
    hook opens `/api/v1/stream` with auth, forwards events, reports connection state, and reconnects;
    `<ConnectionBadge>` renders that state via `StatusPill` (icon + label, not colour-only — F18).
  - 16 backend tests + 12 frontend tests; documented in the dashboard architecture + `docs/reference/api.md`.
- **`docs/reference/cli-generated.md` + `make docs-cli`** — an always-current full command reference
  auto-generated from the live Typer/Click tree, so the reference can't drift from the code.
- **Global `--quiet`/`-q` and `--verbose`/`-v` + documented exit-code contract (exa CLI next-gen, N4).**
  `--quiet` suppresses non-essential chatter (hints/info/detail); `--verbose` enables extra `detail()`
  diagnostics. The stable exit-code contract (`0` success, `1` runtime error, `2` usage error) is now
  documented in `docs/reference/cli.md`.
- **Safety pattern rolled into mutating commands (exa CLI next-gen, N5).** `exa drift reset`
  (deletes snapshots) now previews with `--dry-run`, requires confirmation (default *no*; auto-yes under
  `--yes`/`--json`/CI), and writes a `drift_reset` audit event. `exa drift baseline` gains `--dry-run`
  and confirms before overwriting an existing baseline. `exa serve traffic` gains `--dry-run` (it
  already confirmed + audited). `exa approvals approve` (fires training) now confirms first and
  supports `--dry-run`; `exa approvals reject` gains `--dry-run`. `exa drift input baseline`
  (`--dry-run` + confirm-before-overwrite) and `exa drift input reset` (`--dry-run` + confirm +
  `input_reset` audit event) round it out. Same `dry-run → confirm → audit` shape as `exa retrain`.
- **Live `--watch` on more read-only views + drift Trend sparkline (exa CLI next-gen, N3).**
  `exa drift status` and `exa serve traffic-list` now accept `--watch`/`-w` + `--interval N` for a
  screen-clearing live view (Ctrl-C to exit), matching `exa status --watch`. `exa drift status` gains
  a **Trend** column — a unicode sparkline of each model's recent predictions — and `--json` output now
  includes the raw `recent` series backing it.
- **Dashboard next-gen initiative — design complete + implementation started.** A 24-feature program
  to make the operator dashboard exascale-ready. Design phase distilled 200 aspects into a roadmap,
  target-state architecture, and 24 accepted ADRs (`design/adr/0050`–`0073`) + specs
  (`design/vision/specs/F1`–`F25`); see `.claude/plans/dashboard-nextgen/`.
  - **Design-system substrate (F3 / ADR 0051):** `lib/status.ts` — colourblind-safe status/severity
    semantics (`HealthStatus`/`Severity` → `{label, icon, theme-aware colorVar}`, `statusMeta()`,
    `normalizeHealth()`); `ui/status-pill.tsx` `<StatusPill>` pairs colour with an icon **and** a text
    label (never colour-only, WCAG 2.2 AA / F18); `ui/skeleton.tsx` `<Skeleton>` loading convention.
  - **First-class states (F1 / ADR 0050):** `ui/empty-state.tsx` `<EmptyState>` (accessible region +
    CTA); `NotFound.tsx` and `Forbidden.tsx` designed 404 / access-denied surfaces — `Forbidden` always
    explains *why* access is denied (F15 R3), never a silent dead end.
  - Additive frontend-only; 17 new dashboard tests (green). Adoption across the 13 existing pages is
    the next step.
- **`exa` CLI next-gen overhaul — agent-native surface (Phases N1–N7 + N1.1/N4.1).** Turns the
  operator CLI into a futuristic, agent-native, extensible platform surface. See ADR 0045 and
  `.claude/plans/exa-cli-nextgen/`.
  - **MCP + Agent-to-Agent (N1/N1.1):** new `examlops.mcp` package exposes the platform to LLM
    agents and MCP clients, reusing the CLI's own `_client`/`platform_db` code paths (single source
    of truth). `exa mcp serve` (FastMCP, stdio+http), `exa mcp tools`, `exa mcp resources`,
    `exa mcp prompts`, `exa mcp agent-card` (A2A Agent Card). 9 tools, 4 resources (incl. templated
    `examlops://model/{name}`), 3 expert prompts (`diagnose_drift`/`promote_safely`/`platform_triage`).
    Write-safety via `EXAMLOPS_MCP_ALLOW_WRITES` / `--allow-writes`. `fastmcp` optional extra
    (`examlops[mcp]`, lazily imported — core CLI unaffected).
  - **Conversational & discoverability (N2):** `exa ask "<q>"` routes plain English to the Skipper
    agent (`AGENT_URL`, `--session`, `--json`, HITL hint); `exa explain [cmd …]` gives plain-language
    help + examples from the live command tree; fuzzy "did you mean" on unknown commands.
  - **Visual/live (N3):** `exa status --watch`/`-w --interval` live auto-refresh; reusable
    `sparkline`/`bar` primitives (GPU-hours trend under `exa models cost`).
  - **Output ergonomics (N4/N4.1):** global `--output`/`-o table|json|yaml|csv` (superset of `--json`);
    `exa docs` generates the full command reference from the live tree (`--out`, `--json`).
  - **Safety & governance (N5):** `exa retrain` gains `--dry-run`, a confirmation prompt (auto-yes
    under `--yes`/`--json`/CI), and a `retrain_triggered` audit event.
  - **Multi-environment (N6):** named config contexts (`[contexts.<name>]` + `active_context`);
    `exa config set --context`, `exa config use`, `exa config contexts`, `exa env` (effective config +
    per-key provenance, secrets redacted); global `--context`/`-c`.
  - **Extensibility (N7):** third-party `exa` subcommands via the `examlops.cli_plugins` entry-point
    group; `exa plugins` lists load status (error-resilient discovery).
  - New deps: `tomli-w>=1.0` (config), `fastmcp>=3.4` (optional `[mcp]` extra). New env vars:
    `EXAMLOPS_MCP_ALLOW_WRITES`, `AGENT_URL`, `EXAMLOPS_CONTEXT`, new `agent_url` config field.
    ~73 new tests; fixed 3 latent bugs (status `.get()`-on-`str`, retrain `StorageBackend` enum
    serialization, TOML nested-table round-tripping).
- **Skipper long-term memory (Phase 25, SM1–SM3).** The Skipper agent gains cross-session
  memory on top of its per-thread checkpointer, built LangGraph-native and fully self-hosted.
  See ADR 0033 (architecture) + ADR 0034 (governance) and `design/architecture-skipper-memory.md`.
  - **SM1 substrate:** official sync `SqliteStore` (`langgraph-checkpoint-sqlite>=3.1`, `sqlite-vec`)
    in its own `skipper_memory.db`; **local embeddings only** (Ollama `nomic-embed-text` default,
    `sentence-transformers` offline fallback); a context-trimming `pre_model_hook` (default-off).
    All additive — the agent degrades to short-term memory if the store/embeddings are unavailable.
  - **SM2 memory types:** procedural / episodic / preference / KB memories (Pydantic schemas +
    namespaces), stored as **summaries + foreign-key pointers into `platform_db`, never row copies**;
    three store-backed tools (`recall_memory`, `remember_preference`, `record_procedure`) wired into
    the agent only when a store is present; intent-gated retrieval guidance in the system prompt.
  - **SM3 governance:** every memory mutation is audited to `platform_db.audit_events`; procedure
    writes are confirmation-gated (HITL); `python -m skipper.memory_admin` enumerates/exports/erases
    memory (cascade + audited) for GDPR; a red-team invariant proves no poisoned memory can bypass the
    confirmation gate on dangerous tools.
  - New env vars: `AGENT_MEMORY_ENABLED/_DB`, `AGENT_EMBED_BACKEND/_MODEL/_DIMS`,
    `AGENT_SUMMARIZE_ENABLED`, `AGENT_MAX_CONTEXT_TOKENS`, `AGENT_MEMORY_REQUIRE_CONFIRM`,
    `AGENT_MEMORY_AUDIT`, `AGENT_ACTOR`. New `make skipper-memory` target. 19 new agent tests.

## [0.27.2] — 2026-07-01

### Fixed

- **CI: declare `paramiko` for the SSH-transport tests.** With the ruff gate unblocked by
  v0.27.1, the `test:examlops` job reached the test suite and failed 3 of 584 tests
  (`tests/unit/test_executor.py`) with `ModuleNotFoundError: No module named 'paramiko'`.
  paramiko is a runtime dependency of the `examlops-pipelines` package, but CI installs only
  the root workspace `.[dev]`, which never declared it — so it passed locally (paramiko present
  in `.venv`) and failed only in CI's clean install. Added `paramiko>=3.5.0` to the root
  `[project.optional-dependencies].dev`. Verified locally: `uv pip compile --extra dev` resolves
  `paramiko==5.0.0` and the 9 `test_executor.py` tests pass.

## [0.27.1] — 2026-07-01

### Fixed

- **CI: `ruff format` compliance (unblocks the v0.27 pipeline).** The `test:examlops`
  GitLab job failed at `ruff format --check` (exit 1) on the `v0.27.0` tag because two
  files were not formatted to ruff's style:
  `platform/cli/src/examlops/cli/commands/ab_cmd.py` and
  `platform/cli/src/examlops/cli/commands/models.py`. Reformatted both (line-wrapping of
  long `error()`/`typer.Option(...)` calls only — no behavior change); the full
  `ruff format --check` now passes (264/264 files). Lint (`ruff check`) was already green.

## [0.27.0] — 2026-07-01

### Added

- **HPC scheduler abstraction — Flux adapter + SSH transport (Phase 23).** The platform can
  now submit real training jobs to a Flux cluster (the actual `lxp-cpu01/02` scheduler),
  not just Slurm, and over SSH from the Docker Prefect worker (no shared filesystem needed).
  - Two orthogonal axes: **scheduler backend** (`mock`/`slurm`/`flux`, via a shared
    `SchedulerAdapter` Protocol + `BasePollingAdapter` in `scheduler.py`) and **transport**
    (`local`/`ssh`, via a `RemoteExecutor` Protocol in `executor.py` — `LocalExecutor` and a
    paramiko-based `SSHExecutor` with SFTP staging + single-reconnect keepalive).
  - New `FluxAdapter` (`flux batch` / `flux jobs` / `flux job info`) with F58 job-id parsing,
    Flux state/result → normalized terminal-state mapping, and `HH:MM:SS`→FSD time conversion.
  - Selection via `EXAMLOPS_HPC_SCHEDULER`; `EXAMLOPS_SLURM_MODE` and all `EXAMLOPS_SLURM_*`
    keys remain fully backward-compatible (`get_slurm_adapter()` kept as an alias).
  - Scheduler-neutral resources threaded through the pipeline: adds
    `EXAMLOPS_HPC_{GPUS,ACCOUNT,QOS,CONSTRAINT,NTASKS}` plus `EXAMLOPS_HPC_*` mirrors of the
    Slurm resource vars; each backend translates them to its own flags.
  - New `hpc_jobs` tracking table (`platform_db.record_hpc_job`/`update_hpc_job`/`get_hpc_jobs`),
    idempotent on `(scheduler, job_id)`.
  - `exa models cost --record` now reads a scheduler-neutral `hpc_job_id` MLflow tag (closing
    the previously-dangling `slurm_job_id` contract) and supports Flux via `_real_flux_cost`
    plus a `CPU_COST_PER_HOUR` term so CPU-only Flux runs show non-zero cost.
  - New unit tests: `test_flux_adapter.py`, `test_executor.py`, `test_scheduler_factory.py`
    (+ updated `test_slurm_faults.py` / `test_pipeline.py`). SOTA study + ADR 0002 in `design/`.
- **Ground-truth feedback loop — `exa eval feedback` (#9).** Closes the loop between
  predictions and delayed real-world labels to measure *live model quality*, not just a
  drift proxy.
  - New `exa eval feedback ingest` (single `--request-hash`/`--label` or bulk `--from-csv`
    with `request_hash,label[,source]` columns) records observed labels into a new
    `ground_truth` table, keyed by prediction `request_hash`.
  - `exa eval feedback join <model>` shows prediction/label pairs joined on `request_hash`
    (optionally filtered by MLflow `--alias`).
  - `exa eval feedback accuracy <model>` computes live RMSE/MAE over labelled predictions;
    `--record` persists them to the `live_metrics` table.
  - Backed by `platform_db` (`ground_truth` table + `write_ground_truth`,
    `join_predictions_with_truth`, `write_live_metric`/`get_live_metrics`); 5 new unit tests
    in `test_cli_feedback.py`.
- **Statistically-rigorous A/B testing — `exa serve ab analyze` (#13).** Turns the
  bookkeeping-only A/B path into real hypothesis testing.
  - New pure module `examlops/analysis/ab_stats.py`: Welch's unequal-variance t-test,
    two-proportion z-test, and `analyze_ab()` with a minimum-sample gate and a
    direction-aware winner (`--lower-is-better` for RMSE/latency).
  - `exa serve ab analyze <model>` runs the test over the recorded `ab_results` observations
    and reports p-value, significance, and the winning variant (`--alpha`, `--min-sample`,
    JSON output, audit event). No new dependency (uses the existing scipy).
  - 14 new unit tests (`test_ab_stats.py`, `test_cli_ab_analyze.py`).
- **FinOps + Green-AI carbon accounting — `exa finops` (#20).** Per-project budgets and
  energy/carbon reporting for HPC training.
  - New pure module `examlops/finops/carbon.py` (energy `kWh = gpu_hours × TDP/1000 × PUE`,
    `gCO₂e = kWh × grid_intensity`, budget-usage ratio; documented overridable defaults).
  - `exa finops budget set|status` — per-project (= namespace) GPU-hour / cost budgets,
    enforced against real `model_costs` spend joined through `namespace_models` (flags `OVER`).
  - `exa finops carbon estimate|record|report` — estimate, persist to a new `carbon_records`
    table, and aggregate energy/CO₂e per run/model.
  - `platform_db` gains `project_budgets`/`carbon_records` tables + `set/get_project_budget`,
    `write/get_carbon_record`, `get_project_consumption`, `list_project_budgets`. 11 new unit
    tests (`test_finops_carbon.py`, `test_cli_finops.py`). No new dependency.
- **Next-generation feature substrate (Phase 0 of the 20-feature roadmap).** `platform_db`
  now provisions 24 additive tables covering the full next-gen roadmap (autoscaling, LLM
  endpoints, online features, data versioning/contracts, predictions/ground-truth/live-metrics,
  labeling, A/B assignment, canary, eval, optimization, explanations, fairness, attestations,
  budgets/carbon/energy, compliance/retention) plus an idempotent `_migrate_columns()` that
  adds the HPO study/trial columns. All tables are inert until each feature's `EXAMLOPS_*`
  flag is enabled; existing sklearn/CPU paths are unchanged.

## [0.26.1] — 2026-07-01

### Changed

- **Control plane security & correctness hardening:**
  - Constant-time comparison for the bearer token (`_require_token`) and the GitLab
    webhook token (`_verify_gitlab_token`) — no longer leak via response timing
    (now matches the existing GitHub HMAC path).
  - `approve_model` atomically **claims** the pending approval (`pending → approving`)
    before calling Prefect, so two concurrent approvals for the same model can't both
    create a flow run (the loser gets 409); the claim reverts to `pending` if Prefect
    fails, keeping the approval retryable.
  - Malformed GitLab/GitHub webhook bodies now return a clean **400** instead of a 500.

### Fixed

- **Test isolation** — the SeanerBUS bridge / drift / metrics unit suites now pass
  standalone (`pytest <file>`), not only in full-suite order: a `tests/unit/conftest.py`
  pre-imports the real `prometheus_client` so the bridge tests' stub guard skips it,
  and the bridge test's `httpx` stub gained `Timeout`/`RequestError`/`ConnectError`
  (needed by the shared `examlops.resilience` timeout helper).

### Added

- Approval-race, Prefect-revert, and malformed-webhook regression tests in
  `platform/services/control_plane/tests/test_reliability.py`.

## [0.26.0] — 2026-07-01

### Added

- **Platform-wide fault-tolerance layer (`examlops.resilience`)** — one shared
  foundation replacing four incompatible ad-hoc retry idioms:
  `CircuitBreaker`/`CircuitOpenError`, `retry_call` + transient/lock classifiers,
  env-configurable HTTP timeouts (`httpx_timeout`), sync/async `request_json`,
  and a hardened SQLite helper (`db.connect`/`db.harden`/`db.write_retry`). Covered
  by `tests/unit/test_resilience.py` (17 tests, incl. an 8-thread concurrent-writer
  proof).
- **Fault-injection test suites** — `tests/unit/test_ray_serve_faults.py` (503-vs-404,
  health/ready split, predict timeout), `tests/unit/test_slurm_faults.py` (bounded
  wait, transient UNKNOWN, CLI timeout), and drift-regression tests in
  `tests/unit/test_seanerbus_bridge.py` (transport errors must not feed drift).

### Changed

- **Fault tolerance hardened across every runtime surface (~10× fewer single-point
  failures):**
  - **Shared DB** (`examlops.platform_db.get_db`): WAL + `synchronous=NORMAL` +
    `busy_timeout` + `check_same_thread=False` for all ~170 call sites and the 3
    concurrent long-lived writers — eliminates `database is locked` under contention.
  - **Ray Serve** (`serving/ray_serving/app.py`): MLflow HTTP timeout/retry (native
    env knobs), a hard `predict` timeout (504), a `/ready` liveness endpoint and a
    `/health` that returns **503 when degraded**, MLflow-unreachable surfaced as
    **503 not 404**, a robust poller (daemon-thread fallback + liveness flag), and
    `max_ongoing_requests`.
  - **Inference pipeline** (`serving/inference_pipeline/app.py`): router retries
    transient transport errors; traffic rules are now **durable** (persisted to
    `platform_db`, shared across replicas); added `/health`; 422 on bad rule values.
  - **SeanerBUS bridge**: **transport/infrastructure errors no longer feed the drift
    tracker** — fixes an outage masquerading as model drift and firing spurious
    retrains; fire-and-forget trigger tasks are strong-referenced; shared timeouts.
  - **Skipper agent**: SQLite checkpointer hardened (WAL + busy_timeout + absolute
    path + in-memory fallback); `check_backend` now actively probes Azure/Claude
    reachability instead of assuming healthy from env-var presence.
  - **Pipelines & Slurm**: retries + exponential backoff + `timeout_seconds` on the
    6 I/O Prefect tasks; the Slurm adapter now bounds `wait_until_complete`
    (`_MAX_WAIT_S`), times out every `sbatch`/`squeue`/`sacct` call, and treats a
    transient `UNKNOWN` state as recoverable instead of terminal.
  - **Infra** (`docker-compose.yml`): a healthcheck on ray-serving, env-overridable
    `mem_limit` across services (OOM isolation), and `service_healthy` (was
    `service_started`) for dashboard→control-plane and bridge→ray-serving; bridge
    `restart: on-failure` → `unless-stopped`.

- **Renamed the management agent to "Skipper"** — the Python package `platform/services/agent/exa_agent/` → `skipper/` (all `from exa_agent…` imports updated across source and tests; git-tracked as renames so history is preserved). Renamed to avoid a name clash with the separate **ExaAgent** product. Branding updated in the system prompt (`You are Skipper …`), the CLI banner/prompt (`skipper >`), the web chat UI (`<title>Skipper · ExaMLOps</title>`), and the FastAPI title (`Skipper (ExaMLOps agent)`).
  - **Makefile**: primary targets are now `make skipper` / `skipper-server` / `skipper-chat` / `skipper-test`. The former `agent` / `agent-server` / `agent-chat` / `agent-test` targets are retained as backward-compatible aliases.
  - **Unchanged (deliberate, low-risk)**: the `platform/services/agent/` service directory, all `AGENT_*` env vars (`AGENT_SERVER_PORT`, `AGENT_API_KEY`, `AGENT_DB`, …), and the `agent.py` / `agent_server.py` entry-script filenames — so deployed `.env` files and docker-compose/CI paths keep working.

## [0.25.0] — 2026-07-01

### Added

- **OpenAI-compatible chat bridge for the kube-q (`kq`) client** (`platform/services/agent/skipper/oai_compat.py`) — new `POST /v1/chat/completions` (SSE when `stream=true`, JSON otherwise) and `GET /healthz` endpoints that translate the OpenAI Chat Completions wire format onto the ExaMLOps LangGraph agent. This lets the generic `kq` terminal client (`kq --url http://localhost:18004`) drive the real agent — with all its tools, prompts, and the write-tool confirmation gate — **unforked, straight from PyPI**, so one chat client can target ExaMLOps or any other agentic backend by URL/profile.
  - Conversation state is kept server-side by the LangGraph SQLite checkpointer, keyed by the `X-Session-ID` header → `thread_id` (matches `kq`'s latest-message-only payload).
  - HITL: a LangGraph `interrupt()` (write-tool confirmation) becomes a final chunk carrying `hitl_required=true` + `action_id`; `/approve`/`/deny` are relayed to the graph as `Command(resume=…)`. Tool activity is surfaced via the `ki_event` side-channel.
  - Optional bearer auth via the new `AGENT_API_KEY` env var (unset ⇒ the bridge is open, the local-dev default; when set, clients send `Authorization: Bearer <key>`).
  - New `make agent-chat` target launches `kq` against a running `agent-server` (installs `kube-q` if missing); `make agent-server` now advertises the bridge. Usage README + `kq` profile template under `platform/services/agent/kube-q/`.
  - Covered by `platform/services/agent/tests/test_oai_compat.py` (8 tests). Docs updated: `docs/guides/agent.md`, `docs/reference/env-vars.md`, `docs/reference/commands.md`, `docs/guides/interfaces.md`.

## [0.24.1] — 2026-06-24

### Added

- **`host.docker.internal` mapping for the SeanerBUS bridge** — added `extra_hosts: ["host.docker.internal:host-gateway"]` to the bridge service in `docker-compose.yml` so `SEANERBUS_HOST=host.docker.internal` reaches a SeanerBUS running bare-metal on the host (Linux Docker needs the explicit host-gateway alias). Documented in `docs/guides/seanerbus-sim.md`.

### Changed

- **SeanerBUS bridge host-configuration docs** — expanded the root and compose-dir `.env.example`, plus `docs/guides/seanerbus-sim.md`, with the bridge→SeanerBUS connection modes (container name / `host.docker.internal` / Docker bridge gateway IP), the "`localhost` ≠ host inside a container" gotcha, the bare-metal `0.0.0.0:5398` bind requirement, and the root-`.env` interpolation precedence (the `environment:` block overrides `env_file:`).

## [0.24.0] — 2026-06-24

### Added

- **Self-healing Docker-egress sidecar** (`platform/infra/firewall-fix/`) — a stopgap for `lxp-cpu01`, where a firewalld quirk leaves a stray `ip filter forward` nftables chain with `policy drop` that kills all Docker-bridge egress (containers lose DNS + TCP to internal GitLab, MinIO, external APIs). A tiny privileged host-network `alpine` + `nft` sidecar idempotently re-inserts `nft insert rule ip filter forward ip saddr 172.16.0.0/12 accept` whenever the drop chain wipes it. Matching the whole Docker IPAM range (not a `br-…` bridge name) means it survives `docker compose down/up` bridge renames and firewalld reloads — the exact failure mode that broke `exa modelzoo sync` after a CI deploy renamed the `examlops_default` bridge. Needs Docker access only (no host sudo). New Makefile targets: `make firewall-fix-up` / `firewall-fix-down` / `firewall-fix-logs`. Stopgap until the sysadmin installs a host-level systemd one-shot ordered after `firewalld.service`/`docker.service`.
- **Model Rollback** (exa models rollback) — roll back Production alias to any previous version; history in model_rollbacks table.
- **Data Quality Gates** (exa pipeline quality) — validate dataset schema/completeness pre-training; results in data_quality_checks.
- **Shadow Deployment** (exa serve shadow) — configure traffic mirroring to shadow alias; results in shadow_results.
- **A/B Testing** (exa serve ab) — controlled experiments comparing two variants; experiment state in ab_tests/ab_results.
- **Batch Inference** (exa serve batch) — synchronous batch inference from JSON/JSONL input files; job records in batch_jobs.
- **Explainability/XAI** (exa serve explain) — feature importance via /explain endpoint; graceful fallback when SHAP absent; logged in explain_logs.
- **HPO** (exa pipeline hpo) — trigger and track hyperparameter optimisation studies via Prefect; hpo_studies/hpo_trials tables.
- **Model Cards** (exa models card) — generate standardised Markdown cards from YAML + MLflow metadata; history in model_cards.
- **Feature Store** (exa features) — versioned local feature file store; metadata in feature_versions.
- **Namespaces** (exa namespace) — project namespace isolation; namespaces/namespace_models tables; model-to-namespace assignment.

### Fixed

- **`exa pipeline deploy` fell back to an ephemeral Prefect server** — `pipelines/deploy.py` resolved `PREFECT_API_URL` only for the log banner; the Prefect SDK never saw it, so an unset variable made `.serve()` spin up a throwaway ephemeral server (`Cannot schedule flows on an ephemeral server…`) and the deployment never landed on the real `:14200` server. `deploy()` and `deploy_from_registry()` now write the resolved URL into `os.environ` before serving.
- **Dashboard "Run CI Pipeline" error message** — `apiFetch` was discarding the response body on non-2xx, so API errors showed only "API error 503" with no actionable detail. Now reads the JSON `detail` field and propagates it to the UI (e.g. "GitLab pipeline token not configured. Set AI_PROD_PIPELINE_TRIGGER_TOKEN in .env…"). Added `retry: false` to `useModelzooStats` and `useModelzooDatasets` so a failing GitLab connection clears the loading skeleton in one timeout instead of retrying three times with exponential backoff.
- **Dashboard "Run CI Pipeline" 503 on fresh installs** — the `/api/modelzoo/trigger-pipeline` endpoint required a pipeline trigger token stored in the Config DB. Now falls back in order: (1) DB token, (2) `AI_PROD_PIPELINE_TRIGGER_TOKEN` env var (triggers the ai-production project #88 pipeline), (3) `GITLAB_TOKEN` personal access token via the regular pipeline-create API. Works locally without extra Config setup as long as `GITLAB_TOKEN` is set in `.env`.
- **Dashboard Models page blank registry** — `ControlPlaneClient` now retries transient control-plane transport errors (`ConnectTimeout`/`ConnectError`/`ReadTimeout`) with backoff and a forgiving connect timeout. The single-worker control plane intermittently could not accept a connection within the old 5 s deadline under steady dashboard polling, so a single blip errored `GET /models` and blanked the Model Registry section. `list_registry` also skips a model on a transient `get_meta` error instead of failing the whole page.
- **Dashboard Audit page blank** — `GET /api/platform-audit` returned a bare list, but the frontend expects `{items, total}`; the mismatch crashed the render. Now returns the wrapped shape.
- **`exa modelzoo sync` timeout** — the CLI hard-coded a 10 s client timeout while the control plane makes a blocking GitLab API call before responding; `post()` now accepts a `timeout` and `modelzoo sync` uses 30 s.
- **GitLab CI/CD pipeline repeatedly broken (no auto-deploy)** — fixed six independent blockers so `deploy:lxp` runs and `smoke:lxp` passes: (1) 5 unused-import ruff errors in v0.24.0 feature tests; (2) `ruff format --check` drift — pinned `ruff==0.15.6` and formatted the codebase so the formatter is deterministic across CI runs (the loose `ruff>=0.4` let CI install a newer formatter than the code was written with); (3) a unit-test mock that didn't accept the new `timeout` kwarg; (4) dashboard tests — docs router returned 503 in CI (now sets `EXAMLOPS_DOCS_ROOT`) and `test_storage` failed on a moto/aiobotocore `raw_headers` incompat (skipped in CI, bumped `moto` 5.0.10→5.2.2); (5) flaky `uv venv .venv` "already exists" (the cache restores `.venv`) — now `uv venv --clear`, and `test:modelzoo` is `allow_failure` so the read-only upstream mirror never gates the deploy; (6) `smoke:lxp` health check probed a non-existent container service `prefect` (the compose service is `orchestrator`) — it reported `unknown` and triggered an unwarranted auto-rollback; now checks `orchestrator`, which reports `healthy`; (7) `post-deploy:lxp:retrain-push-models` timed out (15 s) triggering Prefect retrains right after a fresh deploy while Prefect was still warming up — bumped to 60 s and made the job `allow_failure` (a best-effort retrain kickoff must not fail an already-validated deploy).

### Changed

- **Dashboard Models registry endpoint** — fetches per-model metadata concurrently (was N+1 sequential round-trips to the control plane) and adds a 15 s server-side cache, so a hard browser refresh renders the model cards near-instantly instead of holding on skeletons.
- **`moto[s3]`** pinned `5.0.10 → 5.2.2`; **`ruff`** pinned to `==0.15.6` for deterministic formatting in CI.

### Removed

- **`DemoAnomaly` demo model (prototype — never released)** — a self-contained dummy anomaly detector (model, `SyntheticAnomalyDataset`, per-model config/YAML, and a SeanerBUS request generator) was prototyped during this cycle and then removed before the v0.24.0 tag, so it ships in no release. The active model registry is `JPCP / MACK / MCBound`. The runbook at `docs/tutorials/demoanomaly-e2e-demo.md` is intentionally kept as a step-by-step reference for wiring a model end-to-end.

## [0.23.1] — 2026-06-12

### Fixed

- **MLflow 3.x compatibility** — migrated all REST calls from deprecated `/ajax-api/2.0/mlflow/` to `/api/2.0/mlflow/` across CLI, dashboard backend, control plane, and agent tools; `registered-models/list` → `registered-models/search`.
- **Dashboard TypeScript build** — removed unused `GitBranch` import and added missing type fields (`hyperparameters`, `lifecycle_gates`, `retraining`, `seanerbus_uuid`) to `ModelDetailResponse` interface.
- **`exa status` dashboard unreachable** — health probe cache used per-Host-header keys so Docker healthcheck (`localhost`) never warmed the cache for the control plane (`dashboard:8099`); switched to a global cache key with per-response URL rewriting. Increased control-plane dashboard probe timeout from 5 s to 12 s.
- **`exa doctor` warnings** — created missing config file at `~/.config/examlops/config.toml` on LXP; added `CONTROL_PLANE_TOKEN` to the LXP `.env`.

### Added

- **`make rebuild` / `rebuild-all` / `lxp-rebuild`** — force-rebuild all Docker images (`--no-cache`) and restart the full stack, including monitoring and JupyterHub profiles.

## [0.23.0] — 2026-06-12

### Added

- **CI/CD: `smoke` stage** — post-deploy `platform/ci/smoke_check.sh` probes critical endpoints (Dashboard, Control Plane, MLflow, Prefect) and non-critical (Ray Serve, MinIO, Prometheus, Grafana). Auto-rollback to previous SHA on failure.
- **CI/CD: runtime self-healing** — `platform/ci/self_heal.sh` scans all Docker Compose containers and restarts exited/unhealthy ones. `setup_selfheal_systemd.sh` installs a systemd timer (every 2 min, persistent) on lxp-cpu01.
- **CI/CD: failure notifications** — `platform/ci/notify_failure.py` sends a Slack-compatible webhook payload when any CI job fails (requires `NOTIFICATION_WEBHOOK_URL` masked CI variable).
- **`exa pipeline add-model <Name>`** — register an existing ModelZoo class into the training/serving pipeline without re-scaffolding; verifies model class exists in modelzoo, generates YAML + config shim only.
- **`tools/scaffold_model.py --skip-model-class`** — pipeline-only scaffold (YAML + config shim); used by `exa pipeline add-model`.
- **Control plane `/models/{name}/meta`** — response now includes `seanerbus_uuid`, `hyperparameters`, `prefect` config, and `enabled` flag from model YAML.
- **Dashboard `ModelDetail`** — lifecycle gates section (color-coded Staging/Canary/Production cards with metric/threshold/direction); retraining schedule section (cron, deployment name, work pool, concurrency); hyperparameters in Technical section.
- **`make smoke-check`** — run `smoke_check.sh` against the local stack.
- **`make selfheal`** — run `self_heal.sh` once against the local stack.

### Fixed

- Control plane test assertion: `"not declared as supported"` → `"not supported by"` to match actual error message.

## [0.22.0] — 2026-06-12

### Added

- **`exa doctor`** — comprehensive self-diagnosis command: checks config file, API token, all 5 services (control plane, MLflow, Prefect, Ray Serve, dashboard), platform DB, Python ≥3.12, Docker availability. Color-coded checklist with actionable fix hints per failure.
- **`--version` / `-V`** global flag — print installed `examlops` package version and exit.
- **`--yes` / `-y`** global flag — skip all confirmation prompts; enables fully non-interactive CI scripts.
- **`exa approvals delete <uuid>`** — retract a stale or duplicate pending approval by UUID.
- **`exa models cost-list`** — cross-model HPC cost summary (latest recorded cost per model).
- **`exa pipeline promote-delete [MODEL|--all]`** — delete saved promotion rules (with confirmation).
- **`exa modelzoo config-set <key> <value>`** — write a ModelZoo config key via the control plane PUT endpoint.
- **`exa stack monitoring-up/down/status`** — CLI shortcuts for monitoring Docker Compose profile (Prometheus, Grafana, Loki, Promtail, Alertmanager, Tempo).
- **`exa drift snapshots <MODEL>`** — inspect raw prediction drift snapshots (`--last N`, `--raw`).
- **`exa drift input reset <MODEL>`** — clear input embedding snapshots for a model.
- **`exa serve models [--detail]`** — list models hot-loaded in Ray Serve.
- **`exa serve traffic-list`** — aggregate traffic split view across all models.
- Dashboard: **Drift monitor page** — 3-tab view: prediction drift, input embedding drift, auto-retrain config.
- Dashboard: **Audit page** — two-tab layout: Platform Ops (audit_events) + Config Changes.
- Dashboard: `ModelDetail` — pre-populate inference JSON from `input_schema`; show `CostHistory` component.
- Dashboard backend routers: `drift_data` (prediction/input drift, auto-retrain), `platform_audit` (audit events), `platform_data` (traffic rules, promotion rules).
- Control plane: `/health` returns `db_ok` (SQLite connectivity) and `poller.alive` (poller thread liveness).
- Control plane: `DELETE /approvals/{approval_id}` endpoint.
- **Agent: dual LLM backend** — `skipper/llm.py` now prefers Claude API (`ANTHROPIC_API_KEY`) over Ollama; `ChatAnthropic` with adaptive thinking (`claude-opus-4-8`); falls back to `ChatOllama` when key is absent. New `check_backend()` returns `{type, model, ok}` dict.
- **Agent: streaming CLI (10 extra features)** — `graph.stream(stream_mode="messages")` token-by-token output; ANSI colors (`_C` class); token/cost display per turn (`_COST_PER_1M`); `/history [n]` conversation replay; `/export [file]` thread export; `/grep <pattern>` message search; `/watch <secs> <query>` auto-refresh loop; `/model <name>` live model switch; HTTP retry with exponential backoff (`retries=2`, `RequestError` only); startup health brief (probes 3 services at boot).
- **Agent: web chat UI** (`make agent-server`, port 18004) — FastAPI server (`skipper/server.py`) with WebSocket streaming; dark-theme self-contained chat HTML (`skipper/chat_html.py`); REST endpoints `GET /`, `/api/info`, `/api/threads`, `/api/threads/{id}/history`; write-confirm modal; Markdown rendering via marked.js + highlight.js; auto-reconnect; copy-to-clipboard; token count per message.
- **Agent tools**: `get_platform_summary` (cross-service health + drift snapshot in one call) and `diagnose_platform` (root-cause checks for common failure modes) added to `platform_ops` group (total 11 tools in that group).

### Changed

- `_output.py`: added `warning()`, `info()`, `hint()` functions for visual hierarchy; `spinner()` context manager; `confirm()` respects `--yes` mode; `error()` accepts `hint=` for actionable next steps; `print_table()` shows row count in caption.
- `_client.py`: improved error messages — 401→token hint, 403→permission hint, 409→surfaces API detail, 429→shows Retry-After, 5xx→suggests `exa stack logs`, connection refused→`exa stack up`, timeout→`exa status`.
- `exa status`: spinner during health checks, color-coded service status, port hints per service.
- `exa retrain`: spinner, structured `print_record` output, post-success hint.
- `exa approvals approve/reject`: spinner, structured output, confirmation prompt on reject.
- `exa serve reload/check/infer-check`: spinner, contextualized error hints.
- `exa serve traffic` (write path): confirmation prompt before applying split.
- Dashboard `Config.tsx`: loads `mzAutoRetrain` initial state from control plane on mount.
- Dashboard `Pipelines.tsx`: fix hardcoded `dummy:true` bug in run trigger.

### Fixed

- MLflow metrics/params returned as lists — `exa models diff` and `exa models lineage` now convert to dicts.

## [0.21.0] — 2026-06-11

### Added

- Monitoring: comprehensive Grafana dashboard overhaul — 7 dashboards (overview, online metrics,
  control plane, drift, approvals, SeanerBUS, logs); SLO error-budget panels; per-model latency
  tables; drift input embedding panels; approval funnel and SLA risk gauges.
- Alertmanager: 25 alert rules across 4 groups with `promtool` CI validation (`make alerts-check`).
- Control plane round 2 (app v0.13.0): JSON structured logging (`LOG_FORMAT`), Prefect circuit
  breaker (`PREFECT_CB_FAIL_MAX`, `PREFECT_CB_RESET_TIMEOUT`), retrain Prometheus metrics
  (`examlops_retrain_requests_total`, `examlops_retrain_duration_seconds`,
  `examlops_prefect_circuit_breaker_opens_total`, `examlops_approvals_expired_total`),
  `X-Request-ID` middleware, `/ready` liveness probe (always 200), approval auto-expiry
  (`APPROVAL_EXPIRY_HOURS`), `X-Idempotency-Key` deduplication (`IDEMPOTENCY_TTL_SECONDS`),
  security headers middleware, `POST /admin/reload` hot-reload endpoint, DB NFS retry with
  exponential backoff. Total: 20 production-grade improvements across two batches.

## [0.20.0] — earlier

Phase 0–21 shipped. See `CLAUDE.md` Phase rollout table for scope per phase.
