# Runbooks: control plane

Alerts from the `examlops-control-plane` group about the control plane itself, its asynchronous
commands, the approval gate and its Prefect dispatch. The event-backbone alerts in the same group
have [their own page](events.md). Background: [Control plane](../guides/control-plane.md).

Most checks start at the control plane's diagnostic endpoint:

```bash
curl -s localhost:18002/health | python3 -m json.tool
```

| Field | Tells you |
|---|---|
| `status`, `ready` | Overall verdict; `ready` is what `/readyz` answers |
| `dispatch.state` | Whether the Prefect deployment retrains go to exists: `ok`, `missing`, `incompatible`, `unreachable` |
| `circuit_breaker.state` | The Prefect gateway breaker: `closed`, `open`, `half-open` |
| `startup_checks` | Datastore, registry and Prefect checks; a failed one is re-run by the probe |
| `runtime.outbox`, `runtime.event_relay_error` | Event backlog and the relay's last error |
| `runtime.serving_snapshot` | The snapshot projector's generation and last error |

## ControlPlaneDown {#controlplanedown}

**Meaning:** Prometheus has not reached the control plane for 2 minutes.

**Impact:** no retrain can be submitted or dispatched, and no approval decided. Serving is
unaffected (static stability), but nothing reaches it until the control plane is back.

**Check:** `exa stack status` (is the container running or restarting?), then
`exa stack logs --service control-plane --tail 200`. `curl -s localhost:18002/livez` answers
while the process can respond; `/readyz` says whether it takes traffic.

**Fix:**

- **Crash loop at startup.** The logs name the check. The usual causes: an unusable
  `CONTROL_PLANE_TOKEN` (placeholder values are refused), or the datastore unreachable
  (`CONTROL_PLANE_DB` or the Postgres DSN).
- **Up but unscraped.** The Prometheus target or the network zone is wrong. With the segmented
  overlay, the control plane must be on the `ops` network.
- **The datastore is down** (`/readyz` answers 503 in about 2 s, with `datastore unavailable at
  <host>:<port>` in the body, and retrain submissions answer 503). The control plane is doing the
  right thing: it takes itself out of rotation and refuses writes cleanly rather than hanging. Fix
  the datastore; the control plane becomes ready again within a moment of Postgres accepting
  connections, with no restart. Do not restart the replicas — that loses nothing, but it does not
  help either, and `/livez` deliberately keeps answering so the orchestrator does not.
  [What is bounded, and the measured timings](../guides/postgres-backend.md#when-the-datastore-goes-away).

## DriftEvaluationFailing {#driftevaluationfailing}

**Meaning:** the control plane's drift evaluator, which scores every model's prediction drift
every `CONTROL_PLANE_DRIFT_EVAL_SECONDS` (60), failed more than three times in 10 minutes.

**Impact:** no `drift.status_changed` or `alert.drift` event is sent, so nothing that reacts to
drift through the event backbone hears of it. `exa drift status` still works, because it scores on
demand.

**Check:** `exa stack logs --service control-plane --tail 300`, the lines `Drift evaluation
failed: …`. The usual causes:

- the platform store is unreadable;
- a custom drift provider raises (`exa providers list --domain drift`).

**Fix:** restore the store, or fix or unset the provider. The next evaluation announces any change
it missed, because the change is measured against the last status recorded, not the last evaluation.

## AuditMaintenanceFailing {#auditmaintenancefailing}

**Meaning:** the scheduled audit maintenance (ADR 0028), which the control plane runs every
`EXAMLOPS_AUDIT_MAINTENANCE_SECONDS` (3600), failed more than one step in three hours. A step is
signing a checkpoint over the audit chain head, anchoring it to the WORM store, logging it to the
transparency log, or the opt-in retention prune.

**Impact:** the audit trail is still hash-chained and append-only, but its newest state is not
anchored off-platform, so a rewrite of the whole database would not be detected against the anchor.

**Check:** `exa audit maintenance-runs` lists each cycle and the step that failed;
`exa --json audit maintain --dry-run` says what the next cycle would do. The usual causes:

- no checkpoint signing key (`unconfigured`): set `EXAMLOPS_SIGNING_KEY` or the secret
  `model-signing/key`;
- the WORM store is unreachable, so the entry degraded to the local fallback file
  (`exa audit verify-worm` warns about it);
- the transparency log refused the upload or is unreachable (`exa audit verify-transparency`);
- a scheduled prune was refused (`refused`): the reason names the precondition.

**Fix:** restore the failing target. The next cycle signs, anchors and logs the current head,
which covers every event before it.

## FeatureViewStale {#featureviewstale}

**Meaning:** a feature view's online store was last materialized longer ago than its TTL, or it
has never been materialized although it declares a TTL or a schedule, for 30 minutes
(`examlops_feature_view_stale{view=…} == 1`, published from the registry on every scrape).

**Impact:** a request that names its entity (for example `job_id`) instead of carrying its
features is served the last materialized value, which is now older than the view promises — or is
refused with `validation_error` when nothing was materialized. Requests that carry their features
are unaffected. See [Feature store](../guides/feature-store.md).

**Check:**

1. `exa feature status` — age, TTL, schedule interval and whether the view is due.
2. `exa stack logs --service control-plane --tail 300 | grep -i "feature"` — the materializer logs
   each run and each failure (`Feature view … failed to materialize: …`).
3. `CONTROL_PLANE_FEATURE_MATERIALIZE_SECONDS` is not `0`, and the view has
   `materialize_interval_seconds` > 0 (a view with a TTL but no schedule is only materialized by
   hand).

**Fix:** `exa feature materialize-due --view <view>` (or `exa feature materialize <view>`), then
repair whatever made the scheduled run fail. The next scrape clears the alert.

## FeatureFreshnessUnreadable {#featurefreshnessunreadable}

**Meaning:** `/metrics` could not read feature-view freshness from the registry for 10 minutes
(`examlops_feature_freshness_read_errors_total` is increasing).

**Impact:** the freshness gauges keep their last values, and a control plane started during the
failure publishes none, so `FeatureViewStale` cannot be trusted while this fires. Serving is not
affected by the read itself.

**Check:** `exa stack logs --service control-plane --tail 300 | grep "feature-freshness read
failed"` names the error; `exa feature status` from the same host shows whether the registry
(`platform.db`, or Postgres when `EXAMLOPS_DB_BACKEND=postgres`) answers.

**Fix:** restore access to the platform datastore. The alert clears 10 minutes after reads succeed.

## ControlPlaneCommandDead {#controlplanecommanddead}

**Meaning:** at least one asynchronous command (a retrain submitted through `POST /v1/retrain`)
used up its attempts (`CONTROL_PLANE_COMMAND_MAX_ATTEMPTS`, 5) in the last 15 minutes.

**Impact:** that retrain will not run. Whoever submitted it was told "accepted"; nothing will
follow unless someone resubmits.

**Check:** `exa commands list --state dead` and `exa commands show <id>` show `last_error`.

**Fix:** by cause:

- **The Prefect deployment is missing** (`dispatch.state` is `missing`): run
  `exa pipeline deploy` on the runner host.
- **Prefect is down:** see [PrefectCircuitBreakerOpen](#prefectcircuitbreakeropen).
- **Unknown parameters:** the flow refused the parameters; the command's `last_error` names them.
- **`abandoned: its dispatcher stopped on every attempt`:** each replica that claimed it stopped
  before finishing: a crash, an OOM kill, a node lost. After a claim lease
  (`CONTROL_PLANE_COMMAND_LEASE_SECONDS`, 60 s) another replica takes a command over; one that
  took down every dispatcher is buried rather than handed to the next. Look for restarts and OOM
  kills of the control-plane pods around the command's attempts.

Then resubmit the retrain (`exa retrain <model> --dataset <dataset>`). A dead command is never
retried automatically.

**Check whether it left a run behind before you resubmit** — and the platform now checks for you,
raising [ControlPlaneDeadCommandLeftARun](#controlplanedeadcommandleftarun) and naming the run in the
command's `last_error`. A command can die because the replica
holding it could reach the datastore but not Prefect — a partition, a firewall change, a hung
connection. That replica keeps the command (it is healthy and renews its claim, so no other replica
takes it over), spends every attempt itself, and the command ends `dead`. Meanwhile a dispatch that
was merely *slow* can arrive after all, and Prefect starts the run anyway. The platform then records
the work as dead while a training job for it is running, with nothing pointing at it.

Measured in `tests/integration/test_control_plane_partition_kind_live.py`: one command, ten dispatch
attempts over 116 s, all from the one replica, ending `dead` — and exactly one flow run in Prefect,
which the command never recorded.

So look in Prefect before resubmitting, by the command's own idempotency key
(`v1:retrain:<hash>`, shown by `exa commands show <id>`):

```bash
exa commands show <id>                 # state, attempts, last_error, and the key it dispatched with
# then, on the runner host, look for a flow run with that idempotency key
```

If a run exists and is still going, let it finish rather than starting a second one — the key means
a resubmission of the *same* command would attach to it, but a fresh `exa retrain` is a new command
with a new key and would train twice.

## RetrainDispatchedButNotRecorded {#retraindispatchedbutnotrecorded}

**Meaning:** a retrain reached Prefect and started, and the write that records it failed. The
training is running; the platform's own row for it says `failed`.

**Impact: low, and it self-heals — which is why this is a warning about the datastore, not about
the retrain.** The command is retried on the next worker cycle, the retry carries the **same**
Prefect idempotency key, and Prefect returns the run it already made rather than creating a second
one. No duplicate training results from the retry. What you are being told is that the datastore
refused a write on the dispatch path.

**How it catches the first one.** `examlops_retrain_requests_total` is labelled by model *and*
dataset, so the first `dispatched_unrecorded` for a model arrives as a **new series already at 1** —
and `increase()` over a series with no earlier sample is `0`. For most deployments that first
occurrence is the only one there will ever be, so the alert carries the platform's idiom for it:
an `or` clause selecting a series that exists now and did not a window ago. The same clause appears
on `InferenceReplicasLost`, `InferenceRetryBudgetSpent` and `RayServeReloadFailures`, whose Ray
counters cannot be pre-created at all — `ray.util.metrics.Counter.inc()` raises on a value of `0`.
Where a counter's labels *are* enumerable at startup, the platform pre-creates them instead
(`initialize_command_outcomes`, and the Art. 12 actions on `examlops_audit_events_dropped_total`).

**Why it has its own outcome rather than counting as an error:** it used to be recorded as
`outcome="error"`, which is a different claim — that the retrain failed. It did not.
[HighRetrainErrorRate](#highretrainerrorrate) pages above a 20% error rate over 15 minutes and
retrains are rare, so a single miscounted success was a 100% error rate and paged the on-call about
a subsystem that was working. The outcome is now `dispatched_unrecorded`, which is what happened.

**Check:**

```bash
exa commands show <id>          # state=failed, last_error names the bookkeeping failure
exa commands list --state failed
```

The control-plane log carries the matching line at warning level, naming the flow run:

```
Command v1:retrain:… dispatched flow run 69b604b8-… but could not be recorded (…);
it will be retried against the same Prefect idempotency key
```

**Fix:**

1. **Confirm it cleared.** The next worker cycle should move the command to `succeeded`. If it did,
   the only remaining question is the datastore.
2. **Look at the datastore.** This fires on a write failure on the dispatch path — check the
   control plane's own `/readyz` and, on Postgres,
   [what is bounded and the measured timings](../guides/postgres-backend.md#when-the-datastore-goes-away).
3. **If it does not clear across every attempt**, the command is buried and you are now in
   [ControlPlaneDeadCommandLeftARun](#controlplanedeadcommandleftarun) — follow that section, which
   is the one where a resubmission can train twice.

**Related:** [ControlPlaneDeadCommandLeftARun](#controlplanedeadcommandleftarun) is what this turns
into if the write never succeeds.

## ControlPlaneDeadCommandLeftARun {#controlplanedeadcommandleftarun}

**Meaning:** the control plane gave up on a command and then found that Prefect holds a flow run for
it — the platform reporting its own inconsistency.

It asks twice over: once when it buries the command, and again from the reconcile sweep on **any**
replica, within `CONTROL_PLANE_ORPHAN_CHECK_WINDOW_SECONDS` (15 minutes). The second one is the one
that usually works: the replica that gives up on a command is typically the one that cannot reach
Prefect, so its own lookup times out too.

**Impact:** a training job is running that nothing points at. Nobody is waiting for it, its result
will be registered by the pipeline as usual, and the command that asked for it says `dead`. If
someone resubmits without looking, the platform trains the same thing twice — the resubmission is a
*new* command with a new idempotency key, so nothing deduplicates it.

**How it happens:** a replica that can reach the datastore but not Prefect keeps the command it
claimed, spends its attempts, and the command dies — while its last dispatch, merely slow rather
than lost, arrives afterwards. Measured in
`tests/integration/test_control_plane_partition_kind_live.py`.

**Check:** `exa commands show <id>` — the command's `last_error` now names the run:

```
a Prefect flow run exists for this command: 69b604b8-…
```

`exa commands list --state dead` finds the commands; the control-plane log carries the same line at
warning level.

**Fix:**

1. **Look at the run before anything else.** If it is still going, let it finish: it is the work
   that was asked for. If it failed, decide on its merits.
2. **Do not resubmit while it runs.** That is the one action that turns this into two trainings.
3. **Then fix the cause** — the replica could not reach Prefect ([dead
   commands](#controlplanecommanddead), [PrefectCircuitBreakerOpen](#prefectcircuitbreakeropen)).
   Restarting the cut-off replica is what hands its claims to the others.

## ControlPlaneCommandBacklog {#controlplanecommandbacklog}

**Meaning:** more than 20 asynchronous commands have been waiting for 15 minutes.

**Impact:** retrains are accepted but not dispatched. Callers that waited for a dispatch reported
"accepted, not yet dispatched".

**Check:** `exa commands list --state pending` and `exa commands list --state failed`; `/health`
`dispatch` and `circuit_breaker`.

**Fix:**

- **The workers are off.** With `CONTROL_PLANE_COMMAND_WORKERS=0` nothing is ever dispatched.
- **Admission is full.** Retrains are admitted up to `EXAMLOPS_ADMISSION_MAX_RUNNING` in total and
  `EXAMLOPS_ADMISSION_PER_TENANT` per tenant; the rest wait for a slot. `exa admission stats`
  shows the queue by state **and how long the oldest queued item has been waiting** — counts alone
  cannot tell a busy queue from one nothing is draining. A slot held by a dispatch that died is released when its dispatch
  lease expires. If trainings are simply long, raise the caps.
- **A replica is alive but cut off from Prefect.** It keeps the commands it claimed — it is healthy
  and renews the claim, so nothing takes them over — and works through its attempts until they die
  ([what that leaves behind](#controlplanecommanddead)). Restarting that replica is what hands its
  claims to the others: the lease then expires with nobody renewing it.
- **A replica died holding commands.** They stay `dispatching` until their claim lease
  (`CONTROL_PLANE_COMMAND_LEASE_SECONDS`, 60 s) runs out. Then a surviving replica dispatches them
  with the same Prefect idempotency key, so a run the dead one did create is not created twice.
  A synchronous `POST /retrain` whose replica died becomes `failed` instead; its caller's retry
  with the same `Idempotency-Key` picks it up.
- **Prefect is down or the breaker is open:** commands fail and retry with backoff. Fix Prefect;
  the backlog then drains by itself.

## HighRetrainErrorRate {#highretrainerrorrate}

**Meaning:** more than 20 % of retrain dispatches failed over 15 minutes, sustained for 10. This
counts dispatches from both the command workers and the deprecated synchronous `POST /retrain`.

**Impact:** retrains are not starting. Asynchronous ones are retried, then go dead
([ControlPlaneCommandDead](#controlplanecommanddead)); synchronous callers see the error.

**Check:** `/health` `dispatch` and `circuit_breaker`; `exa commands list --state failed`;
`exa stack logs --service control-plane --tail 200` (look for `attempt … failed`).

**Fix:** as for the cause: Prefect unreachable, deployment missing, or parameters refused. See
the two alerts above.

## RetrainDurationP99High {#retraindurationp99high}

**Meaning:** at p99, creating the Prefect flow run for a retrain took more than 5 minutes (30-minute
window, sustained for 15).

**Impact:** dispatches are slow; the worker spends its time waiting on Prefect instead of draining
the queue.

**Check:** Prefect's own health (`exa stack logs --service orchestrator --tail 200`, the Prefect UI
on port 14200), and [PrefectRetryRateHigh](#prefectretryratehigh): retries stretch a dispatch.

**Fix:** a slow Prefect API is usually its database. Check the `prefect` Postgres database's load,
and restart the orchestrator if it is wedged.

## PrefectCircuitBreakerOpen {#prefectcircuitbreakeropen}

**Meaning:** the control plane's Prefect gateway circuit breaker opened in the last 5 minutes. It
opens after `PREFECT_CB_FAIL_MAX` (5) consecutive Prefect failures.

**Impact:** for `PREFECT_CB_RESET_TIMEOUT` (30 s) at a time, every dispatch fails fast instead of
waiting on Prefect. Asynchronous retrains retry later; synchronous callers see 503.

**Check:** `exa stack status` (orchestrator), `exa stack logs --service orchestrator --tail 200`,
and `/health` `circuit_breaker.state`.

**Fix:** bring Prefect back. The breaker lets one trial through after the reset timeout and closes
on success; nothing needs resetting by hand.

## PrefectRetryRateHigh {#prefectretryratehigh}

**Meaning:** the control plane retries Prefect calls more than once every 10 seconds, sustained
for 10 minutes.

**Impact:** Prefect is degraded but still answering. Dispatches are slow, and the breaker may open
next.

**Check and fix:** as for [PrefectCircuitBreakerOpen](#prefectcircuitbreakeropen): Prefect's
health, its database, and the network path between the control plane and the orchestrator.

## ApprovalsStale {#approvalsstale}

**Meaning:** the oldest pending approval has waited more than 24 hours (sustained for 30 minutes).

**Impact:** a model change from CI is blocked on a human decision.

**Check:** `exa approvals list`.

**Fix:** decide it: `exa approvals approve <model>`, or `exa approvals reject <model> --reason …`.
If nobody owns the queue, that is the actual problem.

## ApprovalsStaleUrgent {#approvalsstaleurgent}

**Meaning:** the oldest pending approval has waited more than 72 hours.

**Impact:** it is about to be auto-expired (`APPROVAL_EXPIRY_HOURS`). An expired change needs CI to
open it again.

**Check and fix:** as for [ApprovalsStale](#approvalsstale). Decide it now.

## PendingApprovalQueueLarge {#pendingapprovalqueuelarge}

**Meaning:** more than 10 approvals are pending, sustained for 5 minutes.

**Impact:** reviewers are behind; each change waits longer.

**Check:** `exa approvals list`. Many approvals for one model usually mean a CI loop resubmitting
it; many models means a real backlog.

**Fix:** review the queue. For a CI loop, fix the pipeline that keeps notifying
(`POST /v1/changes`), then retract the duplicates (`exa approvals delete <id>`; they are kept as
`retracted`).

## ManyApprovalsAutoExpired {#manyapprovalsautoexpired}

**Meaning:** more than 5 approvals auto-expired in the last hour.

**Impact:** model changes were dropped without a decision.

**Check:** `exa audit --last 1d` shows the expiries; compare with `APPROVAL_EXPIRY_HOURS`.

**Fix:** either nobody reviews the queue (assign an owner, or notify on
[ApprovalsStale](#approvalsstale)), or the expiry is too short for your review cadence.

## ApprovalMetricsUnreadable {#approvalmetricsunreadable}

**Meaning:** the control plane could not read its approval store while serving `/metrics`, in the
last 10 minutes.

**Impact:** the approval gauges are stale, so [ApprovalsStale](#approvalsstale) and
[PendingApprovalQueueLarge](#pendingapprovalqueuelarge) cannot be trusted. The same datastore
problem probably affects approvals themselves.

**Check:** `exa stack logs --service control-plane --tail 200` (the read error); the datastore
(`CONTROL_PLANE_DB`, or Postgres when `EXAMLOPS_DB_BACKEND=postgres`).

**Fix:** restore the datastore; the next scrape clears it.

## AuditEventsDropped {#auditeventsdropped}

**Meaning:** the control plane attempted to write one or more audit events and could not. The
action it was recording **still happened** — audit writes deliberately
[fail open](../guides/audit-trail.md#complete-by-construction-was-too-strong-corrected-2026-09-14),
because a promotion must not be refused and a secret must not go un-rotated just because the audit
datastore blinked.

**Impact:** the audit trail is **incomplete for this window**, and nothing else can tell you that.
`exa audit verify` will still pass: the hash chain is recomputed over the rows that exist, so an
event that never arrived leaves a perfectly valid chain — a missing *link* breaks it, a missing
*event* does not create one. Every coverage or completeness statement about this window is
therefore unsound, including the EU AI Act
[Art. 12 figure](../guides/eu-ai-act-compliance.md#art-12-record-keeping). Treat one lost event as
an incident, not as a warning: there is no acceptable rate.

**Check:**

1. Which actions, from the alert's `action` label — `promotion`, `approval`, `retrain_triggered`,
   `drift_auto_retrain_triggered` and `eval_gate_override` are the Art. 12 set.
2. `exa stack logs --service control-plane --tail 200 | grep "audit event LOST"` — each loss logs
   the action, target, actor, tenant and cause with a traceback.
3. The datastore itself (`CONTROL_PLANE_DB`, or Postgres when `EXAMLOPS_DB_BACKEND=postgres`) and
   [ApprovalMetricsUnreadable](#approvalmetricsunreadable), which usually fires alongside.

**Fix:** restore the datastore. The counter is per-process and does not decrease, so it will keep
reporting until the control plane restarts — that is deliberate, because the window it describes
does not stop having been incomplete.

**Afterwards:** reconstruct what was lost from the logged records and note the window in your
compliance record. The events cannot be back-filled: writing them now would put them at today's
position in an append-only chain, which is a worse falsehood than the gap.

> **This counter covers the control plane only.** Every process that writes audit events keeps its
> own tally, and the others — the CLI, the agent, the bridge — are short-lived or do not serve
> `/metrics`, so their losses reach the log but not this alert.
