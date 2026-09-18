# Evidence chain — correlation, causation and autonomy

The audit log has been tamper-evident for a while: every event is hash-chained, so an edit, a
deletion or a reordering breaks the chain. What it could not do is explain *causation*. An
autopilot cycle that triggers a retrain which promotes a model wrote three unrelated rows, and
nothing joined them.

That matters because the question worth asking about an autonomous platform is not "what
happened" but:

> For any autonomous action taken in the last 30 days — **who did it, on whose behalf, under
> which mode, and how would it be undone?**

Answering that from the chain alone needs causal edges, so every event can now carry them.

## The fields

| Field | Answers |
|---|---|
| `correlation_id` | which unit of work this event belongs to |
| `parent_correlation_id` | which unit of work *caused* it |
| `mode` | `manual` · `delegated` · `autonomous` |
| `on_behalf_of` | the principal the actor acted for |
| `rollback_ref` | how this action would be undone |

`actor` alone cannot express `on_behalf_of`: a service account acting for a person and the same
account acting on its own initiative are different acts, and only the second is what ADR 0113
gates hardest.

## They are inside the hash, not beside it

A causal edge an attacker could rewrite without breaking the chain would be evidence of nothing,
so the correlation fields are part of what gets hashed. The canonical form folds them in **only
when they carry something**, which is what lets every event written before this existed keep
verifying against the hash it was stored with.

## Correlation is ambient, not a parameter

Roughly two hundred call sites already write audit events. Threading an id through all of them
would be a large mechanical change whose failure mode is silent — one missed call site is an
unexplained gap in a causal chain, and nothing would report it. Instead the context is ambient:

```python
from examlops.evidence import correlated, AUTONOMOUS

with correlated(mode=AUTONOMOUS, on_behalf_of="autopilot"):
    ...                       # every audit event written in here is correlated,
    with correlated():        # and this nested block becomes a child of the one above
        ...
```

A call site gains correlation by being *inside* the unit of work rather than by remembering to
say so. Nesting sets `parent_correlation_id` automatically — that nesting **is** the
orchestrator → tool → downstream chain.

`mode` and `on_behalf_of` are inherited by nested work (a tool called by an autonomous cycle is
also acting autonomously). `rollback_ref` is deliberately **not** inherited: a parent's inverse
does not undo a child, and inheriting it would let an action claim an undo path that does not
undo it — worse than admitting it has none.

When the inverse is only knowable after the fact:

```python
from examlops.evidence import with_rollback_ref

with_rollback_ref(f"restore:alias/{alias}/{previous_version}")
```

## Commands

```bash
# Reconstruct one unit of work and everything it caused
exa audit chain <correlation-id>

# The gate, as a command: every autonomous action and whether it declared an inverse
exa audit autonomy --last 30d
exa --json audit autonomy --last 7d
```

`exa audit autonomy` lists actions with **no** `rollback_ref` rather than filtering them out.
ADR 0110 decision 4 calls a NULL `rollback_ref` on an autonomous action a policy violation; the
refusal that enforces it is not built yet, so for now they are reported and the command says so.

### The listing is bounded; the counts are not

`--limit` (default 500) caps how many actions are **listed**. It never caps the numbers: `count`,
`undoable` and `without_rollback` are counted by the database over the whole `--last` window, and
the command says so when it truncated the page:

```text
Showing the 500 most recent of 6210 autonomous action(s) in the window.
The counts below are for the whole window, not this page.
```

This separation is the point rather than a detail. A violation is, by nature, the rare row — so a
count taken from the length of a page answers "violations among the newest 500" while reading as
"violations in the window", and the more autonomous work a platform does, the more confidently it
reports zero. The same count decides whether the `record_keeping` section of an EU AI Act technical
file is *insufficient*, and the section's own message sends an auditor to this command, so the two
surfaces have to agree on the number.

Raise `--limit` to see the older ones rather than only count them:

```bash
exa audit autonomy --last 365d --limit 5000
```

## What is wired up

`exa drift trigger` and `exa autopilot run` both declare themselves `autonomous` for the whole
cycle, so the retrains they fire — and the ADR-0114 suppressions they record — carry the mode.
Anything those paths call inherits it.

## Generalised rollback — the refusal

`exa audit autonomy` lists what the platform did on its own and which of those declared no
inverse. `examlops.rollback` is the half that makes the list actionable: **an autonomous action
with no `rollback_ref` is refused before it runs**, not reported after.

The distinction that makes the rule workable is between an action that *changed* something and
one that only *recorded* something. A suppressed retrain, a policy denial and a cycle-complete
marker mutate nothing, so demanding an inverse for them would be a tax that teaches operators to
declare fake ones — and a fake inverse is worse than a missing one, because it reads as an undo
path that does not undo. Every registered action is one of three kinds:

| Kind | Meaning | Gated? |
|---|---|---|
| `mutating` | changed state; declares the command that undoes it | yes — needs a `rollback_ref` |
| `record_only` | changed nothing | no |
| `no_autonomy` | changed state with no safe inverse; a person may still do it deliberately | always refused autonomously |

**An unregistered action counts as gated.** Defaulting an unknown action to "allowed" is the
failure that would quietly reopen the gap: the next autonomous action somebody adds would sail
through by virtue of nobody having thought about it. A coverage guard fails the build when an
agent-drivable module writes an audit action the registry does not classify.

Only `autonomous` mode is gated. A person may deliberately do things the platform must not do to
itself — that asymmetry is ADR 0113's autonomy model, not an oversight.

The inverse is declared **before** the action runs, from state read at that moment: once a
retrain has promoted, the version a rollback would restore is no longer the one the alias points
at. Where the previous version cannot be resolved, no inverse can be built and the action is
declined rather than taken with an undo path that does not exist.

## Blast-radius contracts, per-behaviour autonomy, live-run interrupt (ADR 0113)

Every autonomous behaviour publishes a **versioned, machine-readable contract** stating what it
may and may not change, how far one action may reach, and how it is undone. The contract is
*enforced* at the decision point — a change outside `may_change` is denied and the denial names
the exact clause (a `contract_denied` audit event carries it) — so contract drift surfaces as a
failure, never as a stale document.

```bash
exa autopilot contract                      # every behaviour's contract, verbatim
exa autopilot contract drift_auto_retrain   # one behaviour
exa autopilot status                        # contracts + effective autonomy + run history
```

An operator can **narrow** a contract with a YAML overlay (`EXAMLOPS_CONTRACTS_FILE`): move
autonomy toward REVIEW/DISABLED, add `may_not_change` entries, shrink extent caps. Widening is
deliberately impossible from config — that is a code change with review.

**Autonomy is per behaviour, not global** (`AUTONOMOUS | REVIEW | DISABLED`), each individually
pausable without losing its configuration. Granting AUTONOMOUS records an explicit human
acknowledgment; a grant without one degrades to REVIEW:

```bash
exa autopilot autonomy autopilot_promote REVIEW           # pause just promotions
exa autopilot autonomy drift_auto_retrain AUTONOMOUS \
    --ack "retrain autonomy accepted for the staging fleet"  # recorded + audited
```

**One in-flight run can be interrupted** — no more all-or-nothing kill-switch:

```bash
exa autopilot interrupt 42 --freeze     # pause run 42 at its next checkpoint
exa autopilot resume 42                 # let it continue
exa autopilot interrupt 42 --kill       # abort it (audited; the run record says so)
exa autopilot quarantine JPCP --reason "drift sensor suspect"   # contain one model
exa autopilot release JPCP
```

## Anchored telemetry (ADR 0110 decision 2)

High-volume telemetry — one row per inference (`drift_snapshots`, `input_snapshots`), per job
(`hpc_jobs`), per dataset revision (`dataset_revisions`) — cannot run through the serialised
chain without making the chain the platform's write bottleneck. Instead each side table is
**anchored**: a `telemetry_anchor` chain event carries a SHA-256 over the table's new rowid
range. Tampering with an anchored row breaks the anchor; the anchor is protected by the chain.

```bash
exa audit anchor           # anchor all new rows (cron-able; the autopilot anchors each cycle)
exa audit verify-anchors   # recompute every anchor; exit 1 on a break
```

Each anchor names its own range, so the verifier always knows the guarantee it is checking.
An audited retention prune (`exa data retention-prune`) reports as *pruned*, never tampering;
rows newer than the last anchor are counted and reported, never silently skipped.

## Reviewed audit (ADR 0113 decision 5)

An unreviewed audit trail is theatre. `exa audit review` samples everything since the last
recorded review, shows it, and writes an `audit_reviewed` chain event naming the reviewer,
covered range, sampled ids and notes — so the review cadence is itself auditable:

```bash
exa audit review --sample 25 --notes "weekly pass"
exa audit reviews          # who reviewed, when, covering what
```

## What a compliance pack will not vouch for

The chain and the anchors decide more than whether tampering is detectable. They also decide what
a compliance pack may claim (ADR 0110 decision 6). The Annex-IV technical file
(`exa compliance technical-file`) and the NIST AI RMF coverage report (`exa governance report`)
judge every section against the records its evidence comes from. They don't just ask whether
evidence exists:

| status | meaning | counts as a gap |
|---|---|---|
| **verified** | evidence exists, and every record it rests on was checked and passed | no |
| **not tamper-evident** | evidence exists, but some of it lives in a table outside the chain and its anchors, so an edit would go unnoticed | no, but it is named |
| **insufficient** | evidence exists, but its check **failed** (a broken chain link or a broken anchor), **could not run**, or the record shows an autonomous action with no `rollback_ref` | **yes** |
| **missing** | no evidence found | **yes** |

A check that raised an error is *insufficient*, never *verified*: an unrun check is the absence of
one. Because insufficient sections count as gaps, a Declaration of Conformity whose technical
file rests on a broken chain stays a **draft**.

The technical file opens with an **Insufficient evidence** section listing every section it
cannot vouch for, with the reason, followed by an **Evidence integrity** summary: the chain's
state and head hash, and the anchors checked, broken or pending. A partial pack that names its
own gaps is useful; a confident partial pack is not.

```text
## Insufficient evidence
- **Changes** (Annex IV §6) — INSUFFICIENT
    - audit chain is broken at event 412 (hash mismatch (event altered)) — events after it cannot be relied on
- **Data Governance** (Annex IV §2(d)) — not tamper-evident
    - dataset_revisions: 3 row(s) newer than the last anchor (run: exa audit anchor)
    - data_quality_checks is outside the hash chain and the telemetry anchors
```

Which tables each section draws on is declared once, in
`examlops.compliance.sufficiency.EVIDENCE_SOURCES`. A test fails if a collector has no entry,
so a new section can never be treated as verified by default.

## Design

- ADR 0110 — *Evidence chain: synchronous actions, anchored telemetry* (G1.6 · G1.7 · G8.3 · G8.4)
- ADR 0113 — *Blast-radius contracts and a provable L3 autonomy* (`examlops.blast_radius`)
- `examlops.evidence` — the correlation context
- Related: [audit trail](audit-trail.md) for the hash chain itself
