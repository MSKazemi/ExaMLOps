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

## Not yet implemented

Two of W2's six items are done. Still open, from ADRs 0110 and 0113:

- **Anchored telemetry** — lineage and resource events into side tables with a periodic
  checkpoint hash, so high-volume telemetry is tamper-evident without serialising it through the
  chain.
- **Blast-radius contracts** — a declarative `may_change` / `may_not_change` per behaviour, with
  denials naming the clause.
- **Live-run interrupt** — freeze, kill or quarantine a single in-flight run. Today only a global
  kill-switch exists.
- **Per-rule autonomy** — autonomy declared per behaviour rather than globally, individually
  pausable without losing its configuration.

## Design

- ADR 0110 — *Evidence chain: synchronous actions, anchored telemetry* (G1.6 · G1.7 · G8.3 · G8.4)
- `examlops.evidence` — the correlation context
- Related: [audit trail](audit-trail.md) for the hash chain itself
