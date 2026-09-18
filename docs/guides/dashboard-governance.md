# Governance & Compliance

The **Governance** page renders the platform's compliance posture: NIST AI RMF control coverage, EU AI
Act status per model, model-card coverage, and audit-trail integrity. It is deliberately **honest** —
it reports *evidence coverage*, not certification, and surfaces gaps rather than showing false green.

Open it from the sidebar (**Govern → Governance**, admin only) or navigate to `/govern/governance`
(the old `/governance` URL redirects there). The EU AI Act technical file and Art. 12 coverage have
their own page, **Govern → Compliance** (`/govern/compliance`).

- **Feature:** F14 · **Design:** ADR 0063 (`design/adr/0063-dashboard-governance-compliance-surface.md`) ·
  **Spec:** `design/vision/specs/F14-governance-compliance-surface.md`
- **Backend:** `platform/services/dashboard/backend/governance.py` + `routers/governance.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/governance.ts` + `pages/Governance.tsx`

## What you see

### NIST AI RMF posture (R1)

A list of controls, each graded from real evidence:

| Control | Satisfied when… |
|---|---|
| `GOVERN-1.1` Audit trail present | any audit events exist |
| `MAP-1.1` Risk classification | compliance records exist |
| `MEASURE-2.1` Model documentation | graded by model-card coverage (full → satisfied, some → partial, none → gap) |
| `MANAGE-4.1` Change approval logged | approval audit events exist |

Status is `satisfied` / `partial` / `gap` — **never false green**. With no evidence, every control
reads `gap`.

### EU AI Act (R2)

Per-model risk class, Annex-IV technical-file presence, and provenance-hash presence (from
`compliance_records`).

### Model-card coverage (R5)

The fraction of known models that have a model card, with the missing models named explicitly (the
completeness gate that ties into F9 promotion).

### Audit integrity (R3)

The page reports the audit log's **own** hash chain — the one
[`exa audit verify`](audit-trail.md) checks and the one every event carries in its `prev_hash` /
`hash` columns. `headDigest` is the stored hash of the newest chained event, so an external copy of
it is tamper-evidence that any other tool can reproduce.

| Field | What it means |
|---|---|
| `headDigest` | the newest chained event's stored hash — the anchor worth copying |
| `count` | events in the log |
| `unchained` | events carrying **no** hash: outside the chain, so their order and presence are not tamper-evident. Counted, never hidden |
| `verified` / `verifiedScope` | whether the **newest few links** recompute, and a phrase naming exactly that scope |

**`verified` is deliberately narrow.** A hash chain is only proven from genesis, and reading a log
that grows forever on every page render is not something a dashboard can do — so the page verifies
the tail in constant time and *says* that is what it did. Full verification is `exa audit verify`,
which reads the log end to end and names the first broken link.

> **This was wrong until 2026-09-13, and the correction is worth knowing** if you ever copied a
> digest from this page. `audit_events` genuinely stored no per-row hash when the page was built,
> so it computed a chain of its own over five columns. The platform has stored a real chain since,
> and the page had become a *parallel* digest that no other tool could reproduce — while its
> `verified` flag was true by construction, because a recomputation is always self-consistent with
> itself. It also read every row of the log per render (139 ms at 100 000 events, and a dict built
> per event to return twenty; now 1.9 ms). Re-copy any digest you were holding: the value has
> changed, and the new one is the log's own.

## Endpoint

```
GET /api/v1/governance/overview     # viewer role; BFF-composed, partial-failure safe
```

Returns `{posture, compliance, cards, audit}`. See [`docs/reference/api.md`](../reference/api.md) for
the full shape and [`docs/dashboard/architecture.md`](../dashboard/architecture.md#governance-compliance-f14)
for the diagram.

## An empty register and an unreadable one are different answers

A console that draws an empty table the same way whether the register *is* empty or the query
*failed* is telling the operator something it has not checked. On most panels that is a nuisance. On
these six it is a claim:

| Read | What an empty answer asserts |
|---|---|
| `GET /api/compliance/systems` | no model is in scope of the EU AI Act |
| `GET /api/fairness` | no fairness policy applies to any model |
| `GET /api/slo` | no service-level objective is defined |
| `GET /api/drift/status` | no model's predictions have moved |
| `GET /api/drift/input-status` | no model's inputs have moved |
| `GET /api/drift/auto-retrain` | no model retrains itself |

Until 2026-09-14 each of those wrapped its query in `except Exception: return []`. A table that a
half-applied migration had not created, a datastore the dashboard process could not reach, or a
`NameError` introduced by an edit produced a clean, entirely fictional register — and produced it
**silently**, with nothing in the service log to say a read had failed at all.

They now answer **503** with the surface named (`the EU-AI-Act system register is unavailable
(OperationalError)`), which is the convention these same routers already used when their *import*
was unavailable. The console renders it as an error with a retry, never as an empty register:

- the cause is logged at `WARNING` with a traceback and a running per-surface count;
- `readfail.read_failures()` reports how many reads of each surface this process has failed to
  serve — a non-zero count means the console refused to draw a panel, which is a datastore
  incident, not a UI preference;
- the exception's **text** is never sent to the browser, only its class. A datastore error message
  carries file paths, table names and, on Postgres, connection details.

The distinction is enforced on both sides. `tests/unit/test_failed_reads_are_not_empty_reads.py`
ratchets the remaining silent handlers in every dashboard router — the count may only shrink, and
these six files must hold none — and the Compliance page has its own test that a failed read is
never drawn as "No systems in the register".

**What did not change:** a register that really is empty still answers `200 []`, and the page still
says so. That is asserted in the same test, so the refusal cannot pass by refusing everything.

## Notes & limits

- Reads from `platform.db` (`PLATFORM_DB`). A **missing row** degrades to empty/gap, as before; a
  **failed read** on the six governance and risk surfaces above is now a 503 (see the section
  above). The BFF-composed `governance/overview` was already honest and is unchanged: a section
  that cannot be built is **omitted** from the payload and **named** in `_partial`, so the page
  shows the rest without claiming the missing section was empty (`bff.aggregate`).
- This slice ships posture + EU-AI-Act + card coverage + audit integrity. The richer F14 surfaces —
  policy-as-code dry-run + decision logs (R4), supply-chain signature/AI-BOM/SLSA + fairness subgroup
  slices (R5), approvals-2.0 with diff+evidence, and one-click compliance-export PDF (R6) — build on
  this and are tracked in the dashboard-nextgen plan. The faceted audit **grid** lands with F17.
