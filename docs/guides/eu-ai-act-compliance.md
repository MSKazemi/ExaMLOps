# EU AI Act Compliance Tooling (D1)

> Next-Gen 40 · feature **D1** · ADR 0012 · spec `design/vision/specs/D1-eu-ai-act-compliance.md`

> **DISCLAIMER:** This feature assembles compliance *evidence* from platform metadata. It
> is **not legal advice, a conformity assessment, or CE certification.** Consult a qualified
> authority before making any EU AI Act declaration. Every generated document and CLI
> surface repeats this disclaimer.

D1 maps ExaMLOps metadata to EU AI Act obligations: per-system **risk classification**, an
auto-generated **Annex-IV technical file** assembled from live artifacts, **Art. 12**
logging conformance, and a **conformity state machine** — all built on a reusable
control → article → evidence framework (shared with D2 / NIST AI RMF).

## Risk classification

```bash
exa compliance classify JPCP --risk-tier high \
    --purpose "HPC job triage" --context "internal operations"
```

Risk tiers: `prohibited | high | limited | minimal`. Classifying a model marks it
**in-scope** and records its intended purpose + deployment context.

**Promotion gate (R2):** an in-scope model with **no** risk classification is blocked from
promotion until it is classified:

```bash
exa pipeline promote jpcp --if-rmse-lt 5.0
# → error: Promotion blocked: in-scope system has no EU AI Act risk classification. Use --force to override (audited).
```

## Annex-IV technical file

```bash
exa compliance technical-file JPCP --out jpcp_annex_iv.md
```

The document is **assembled from live evidence** — never hand-maintained (R5) — and pulls
from the other Next-Gen tracks:

| Section | Annex IV | Evidence source |
|---|---|---|
| System description | §1 | model card (A6) |
| Development process | §2(b) | lineage (A2) |
| Data governance | §2(d) | data versioning + quality (A1/A5) |
| Performance | §3 | evaluation (C2) |
| Fairness | §3 | subgroup report (C8) |
| Risk management | §5 | guardrails + drift (D8/C5) |
| Integrity | §2(e) | AI-BOM + signature (D3) |
| Monitoring | §8 | drift + SLO (C5/C6) |
| Record-keeping | Art. 12 | audit trail (D4) |
| Changes | §6 | change log |

Missing evidence is **flagged** in the document (`⚠️ MISSING EVIDENCE`), never silently
omitted (R4). Each generation is stored as a new **version** in `technical_files`.

Existing evidence is also checked for **integrity** (ADR 0110 decision 6). A section whose
records come from a broken audit chain or a broken telemetry anchor is flagged
`⚠️ INSUFFICIENT EVIDENCE` and counted as a gap, alongside missing sections. A section resting on
records outside the chain and its anchors is marked *not tamper-evident*, which names it without
counting it as a gap. The file opens with an **Insufficient evidence** section listing both, with
reasons, and an **Evidence integrity** summary. `--json` reports `missing`, `insufficient`,
`unverified`, and a `status` and `reasons` per section. The statuses are explained in
[Evidence chain](evidence-chain.md#what-a-compliance-pack-will-not-vouch-for).

## Art. 12 record-keeping

```bash
exa compliance art12 JPCP
```

Verifies that the required operational event types (retrains, promotions, approvals,
overrides) are present in the immutable audit trail (D4) and reports coverage + uncovered
event types.

## Conformity state machine

```bash
exa compliance declare JPCP --state documented
exa compliance declare JPCP --state assessed
exa compliance declare JPCP --state declared
```

Valid states: `draft → documented → assessed → declared` (with limited back-transitions).
Skipping a step (e.g. `documented → declared`) is rejected (R8).

## Declaration of Conformity (Annex V)

`declared` is a state; the **document** is what the state exists to reach.

```bash
exa compliance declaration JPCP \
    --issued-at "Julich, 2026-09-02" \
    --provider "Example GmbH" --provider-address "Example Str. 1, 52425 Example, DE" \
    --signatory "A. Person" --signatory-function "Head of AI Governance" \
    --standard "EN ISO/IEC 42001:2023" \
    --out declaration.md
```

Annex V requires eight items. The platform fills the ones it can know — system identity, risk
tier, conformity state, the Annex-IV technical file version it rests on and that file's gap
count, and the audit-chain head that makes the claim traceable.

**It will not fill the other five.** Provider legal name and address, signatory name and
function, notified body, and harmonised standards are statements only you can make: the platform
holds no legal entity and no signatory. Anything you do not supply renders as
`⚠️ TO BE COMPLETED BY THE PROVIDER`.

### When a declaration is FINAL

Only when all three hold:

1. the conformity state is `declared`,
2. every provider-supplied field is present, and
3. the referenced Annex-IV technical file has **no evidence gaps**.

Otherwise the document is stamped `DRAFT — NOT A DECLARATION` and lists, on its own face, every
reason it is not final. **There is no `--force`.** An override that produces a final-looking
regulatory artifact over a listed objection is precisely the thing this generator must not
offer — if a blocker is wrong, fix the underlying fact, not the document.

Two Annex-V items are conditional and are treated as such rather than as blanks: a system that
processes no personal data gets a recorded statement that it does not, never a GDPR conformity
claim nobody made (V(5)); and an absent notified body reads "Not applicable", because most
systems genuinely have none and that is an answer (V(7)).

`--issued-at` is required. The issue date is a fact about when a person signed, not about when
the generator ran.

Each generated declaration is versioned and retained alongside the technical files, in its own
version sequence. **Art. 47's ten-year retention is not enforced by the platform** — the
documents are stored and every generation is audited, but nothing prevents their removal.

## The shared framework

```bash
exa compliance framework          # the control→article→evidence mapping
```

This data-driven mapping is reused by **D2 (NIST AI RMF)** with a different article
vocabulary — one framework, two regulatory lenses (R9).

## Programmatic use

```python
from examlops.compliance import (
    classify_system, generate_technical_file, check_art12_logging, set_conformity_state,
)

classify_system("JPCP", "high", "HPC triage", "internal", actor="alice")
doc = generate_technical_file("JPCP")     # Document; doc.gaps flags missing evidence
print(doc.to_markdown())
coverage = check_art12_logging("JPCP")    # {'coverage': {...}, 'uncovered': [...]}
set_conformity_state("JPCP", "documented", actor="alice")
```

## Governance

- **Audited (D4):** `compliance_classified`, `compliance_conformity`,
  `promotion_blocked_by_compliance`, `compliance_gate_override`.
- **Tenant-scoped (D6):** every record carries a `tenant`.

## See also

- [Fairness (C8)](fairness.md), [SLOs (C6)](slos.md), [Advanced drift (C5)](drift-advanced.md) — evidence sources.
- [Supply-chain security (D3)](supply-chain-security.md) — AI-BOM + signatures.
- [Lineage (A2)](lineage.md) — development-process evidence.
