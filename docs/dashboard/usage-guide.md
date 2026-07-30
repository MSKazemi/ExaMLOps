# Dashboard Usage Guide

The **ExaMLOps dashboard** is a single FastAPI service that serves a React SPA plus a JSON
API. It is the browser-based control plane over the whole platform — MLflow, Prefect,
Ray Serve, MinIO, Grafana/Prometheus, the HPC schedulers, the control plane, and the
`platform.db` operational store — surfaced as consoles, health checks, deep links, and a
reverse proxy. Almost every write on the dashboard calls the **same `examlops.*` /
`platform_db` code paths as the `exa` CLI**, so the UI never drifts from the CLI and there
is a natural CLI equivalent for most actions.

- **URL:** http://localhost:18099 (remote `lxp-cpu01`: `23.109.46.77:18099`, or via `ssh lxp` port-forward)
- **API docs (Swagger):** http://localhost:18099/api/docs (or `/docs`)

## Login & roles

There is no user table — the dashboard has exactly **two shared-password roles**:

| Role | Password env var | Can do |
|---|---|---|
| **viewer** | `DASHBOARD_VIEWER_PASSWORD` | Read everything; secret values render as `***`; no writes |
| **admin** | `DASHBOARD_ADMIN_PASSWORD` | Everything a viewer can, plus all mutations (edit config, write/rotate secrets, promote, approve, trigger, start/stop services, manage projects), and read the audit log |

Login exchanges the password for an HS256 JWT (`POST /api/auth/login`, TTL default 12h,
stored in browser `localStorage`). When it expires the next API call 401s and the SPA
re-prompts. Role gating is enforced at the backend (BFF), not just hidden in the UI: a
viewer hitting an admin route gets a 403 with a `deny_reason`. Throughout this guide,
actions marked **(admin)** are admin-only; everything else is viewer-visible.

## Navigation & layout

The left sidebar groups ~40 consoles into six lifecycle groups plus a Home item and a
footer of utility links. URLs are lifecycle-scoped (e.g. `/build/models`,
`/operate/drift`); old flat paths (`/models`) redirect for one release.

| Group | Consoles |
|---|---|
| **Home** | Overview |
| **Build** | Models · MLOps · Datasets · Features · Pipelines · Prompts |
| **Serve** | LLMOps · Traffic · Gateway · Scaling · Next-Gen |
| **Operate** | Drift · Alerts · Autopilot · SLOs · Admission · Facility · FinOps · Self-Obs |
| **Govern** (admin) | Compliance · Audit · Approvals · Fairness · Secrets |
| **Platform** | Projects · Events · Services · Providers · Config · SeanerBUS · Jupyter · Flags (admin) |
| **Utility (footer)** | Documents · Preferences |

Cross-cutting tools available on every page: the **Command Palette** (⌘K / Ctrl-K), the
**Copilot** launcher (bottom-right), the **Help drawer** (`?`), theme toggle, and the
per-entity **watchlist** star. Some consoles are feature-flagged (MLOps `mlopsConsole`,
Facility `facilityConsole`, Projects `projectsConsole`) and only appear when their flag is
on.

**Reaching Documents:** sidebar footer → **Documents**, or navigate to `/documents`. It
renders the repo's README + `docs/` tree in-app.

---

## Overview  <!-- (role: viewer) -->

The command-center landing page: platform health at a glance — a live "Platform Active"
hero with the ModelZoo last commit, stat tiles, quick actions, an architecture flow
diagram, an embedded Grafana metrics panel, and per-service health cards. Use it to
confirm the platform is up and jump into any area.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View platform status | Shows service count, online count, models loaded, ModelZoo/dataset counts, and per-service `ok/degraded/down` badges | First thing you check each session — is everything healthy | Sidebar → Overview (or `/`) | `exa status` |
| Quick actions | Shortcut buttons into common flows (command-center) | Fast jump to frequent tasks | Overview → Quick Actions row | — |
| Open a service | Deep-link to a service's own UI (MLflow, Grafana, …) | Drill into a specific tool | Click a Service Health card | — |
| Jump to Models / Datasets | Stat tiles link into those consoles | Navigate by the numbers | Click Models Loaded / ModelZoo / Datasets tile | `exa models list` / `exa data list` |

---

## Models  <!-- (role: viewer / admin) -->

Three stacked sections: **GitLab ModelZoo** (auto-discovered upstream model library),
**Model Registry** (models versioned in MLflow, with ModelZoo freshness badges
CURRENT/UPDATED), and **Ray Serve** (models actively serving inference right now). Plus a
"Recent ModelZoo Pushes" feed. Use it to see what exists, what's live, and to scaffold new
models.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View model list | Lists ModelZoo, registry, and live Ray Serve deployments with version/run/status | See every model and which are actually serving | Sidebar → Models | `exa models list` |
| Open a model | Navigate to the Model Detail page | Inspect versions, run inference, edit README | Click any registry/ModelZoo card | — |
| Reload Ray Serve | Re-pulls the alias→version hot set into Ray Serve | Make a just-promoted version go live | Models → **Reload Ray Serve** | `exa serve reload` |
| Sync from GitLab | Re-fetches ModelZoo models/stats from GitLab | Pick up a new upstream model push | Models → **Sync from GitLab** | — |
| New Model **(admin)** | Opens the ScaffoldWizard to generate a new model skeleton | Add a new model to the platform | Models → **New Model** | `exa scaffold <Name> --task … --type …` |
| Open Ray Serve docs | Opens the live deployment's OpenAPI docs | Inspect a serving endpoint | Click a live Ray Serve card | — |

## Model Detail  <!-- (role: viewer / admin) -->

Everything about one model: stage badges (Production/Canary/Staging), README (rendered
markdown), technical schema, lifecycle gates, retraining schedule, links, HPC cost
history, a try-it-out inference form, a versions tab with alias management, and a
collaboration discussion thread. Reached at `/build/models/:name`.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Read the model card | Renders the model's README/description + technical schema, promotion gate, hyperparameters | Understand what a model does and how it's configured | Model Detail → Overview tab | — |
| Try-it-out inference | Sends a JSON feature payload to a chosen stage (Production/Canary/Staging) and shows the prediction; pre-fills payload from the input schema | Smoke-test a live model from the browser | Overview → **Try it out** → pick Stage → edit Features JSON → **Send** | `exa serve infer-check` |
| View versions & metrics | Lists every MLflow version with date, framework, aliases, metrics | Compare versions, see what's promoted where | Model Detail → **Versions** tab | `exa models diff <name> <vA> <vB>` |
| Promote to an alias **(admin)** | Sets Staging/Canary/Production alias on a version (Production auto-archives the previous prod), with a confirm dialog | Move a validated version up the lifecycle | Versions tab → row **Promote ▾** → pick alias → **Confirm** | `exa pipeline promote <name> --if-rmse-lt …` |
| Archive a version **(admin)** | Sets the Archived alias | Retire an old version | Versions tab → **Archive** | — |
| Remove an alias **(admin)** | Deletes an alias off a version | Undo a mis-set alias | Versions tab → trash icon next to an alias | — |
| Edit README **(admin)** | Opens the MDEditor to override the model description (markdown, live preview) | Document a model in-place | Overview → Description → **Edit** → **Save** | — |
| Revert README **(admin)** | Discards the override, reverting to the filesystem README (a drift banner warns when the upstream README changed) | Re-sync to the canonical docs | Description → **Revert** | — |
| Upload / delete image **(admin)** | Manages images embeddable in the README via `dashboard://image/<id>` | Add diagrams to a model card | Description → **Upload image** / hover a thumbnail → trash | — |
| Drift banner | Warns when the filesystem README changed since the override was saved | Know your model card is stale | Shown atop Description (admin) | `exa drift status` (for prediction drift) |
| Cost history | Table of GPU-hours + USD per version | Review a model's HPC spend | Model Detail → HPC Cost History (auto-shown when data exists) | `exa models cost <name>` |
| Discuss / share **(collab)** | Comment with @-mentions; share a frozen snapshot link | Coordinate with teammates on a model | Bottom of page → Discussion / **Share snapshot** | — |

---

## MLOps Console  <!-- (role: viewer) --> (flag: mlopsConsole)

Single pane for registry health and **governed promotion**: a registry grid (per-model
version, stage, health token, governed?, freshness) plus a guided promotion gate that says
inline whether a promotion is allowed and, if not, exactly why (missing/disabled policy,
awaiting approval). Read-only — it visualizes the decision the CLI enforces.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Review registry health | Per-model health (`ok`/`warn`/`unknown`), governed flag, freshness | Spot ungoverned or stale models before promoting | Sidebar → MLOps | `exa drift status` / `exa models lineage` |
| Read the promotion gate | Shows the policy, approval step, and the one-line verdict (Blocked / Eligible / Ready) | Decide if a model is safe to promote | MLOps → select a model row | `exa pipeline promote <name> --if-rmse-lt …` |

## Datasets  <!-- (role: viewer / admin) -->

Datasets auto-discovered from the ModelZoo GitLab repo, cross-referenced with which models
use them.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View dataset list | Lists GitLab-discovered datasets + model usage | See available training data | Sidebar → Datasets | `exa data list <name>` |
| Sync from GitLab | Refreshes the dataset list | Pick up a new dataset | Datasets → **Sync from GitLab** | — |
| Run CI Pipeline **(admin)** | Triggers a GitLab CI pipeline and polls its status | Kick a data/CI job from the UI | Datasets → CI Pipeline card → **Run CI Pipeline** | — |
| Open in GitLab | Deep-links a dataset file in GitLab | Inspect the raw definition | Per-dataset **View in GitLab** | — |

## Features  <!-- (role: viewer / admin) -->

Feature-store view registry — shared train/serve feature definitions.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Browse feature views | Lists registered views (entity, TTL, features) | See shared feature definitions | Sidebar → Features | `exa feature` |
| Register / patch a view **(admin)** | Creates or updates a feature view (name/entity/TTL/features) | Add or evolve a feature definition | Features → **Register / patch a view** → **Apply view** | `exa feature apply` |

## Pipelines  <!-- (role: viewer / admin) -->

Prefect training deployments and recent run history: per-deployment status, schedule,
last-run state, and (admin) a trigger button. Plus the last 30 runs.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View deployments | Lists Prefect deployments with status/schedule/last-run | See what's scheduled and its health | Sidebar → Pipelines | `exa pipeline list` |
| View recent runs | Last 30 runs with state + start time | Check whether training succeeded | Pipelines → Recent Runs | — |
| Trigger a run **(admin)** | Fires a Prefect training run for that model's deployment | Manually retrain now | Pipelines → deployment row → **Trigger** | `exa pipeline run --model <M> --dataset <D>` |
| Refresh | Re-fetches deployments + runs | Get fresh status after a trigger | Pipelines → **Refresh** | — |

## Prompts  <!-- (role: viewer / admin) -->

Versioned prompt registry — immutable prompt versions with moving labels (e.g.
`production`).

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Browse prompts | Lists prompts, versions, and labels | Track prompt history | Sidebar → Prompts | `exa prompt` |
| New version **(admin)** | Creates an immutable prompt version (name/label/template) | Iterate a prompt safely | Prompts → **New version** → **Create** | `exa prompt` |
| Point a label **(admin)** | Moves a label (e.g. production) to a chosen version | Promote/rollback a prompt | Prompts → **Point label** → pick version → **Apply** | `exa prompt` |

---

## LLMOps Console  <!-- (role: viewer) -->

Read-only view of the LLM-serving substrate: the LLM endpoint registry (engine, HF model
id, tensor-parallel size, dtype, enabled) and per-model continuous-eval scores with
pass/fail badges. Surfaces without a wired backend degrade to "not yet available".

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View LLM endpoints | Lists each LLM endpoint's config + status | See what LLMs are served and how | Sidebar → LLMOps | — |
| Review eval scores | Latest eval suite/status per model with metric pass/fail + pass-rate pill | Judge LLM quality before relying on it | LLMOps → Continuous eval | `exa eval run` / `exa eval gate` |

## Traffic  <!-- (role: viewer / admin) -->

A/B testing + shadow-deployment console over serving traffic rules. Viewers see the
current rules; admins configure them (controls render disabled for viewers).

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View traffic split | Shows the production/canary split and A/B or shadow config for a model | See how inference traffic is routed | Sidebar → Traffic | `exa serve traffic <M>` |
| Start A/B test **(admin)** | Splits traffic between variant A and B at a chosen % | Compare two versions on live traffic | Traffic → set Variant A/B + Split% → **Start test** | `exa serve ab` / `exa serve traffic <M> --production N --canary M` |
| Stop A/B test **(admin)** | Ends the A/B split | Conclude an experiment | Traffic → **Stop test** | `exa serve ab analyze` (to read the result) |
| Enable shadow **(admin)** | Mirrors traffic to a shadow alias without serving its output | Validate a candidate risk-free | Traffic → set Shadow alias → **Enable** | `exa serve shadow` |
| Disable shadow **(admin)** | Stops shadowing | End shadow validation | Traffic → **Disable** | `exa serve shadow` |

## Gateway  <!-- (role: viewer / admin) -->

LLM gateway virtual-key management — issue scoped, budgeted API keys and revoke them.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View virtual keys | Lists issued keys with project/models/budget/status | Audit who can call the gateway | Sidebar → Gateway | `exa gateway` |
| Issue a key **(admin)** | Creates a scoped, budgeted virtual key; the raw key is shown once | Grant a team/app LLM access | Gateway → **Issue key** (Project/Models/Budget) | `exa gateway` |
| Copy the raw key | One-time copy of the freshly issued secret | Hand the key to the consumer | Gateway → **Copy** (after issue) | — |
| Revoke a key **(admin)** | Disables a key (with confirm) | Cut off a compromised/retired key | Gateway → key row → **Revoke** | `exa gateway cache` (related), `exa gateway` |

## Scaling  <!-- (role: viewer / admin) -->

Per-model autoscaling + inference-routing config.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View scaling policy | Shows autoscale (min/max replicas, metric, target, scale-to-zero) + routing mode | See how a model scales | Sidebar → Scaling | `exa serve autoscale` |
| Set autoscale policy **(admin)** | Configures replicas/metric/target/scale-to-zero | Right-size a model under load | Scaling → Autoscale form → **Set policy** | `exa serve autoscale` |
| Set routing **(admin)** | Sets routing mode (round-robin/cache-aware), SLO latency, disaggregation | Tune inference routing | Scaling → Routing form → **Set routing** | `exa serve routing` |

## Next-Gen  <!-- (role: viewer) -->

Read-only roll-up of the "Next-Gen 40" surfaces — federated training, device pools,
placements, cloud bursts (KPI tiles + tables only; no writes yet).

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Review next-gen state | Shows federated/hardware/placement/burst rollups | Preview forward-looking capabilities | Sidebar → Next-Gen | `exa federated …` / `exa hardware add-pool …` / `exa hardware place …` |

---

## Drift  <!-- (role: viewer / admin) -->

Drift monitoring across three tabs: **prediction drift**, **input (embedding) drift**, and
**auto-retrain config**. Viewers read; admins manage baselines/resets and auto-retrain.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View prediction drift | Per-model drift status vs baseline | Catch models whose predictions are drifting | Sidebar → Drift → Prediction tab | `exa drift status` |
| View input drift | Embedding norm/mean/std vs baseline | Detect input-distribution shift | Drift → Input tab | `exa drift input status` |
| Set drift baseline **(admin)** | Stores current stats as the baseline | Establish "normal" after a good deploy | Drift row → **Baseline** | `exa drift baseline <M>` / `exa drift input baseline <M>` |
| Reset drift **(admin)** | Clears drift snapshots for a model | Start drift tracking over | Drift row → **Reset** | `exa drift reset <M>` |
| Enable/disable auto-retrain **(admin)** | Configures closed-loop retrain (prompts for dataset) | Auto-remediate drift | Drift → Auto-retrain tab → toggle | `exa drift auto-retrain enable <M> --dataset <D>` / `disable` |

## Alerts  <!-- (role: viewer) -->

Unified alert inbox derived from platform signals — prediction drift (≥2σ/≥3σ), budget
overspend, and eval regressions — sorted most-severe first.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View alert inbox | Lists active alerts with severity + a headline summary | Triage what's on fire | Sidebar → Alerts | `exa drift status` / `exa finops budget status` |
| Acknowledge an alert | Records the ack (audited) and publishes it live to other dashboards | Signal you're handling an alert | Alerts → alert → **Ack** | — |

## Autopilot  <!-- (role: viewer / admin) -->

The self-driving retrain/promote loop's kill-switch and recent-run history. Disabled by
default.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View autopilot runs | Recent autopilot cycle history + current on/off state | See what the loop has done | Sidebar → Autopilot | `exa autopilot status` |
| Enable/disable autopilot **(admin)** | Flips the kill-switch (also honored via `EXAMLOPS_AUTOPILOT_ENABLED`) | Turn closed-loop self-driving on/off | Autopilot → **Enable / Disable** | `exa autopilot enable` / `exa autopilot disable` |
| (run a cycle) | Not a dashboard action — cycles run from CLI/schedule | Trigger one detect→retrain→promote cycle | — | `exa autopilot run [--dry-run] [--model M]` |

## SLOs  <!-- (role: viewer / admin) -->

Model-quality SLO specs + live error-budget status.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View SLOs | Lists SLO specs + error-budget status | Track quality targets | Sidebar → SLOs | `exa` (SLO surface) |
| Define an SLO **(admin)** | Creates an SLO (model/name/target, optional gate-promotion) | Set a quality bar that gates promotion | SLOs → **Define an SLO** → **Set SLO** | — |

## Admission  <!-- (role: viewer / admin) -->

Per-tenant fair-share admission-control queue viewer + enqueue.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View admission queue | Queue depth + per-tenant fair-share stats | See who's waiting for compute | Sidebar → Admission | `exa admission` |
| Submit work **(admin)** | Enqueues a job (kind/tenant/priority/payload) | Manually enqueue against the queue | Admission → **Submit work** → **Submit** | `exa admission` |

## Facility Console  <!-- (role: viewer) --> (flag: facilityConsole)

HPC operations view over the scheduler abstraction (mock/Slurm/Flux): KPI cards (nodes,
GPUs, running, queue depth), per-partition pressure with a Backlog pill, a cluster
switcher, and the waiting-job queue (longest-wait first). Read-only, degrades to zeros.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View fleet overview | Nodes/GPUs allocated, jobs running, queue depth, partitions | Gauge HPC load at a glance | Sidebar → Facility | `exa hpc capacity` / `exa hpc nodes` |
| Switch cluster | Rescopes all metrics to one scheduler/cluster (or All) | Focus on one cluster | Facility → **Cluster** dropdown | `exa hpc capacity --cluster <n>` |
| Inspect the queue | Waiting jobs with wait time, model, GPU ask | Find the longest-waiting job | Facility → Queue | `exa hpc queue` / `exa hpc jobs` |
| Approve/reject cluster **(admin)** | Governance gate on a fleet cluster (Facility Fleet panel) | Authorize a discovered cluster | Facility → Fleet panel → Approve/Reject | `exa hpc approve <n>` / `exa hpc reject <n>` |

## FinOps & Green-AI  <!-- (role: viewer) -->

Cost + carbon accounting: KPI tiles (spend, GPU-hours, estimated carbon ±band,
cost-per-run), cost-by-model table, per-project budget usage with an "Over budget" pill,
and an honest carbon-methodology footnote. Read-only.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Review spend & carbon | Total spend, GPU-hours, estimated CO₂e (±30%), unit economics | Report cost/carbon; find expensive models | Sidebar → FinOps | `exa finops` / `exa models cost <M>` |
| Check budgets | Per-project budget-vs-actual with over-budget flag | Catch overspend | FinOps → Budgets | `exa finops budget status` |
| (set a budget) | Not a dashboard action — set from CLI | Define a project budget | — | `exa finops budget set <proj> --gpu-hours N --cost N` |

## Self-Obs (Status)  <!-- (role: viewer) -->

The dashboard observing itself: KPI tiles (request count, server errors, rate-limit hits,
p95 latency) + dependency health pills (`platform_db` probed live), auto-refreshing every
15s. No third-party telemetry egress.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View dashboard health | Self-metrics + dependency up/degraded/down | Debug the dashboard itself | Sidebar → Self-Obs (`/operate/self-obs`) | `exa status` |

---

## Compliance (Governance)  <!-- (role: admin) -->

Compliance posture — NIST AI RMF control coverage (satisfied/partial/gap, never false
green), EU AI Act status per model, model-card coverage with missing models named, and an
audit-trail integrity hash-chain ("Chain verified" badge + digest). Read-only, honest
about gaps.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Review compliance posture | Graded NIST controls + EU AI Act per model | Prepare for an audit; find gaps | Sidebar → Compliance | `exa audit` (evidence source) |
| Check card coverage | Fraction of models with a model card + which are missing | Close documentation gaps before promotion | Compliance → Model-card coverage | — |
| Verify audit integrity | Shows the tamper-evidence hash-chain head digest | Prove the audit log wasn't altered | Compliance → Audit integrity | `exa audit verify-worm` (related) |

## Audit  <!-- (role: admin) -->

Chronological history with two tabs — **Platform Ops** (audit events from across the
platform) and **Config Changes** (dashboard config/secret writes; values never recorded).

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View platform audit | Chronological audit events (source, actor, action) | Investigate who did what | Sidebar → Audit → Platform Ops | `exa audit --last 7d --model <M>` |
| View config changes | History of dashboard config/secret writes | Trace a config change | Audit → Config Changes | — |
| Filter by time window | Scope to All / 30 / 90 / 365 days | Narrow an investigation | Audit → time-window select | `exa audit --last 30d` |

## Approvals  <!-- (role: admin) -->

The sysadmin approval gate: pending model-change requests from CI (model, commit,
message, changed files, requested time, status). Approving fires Prefect training; rejecting
records a reason. Rendered as a filterable/faceted data grid.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Review pending approvals | Lists retrain requests awaiting sign-off | Gate model changes before they train | Sidebar → Approvals | `exa approvals list` |
| Approve a change **(admin)** | Approves the request → fires the model's Prefect training | Green-light a validated change | Approvals → row → **Approve** | `exa approvals approve <M>` |
| Reject a change **(admin)** | Rejects with an optional reason (prompted) | Block a bad change | Approvals → row → **Reject** → reason | `exa approvals reject <M> --reason "…"` |
| Show all statuses | Toggle to include approved/rejected history | Audit past decisions | Approvals → **Show all statuses** | `exa approvals list` |

## Fairness  <!-- (role: admin) -->

Per-model fairness slicing attributes + disparity thresholds.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View fairness config | Lists per-model slice attributes + thresholds | See how fairness is measured | Sidebar → Fairness | — |
| Configure fairness **(admin)** | Sets model/threshold/slice attributes, optional gate-promotion | Enforce subgroup fairness before promotion | Fairness → **Configure fairness** → **Save config** | — |

## Secrets  <!-- (role: admin) -->

Write-only platform secrets store — set/rotate secrets that are never readable back;
metadata (path, updated) only.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View secret metadata | Lists secret paths + last-updated (never values) | Confirm a secret exists | Sidebar → Secrets | `exa secrets` |
| Set / rotate a secret **(admin)** | Writes a secret at a path (value never returned) | Store a credential; rotate a key | Secrets → **Set a secret** → **Save secret** | `exa secrets rewrap` (rotate) |

---

## Projects  <!-- (role: viewer / admin) --> (flag: projectsConsole)

The unified project/workspace list — per-project quota, resources, members, status. Click
in for the full anatomy.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View project list | Lists projects with quota/resources/members/status | See all workspaces | Sidebar → Projects | `exa project` (list) |
| Open a project | Navigate to Project Detail | Manage one workspace | Click a project card | `exa project show <name>` |
| New Project **(admin)** | Create a project with name/description/CPU/memory/storage/GPU quota | Stand up a new workspace | Projects → **New Project** → **Create** | `exa project create <name> --cpu-limit … --memory-gb … --storage-gb …` |

## Project Detail  <!-- (role: viewer / admin) -->

Full project anatomy at `/platform/projects/:name`: quota, budget-vs-consumption, storage,
pipelines (Prefect + Ray Serve), resources by kind, members, connections, workbenches,
authored providers, and a danger zone. Every write modal shows the equivalent `exa`
command inline. Viewers read; admins write.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View anatomy | Quota, budget/consumption, storage, pipelines, resources, members | Understand a project end-to-end | Sidebar → Projects → a project | `exa project show <name>` |
| Edit project **(admin)** | Update description, CPU/memory/storage/GPU quota, namespace, budgets | Right-size a workspace | Quota/Budget card → **Edit** | `exa project quota <name> --cpu … --memory … --storage … --gpu …` |
| Assign resource **(admin)** | Attach a model/pipeline/endpoint/connection/dataset/storage to the project | Group resources under a project | Resources → **Assign resource** | `exa project assign <name> --kind <k> --ref <ref>` |
| Add member **(admin)** | Grant a subject owner/editor/viewer | Give a teammate access | Members → **Add member** | `exa project add-member <name> --subject … --role …` |
| Remove member **(admin)** | Revoke a member | Off-board a teammate | Members → member row → **Remove** | — |
| Provision / bind storage **(admin)** | Ensure per-project MinIO layout; optionally bind a connection | Give a project object storage | Storage → **Provision storage** / **Bind connection** | `exa project storage <name> [--connection <c>]` |
| New connection **(admin)** | Create an s3/uri/dataplane connection; optional secret stored encrypted (never returned) | Wire a project to external data | Connections → **New connection** | `exa connection create <name> --kind … --project <p> [--secret-value …]` |
| Test connection **(admin)** | Reachability probe of a connection | Verify credentials/endpoint | Connections → row → **Test** | — |
| Delete connection **(admin)** | Removes a connection | Clean up | Connections → row → **Delete** | — |
| New workbench **(admin)** | Create a project-bound notebook with its own volume | Give a project an interactive env | Workbenches → **New workbench** | `exa workbench create <name> --project <p> [--image …]` |
| Start/stop workbench **(admin)** | Flip a workbench RUNNING/STOPPED (start injects the project's connections as env vars) | Spin a notebook up/down | Workbenches → **Start** / **Stop** | `exa workbench` |
| Open workbench | Opens the running notebook URL | Do interactive work | Workbenches → **Open** | — |
| Delete workbench **(admin)** | Removes the workbench (volume kept) | Clean up | Workbenches → trash icon | — |
| Manage providers **(admin/cap)** | Create/activate/delete the Python behind the project's calculations (capability-gated) | Swap a FinOps/drift/promotion formula | Providers card → **New provider** / Activate / Delete | `exa providers list` |
| Delete project **(admin)** | Removes the project grouping (underlying models/connections/storage kept) | Retire a workspace | Danger zone → **Delete project** (confirm) | — |

---

## Events  <!-- (role: viewer / admin) -->

The transactional-outbox event backbone — backlog view + publish.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View event backlog | Outbox depth + backlog stats | Monitor the event bus | Sidebar → Events | `exa events` |
| Publish event **(admin)** | Publishes a topic + JSON payload | Inject/replay an event | Events → **Publish event** → **Publish** | `exa events relay` (drain) |

## Services  <!-- (role: viewer / admin) -->

Docker stack control: live status badges for external services (with deep links) and
system containers; admins can start/stop/restart and tail logs (via the Docker socket).

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View service status | Per-container status + external-UI links | See what's up in the stack | Sidebar → Services | `exa status` |
| Start/stop/restart **(admin)** | Container lifecycle via the Docker socket (dashboard self-restart auto-reconnects) | Recover or cycle a service | Service card → Start/Stop/Restart | `make stack-restart` (stack-level) |
| Tail logs **(admin)** | Live-streams a container's logs | Debug a failing service | Service card → log view | `make stack-logs` |
| Open external UI | Deep-link to MLflow/Grafana/Ray/etc. | Jump into a tool | External-service card link | — |

## Providers  <!-- (role: viewer / capability) -->

Authored calculation-providers console (FinOps/drift/promotion formulas) — per project.
Writes are capability-gated (`PROVIDERS_MANAGE`), not raw admin.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| List providers | Shows registered providers per domain/project | See which formula each calc uses | Sidebar → Providers | `exa providers list [--domain …]` |
| New provider **(cap)** | Author a new calculation provider | Swap a formula without core code | Providers → **New provider** | `exa providers list` |
| Activate provider **(cap)** | Makes a provider the active one | Switch to a new coefficient set | Provider row → **Activate** | — |
| Delete provider **(cap)** | Removes a provider (confirm) | Clean up | Provider row → **Delete** | — |

## Config  <!-- (role: viewer / admin) -->

Service endpoints, credentials, thresholds, GitLab ModelZoo integration, ModelZoo webhook
+ auto-retrain toggle, SeanerBUS bridge, Slurm adapter, and the current session API token.
Collapsible sections. Viewers see everything read-only (secrets masked); admins edit.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View config | All service URLs + which secrets are set (values masked) | Confirm platform wiring | Sidebar → Config | `exa config` / `exa env` |
| Edit endpoints/params **(admin)** | Change service URLs, thresholds, Slurm, SeanerBUS values | Point the dashboard at new endpoints | Config → edit fields → **Save** | `exa config set <key> <value>` |
| Set/rotate credentials **(admin)** | Type a new secret (encrypted at rest; blank = unchanged; × = clear) | Store MinIO/Grafana/GitLab/control-plane secrets | Config → Credentials → type → **Save** | `exa config set <key> <value>` |
| Configure GitLab ModelZoo **(admin)** | Set GitLab URL, project ID, access token | Enable model/dataset auto-discovery | Config → GitLab ModelZoo → **Save** | `exa config set gitlab …` |
| ModelZoo webhook + auto-retrain **(admin)** | Copy the webhook URL; toggle auto-retrain on push to main | Wire CI-driven retraining | Config → ModelZoo Integration → **Save webhook config** | — |
| Reload Ray Serve **(admin)** | Runtime reload without a config save | Apply a promotion immediately | Config → Quick Actions → **Reload Ray Serve** | `exa serve reload` |
| Import .env **(admin)** | Upload a `.env`/`.env.dashboard` to populate config | Bulk-load config | Config → **Import .env** | — |
| Apply Config (export) **(admin)** | Download `.env.dashboard` to place in repo + restart | Persist dashboard config to disk | Config → **Apply Config** | — |
| Copy API token | Copies the current session bearer token | Make direct API calls | Config → API Token → copy | — |

## SeanerBUS  <!-- (role: viewer) -->

Status dashboard for the SeanerBUS→Ray-Serve bridge: bridge stats, per-model handler
UUIDs, live Grafana metrics, config summary, and a log tail. Config edits are deferred to
the Config page (admin).

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View bridge status | Bridge health/stats + per-model UUIDs + live metrics | Confirm HPC-job inference is flowing | Sidebar → SeanerBUS | — |
| Copy a UUID | Copies a model's handler UUID | Register a topic handler | UUID row → copy | — |
| Edit in Config | Deep-links to the SeanerBUS section of Config (admin edits) | Change bridge connection settings | SeanerBUS → **Edit in Config** | — |
| (assign missing UUIDs) | Shown when a model UUID is unassigned | Backfill per-model UUIDs | (hint on page) | `exa seanerbus init-uuids` |

## Jupyter  <!-- (role: viewer) -->

JupyterHub launcher/status page.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Launch JupyterHub | Opens the JupyterHub UI (port 18888) | Do notebook work | Sidebar → Jupyter → **Launch JupyterHub** | `make jupyter-up` (start), `make jupyter-add-user USER=…` |

## Flags  <!-- (role: admin) -->

Server-side feature-flag management. Admins see every flag with default/override/effective
state + targeting and can toggle; changes are audited and pushed live.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| View flags | Lists flags with default/override/effective/targeting | See what's rolled out to whom | Sidebar → Flags | — |
| Toggle a flag **(admin)** | Sets an override (persisted, audited, published on the realtime channel) | Enable/kill a feature live | Flags → flag → toggle | — |

---

## Cross-cutting tools (available on every page)

### Command Palette & Search  <!-- (role: viewer / admin) -->

Keyboard-first entry point (⌘K / Ctrl-K): fuzzy-navigate to any page, run a role-scoped
action (which copies the equivalent `exa` command for GUI↔CLI parity), or federated-search
models, HPC jobs, audit events, and pages.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Open palette | Toggle the palette | Jump anywhere fast | ⌘K / Ctrl-K | — |
| Navigate | Fuzzy-jump to any page | Skip the sidebar | Type a page name → Enter | — |
| Copy an action's CLI | Copies the `exa` command for a matched action (viewer never sees admin-only entries) | Learn/script the CLI equivalent | Select an Action row → Enter | (varies by action) |
| Federated search | Search models/jobs/audit/pages | Find an entity by name | Type a query → pick a result | — |

### Copilot  <!-- (role: viewer / admin) -->

Context-aware assistant in the shell (bottom-right). Answers grounded questions and
**suggests** `exa` commands as copy-only cards — it never executes anything. Thin proxy to
the existing Skipper agent (no paid API).

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Ask a question | Grounded Q&A about the current page/entity | "Why is jpcp drifting?" / "How do I promote?" | Copilot launcher → type | `exa ask "<question>"` |
| Copy a proposed command | Read-only or "Needs approval" `exa` command as a copy-only card (never a run button) | Get the exact command, then run it through the normal gated flow | Copilot → copy the command card | (the proposed command) |

### Personalization & Watchlist (Preferences)  <!-- (role: viewer / admin) -->

Preference center (`/preferences`) + a watchlist of pinned entities + a first-run tour +
the help drawer/glossary. Persists to this browser (localStorage).

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Set default landing page | Choose where `/` takes you | Land on the console you use most | Preferences → Default landing | — |
| Set density / language | UI density + EN/IT locale | Personal comfort | Preferences → Density / Language | — |
| Pin to watchlist | Star a model/job; it collects on Preferences | Follow the entities you care about | ⭐ star button on an entity | — |
| Replay onboarding tour | Re-runs the getting-started tour | Re-learn the shell | Preferences → **Replay the getting-started tour** | — |
| Open help / glossary | Searchable glossary of platform terms | Look up "drift", "canary", etc. | Press `?` anywhere | — |

### Collaboration  <!-- (role: viewer / admin) -->

Comment on entities with @-mentions, follow an activity trail, and share a frozen snapshot
link. Tenant-scoped, sanitized, audited. Currently surfaced on Model Detail.

| Action | What it does | Use case | How to (UI) | Equivalent CLI |
|---|---|---|---|---|
| Comment / @-mention | Post a comment on an entity; mention notifies teammates over the realtime channel | Discuss a model in context | Model Detail → Discussion → type `@name` | — |
| View activity trail | Merges comments + audit events into one chronological story | Reconstruct what happened around a model | Model Detail → Discussion | `exa audit --model <M>` |
| Share snapshot | Freezes the current view (path+filters) behind a read-only, expiring, tenant-scoped link | Share exactly what you see | Model Detail → **Share snapshot** | — |

### NOC / Wall kiosk  <!-- (role: viewer) -->

Full-screen auto-rotating kiosk (`/noc`) of big-font FinOps/alert slides for a wall
display. Non-interactive except **Exit** back to `/`.

---

## Notes

- **CLI parity is the design intent.** Dashboard writes call the same `examlops.*` /
  `platform_db` code as `exa`, so a CLI equivalent exists for most mutations; where a table
  cell above says `—`, there is genuinely no direct one-liner (e.g. UI-only conveniences
  like "Sync from GitLab", "Copy token", snapshots).
- **Every mutation is audited** as a `dashboard*` event and readable on the Audit /
  Compliance pages (and via `exa audit`).
- **Role enforcement is server-side.** Hiding an admin button in the UI is backed by a BFF
  403 with a `deny_reason`; a viewer cannot bypass it by calling the API directly.
- **Feature-flagged consoles** (MLOps, Facility, Projects) only appear when their flag is
  enabled on the Flags page.
