# Changelog

All notable changes to ExaMLOps are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), versioning: [SemVer](https://semver.org/).

## [Unreleased]

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
