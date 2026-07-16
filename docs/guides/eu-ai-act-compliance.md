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
