# Governance & Compliance

The **Governance** page renders the platform's compliance posture: NIST AI RMF control coverage, EU AI
Act status per model, model-card coverage, and audit-trail integrity. It is deliberately **honest** —
it reports *evidence coverage*, not certification, and surfaces gaps rather than showing false green.

Open it from the sidebar (**Governance**, admin only) or navigate to `/governance`.

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

`audit_events` stores no per-row hash, so the backend computes a deterministic **SHA-256 hash-chain**
over the ordered events and exposes the `headDigest`. Because each event hashes the previous digest
plus its immutable fields, changing any past event changes the head — so an external copy of the
digest is **tamper-evidence**. The page shows a "Chain verified" badge + the short digest.

## Endpoint

```
GET /api/v1/governance/overview     # viewer role; BFF-composed, partial-failure safe
```

Returns `{posture, compliance, cards, audit}`. See [`docs/reference/api.md`](../reference/api.md) for
the full shape and [`docs/dashboard/architecture.md`](../dashboard/architecture.md#governance-compliance-f14)
for the diagram.

## Notes & limits

- Reads from `platform.db` (`PLATFORM_DB`); missing tables degrade to empty/gap, never an error.
- This slice ships posture + EU-AI-Act + card coverage + audit integrity. The richer F14 surfaces —
  policy-as-code dry-run + decision logs (R4), supply-chain signature/AI-BOM/SLSA + fairness subgroup
  slices (R5), approvals-2.0 with diff+evidence, and one-click compliance-export PDF (R6) — build on
  this and are tracked in the dashboard-nextgen plan. The faceted audit **grid** lands with F17.
