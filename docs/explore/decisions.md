---
title: Who decides
description: Every place a person decides in ExaMLOps — approvals, promotions, traffic, the autopilot switch, cluster admission and agent writes — and the audit trail that records them.
hide:
  - navigation
  - toc
---

# Who decides

ExaMLOps automates the loop from drift to retraining, but some decisions stay with people.
Each one passes through a gate and releases one part of the system, and the decision is
recorded in the hash-chained audit trail, whichever surface made it. This tour walks through them; the catalogue below lists every
human action the platform has.

<div class="xm-player" data-scene="decisions" markdown>
<ol class="xm-steps">
<li data-focus="dev,g-approve" data-run="dev-approve" data-actor="Developer" data-line="human"><strong>A developer changes a model.</strong> CI works out which models the commit touched and files one pending approval per model with the control plane. Nothing trains yet.</li>
<li data-focus="op,g-approve" data-run="op-approve" data-actor="Operator" data-line="human"><strong>An operator approves or rejects it.</strong> From the CLI (<code>exa approvals approve JPCP</code>) or the dashboard's Approvals page. Either can carry a reason; the dashboard asks for one on rejection.</li>
<li data-focus="g-approve,cp" data-run="approve-cp" data-actor="Control plane" data-line="control"><strong>Approval starts the training flow.</strong> The control plane dispatches the training deployment for that model. Approvals nobody answers expire after 72 hours, so a stale change cannot be approved weeks later.</li>
<li data-focus="op,g-promote,mlflow" data-run="op-promote;promote-mlflow" data-actor="Operator" data-line="human"><strong>Promotion waits for a metric.</strong> <code>exa pipeline promote jpcp --if-rmse-lt 5.0</code> moves the alias only if the condition holds. Evaluation and judge-calibration gates can block it too; <code>--force</code> overrides a failing gate and is recorded.</li>
<li data-focus="op,g-traffic,ray" data-run="op-traffic;traffic-ray" data-actor="Operator" data-line="human"><strong>Traffic moves in steps.</strong> <code>exa serve traffic jpcp --production 90 --canary 10</code> sets the weights the model router reads, so a new version takes a slice of traffic before it takes all of it.</li>
<li data-focus="op,g-auto,auto" data-run="op-auto;auto-auto" data-actor="Operator" data-line="human"><strong>The autopilot acts only when switched on.</strong> Its kill-switch is off by default. <code>exa autopilot enable</code> lets it act and <code>exa autopilot disable</code> stops it; each <code>exa autopilot run</code> — from your own scheduler — runs one drift → retrain → promote cycle.</li>
<li data-focus="auto,audit" data-run="auto-notice" data-actor="Autopilot" data-line="control"><strong>Autonomy levels decide whether it acts or asks.</strong> Each behaviour is AUTONOMOUS, REVIEW or DISABLED. Under REVIEW, or when a policy rule says <code>require_approval</code>, the autopilot does not act: it writes a <code>human_approval_required</code> event to the audit trail and an operator follows up with the normal commands.</li>
<li data-focus="sysadmin,g-cluster,fleet" data-run="sysadmin-cluster;cluster-fleet" data-actor="Sysadmin" data-line="human"><strong>A cluster is approved before any job lands on it.</strong> <code>exa hpc connect</code> registers a cluster as PENDING. Placement refuses it until a sysadmin runs <code>exa hpc approve</code>.</li>
<li data-focus="chat,g-confirm,skipper" data-run="chat-confirm;confirm-skipper" data-actor="Skipper user" data-line="human"><strong>Agents ask before they write.</strong> Skipper pauses every mutating tool and asks; through its web UI and <code>exa ask</code>, the approval is single-use and expires after 10 minutes by default. The MCP server offers write tools only when started with <code>--allow-writes</code>, and the dashboard copilot runs a read-only agent that only proposes commands.</li>
<li data-focus="cp,mlflow,ray,fleet,skipper,audit" data-run="cp-audit,mlflow-audit,ray-audit,fleet-audit,skipper-audit" data-actor="Audit trail" data-line="observe"><strong>Decisions are recorded.</strong> Commands such as <code>exa approvals</code>, <code>exa pipeline promote</code>, <code>exa hpc approve</code> and the autopilot write to the hash-chained audit trail: who, what and why, each event carrying the hash of the one before it, so an edited record breaks the chain. The control plane records its own gate decisions in the same chain, in the same transaction as the decision: an approval request, an approval, a rejection, a retraction and every retrain it dispatches, whoever called it — the CLI, the dashboard or Skipper — each with the calling principal. Approvals and rejections also go out as outbox events. The principal that filed a change cannot approve it (the shared legacy token is the one exception, because every holder is the same principal). <code>exa audit verify</code> walks the chain; with an external anchor configured, <code>exa audit verify-worm</code> checks it against that too.</li>
</ol>
</div>

!!! note "What is not wired yet"
    The approval queue guards **model changes from CI**. Retrains started by drift — through
    `exa drift trigger`, the bus bridge's error-rate tracker or the autopilot — go to the control
    plane directly, governed by cooldowns, the control plane's rate limit, the corruption veto
    and, for the autopilot, the autonomy levels above — not by the queue. Routing them through the queue is on the [roadmap](roadmap.md).

## The roles

| Role | Who it is | What it can do |
|---|---|---|
| Dashboard viewer | Signed in with the viewer password | Read everything, acknowledge alerts, ask the copilot |
| Dashboard admin | Signed in with the admin password | Every governed action: approve, promote, trigger retrains, change traffic, manage secrets, connections and projects |
| CLI operator | Whoever runs `exa`; recorded as `EXAMLOPS_ACTOR` or the login user | Anything the configured credentials allow; the control plane checks its token's read/write scope |
| Project owner, editor, viewer | Relations on a project, checked when multi-tenancy is on | Owner ⊇ editor ⊇ viewer; relations on a project apply to everything inside it |
| Sysadmin | By convention, the operator responsible for the HPC fleet (the dashboard requires the admin role) | Registers, approves and rejects clusters |
| Skipper user | Anyone chatting with the agent | Approves or denies each write the agent proposes |

## Every human action

### Data

| Action | How | Gate |
|---|---|---|
| Record a dataset revision | `exa data snapshot FData --backend minio --path ./data/FData` | Admin |
| Prune old telemetry | `exa data retention-prune --days 90 --dry-run` | None in the CLI; preview with `--dry-run` |
| Keep a synthetic dataset the quality gate rejected | `exa data synth generate … --force` | Override, recorded |
| Feed back delayed ground-truth labels | `exa eval feedback ingest …` | Admin |
| Restore platform state from a backup | `exa backup restore <path>` | Confirmation |

### Training and retraining

| Action | How | Gate |
|---|---|---|
| Approve a pending model change | `exa approvals approve JPCP` · dashboard Approvals | Approval queue |
| Reject a pending model change | `exa approvals reject JPCP --reason "…"` · dashboard | Approval queue (reason optional in the CLI) |
| Retrain a model now | `exa retrain JPCP --dataset PM100Dataset` (`--dry-run` to preview) · dashboard Pipelines | Confirmation, policy |
| Set an evaluation regression gate | `exa eval gate set JPCP --suite … --metric … --mode block` | Admin |
| Record an LLM-judge calibration | `exa eval calibrate <judge> --from <file>` | Uncalibrated judges cannot gate |

### Promotion and serving

| Action | How | Gate |
|---|---|---|
| Promote when a metric passes | `exa pipeline promote jpcp --if-rmse-lt 5.0` | Metric, evaluation and calibration gates |
| Override a failing promotion gate | `exa pipeline promote … --force` | Override, recorded |
| Set or remove a version alias | Dashboard Models page | Admin |
| Change the traffic split | `exa serve traffic jpcp --production 90 --canary 10` | Confirmation |
| Start an A/B test or shadow traffic | Dashboard Traffic page · `exa serve ab …`, `exa serve shadow …` | Admin |
| Propose promoting a challenger | `exa serve challenger promote JPCP` (the alias then moves with `exa pipeline promote`) | Significance policy, no SLO regression |
| Roll an alias back | `exa models rollback run JPCP --dry-run` | Confirmation |
| Move or roll back a prompt label | `exa prompt label triage prod 3` · `exa prompt rollback …` | Evaluation gate on `prod` label moves by default (`--force` overrides, recorded); rollback is not gated |

### Drift and the autopilot

| Action | How | Gate |
|---|---|---|
| Set a drift baseline | `exa drift baseline JPCP` · `exa drift input baseline JPCP` | Confirmation |
| Clear drift snapshots | `exa drift reset JPCP` | Confirmation |
| Turn drift-triggered retraining on or off | `exa drift auto-retrain enable JPCP --dataset PM100Dataset` | Admin |
| Fire retrains for drifted models | `exa drift trigger` (`--dry-run` to preview) | Cooldown, corruption veto |
| Switch the autopilot on or off | `exa autopilot enable` · `exa autopilot disable` | Kill-switch |
| Set a behaviour's autonomy level | `exa autopilot autonomy drift_auto_retrain REVIEW` (granting AUTONOMOUS needs `--ack`) | Autonomy level |
| Freeze, kill or resume an autopilot run | `exa autopilot interrupt <run-id> --freeze` (or `--kill`) · `exa autopilot resume <run-id>` | Kill-switch |
| Keep one model away from autonomous action | `exa autopilot quarantine JPCP --reason "…"` | Kill-switch |
| Acknowledge an alert | Dashboard Alerts page | Any signed-in user |

### HPC fleet

| Action | How | Gate |
|---|---|---|
| Register a cluster | `exa hpc connect <login-host> --name cluster-a --user me --key ~/.ssh/id_ed25519` | Starts PENDING |
| Approve a cluster | `exa hpc approve cluster-a` · dashboard Facility | Cluster admission |
| Reject a cluster | `exa hpc reject cluster-a --reason "…"` | Cluster admission |

### Governance and security

| Action | How | Gate |
|---|---|---|
| Write policy rules (allow, deny, require approval) | `policy.yaml` in the config directory · `exa policy …` | Policy |
| Sign a tenant's policy bundle | `exa policy bundle sign --tenant acme` | Admin |
| Classify a system's EU AI Act risk tier | `exa compliance classify JPCP --risk-tier … --purpose …` | Promotion blocked until classified |
| Record a sampled audit review | `exa audit review --sample 20 --notes "…"` | Recorded review |
| Review every autonomous action | `exa audit autonomy --last 30d` | Admin |
| Set, rotate or re-encrypt secrets | `exa secrets set …` · `exa secrets rotate …` · `exa secrets rewrap --dry-run` | None in the CLI; `rewrap --dry-run` previews |
| Sign or verify a model artifact | `exa models sign jpcp 18 --path …` · `exa models verify …` | Verify before load |
| Issue or revoke a gateway key | `exa gateway key issue …` · `exa gateway key revoke …` | Admin |
| Create or delete a named connection | `exa connection create …` · dashboard Connections | Admin; delete asks for confirmation |

### Projects and access

| Action | How | Gate |
|---|---|---|
| Create, archive or delete a project | `exa project create research …` · `exa project archive research` | Confirmation to delete |
| Add or remove a member | `exa project add-member research alice --role editor` | Admin |
| Grant or revoke a relation | `exa project grant alice editor project:research` | Admin |
| Assign a resource to a project | `exa project assign research JPCP --kind model` | Admin |
| Set a project's quota or budget | `exa project set-quota …` · `exa finops budget set research --gpu-hours 500` | Admin |

### Agents and MCP

| Action | How | Gate |
|---|---|---|
| Approve or deny a write Skipper proposes | In the Skipper chat | Single-use, expiring confirmation |
| Let Skipper remember a procedure | In the chat, then `exa agent memory review approve <id>` | Review queue |
| Erase agent memories | `exa agent memory delete …` | Confirmation |
| Allow MCP clients to write | `exa mcp serve --allow-writes` | Off by default |
| Run a command the copilot proposed | Dashboard copilot panel | The copilot only proposes |

## Read more

- [Audit trail](../guides/audit-trail.md) and [evidence chain](../guides/evidence-chain.md)
- [Policy-as-code](../guides/policy-as-code.md)
- [Dashboard authentication](../dashboard/auth.md) and [multi-tenancy](../guides/rbac-multi-tenancy.md)
- [EU AI Act mapping](../guides/eu-ai-act-compliance.md)
