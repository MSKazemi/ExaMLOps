# AI Governance — NIST AI RMF Control Backbone (D2)

> Next-Gen 40 · feature **D2** · ADR 0027 · spec `design/vision/specs/D2-nist-ai-rmf.md`

> This produces an **evidence-coverage** report, **not** a certification or conformity
> assessment.

D2 gives ExaMLOps a versioned **NIST AI RMF** control catalogue, a CI-validated
feature→control mapping, and an auto-generated coverage report crosswalked to the **EU AI
Act** (D1) and **ISO/IEC 42001**. It shares D1's evidence-collector layer, so one pass of
evidence collection serves both regulatory frameworks.

## The catalogue

`examlops/governance/catalogue.yaml` is a versioned YAML encoding NIST AI RMF functions
(**Govern / Map / Measure / Manage**) plus the **GenAI Profile**. Each control has an id,
function, description, the platform evidence it requires, and a crosswalk.

```bash
exa governance catalogue          # list controls + version
exa --json governance catalogue
```

## Validating the mapping (CI gate)

Every control's `evidence` key must resolve to a real collector in `examlops.compliance`.
`exa governance validate` (and `test_governance.py`) fail if a control references a
nonexistent collector, an unknown function, or has no evidence — so the mapping can't
silently drift from the code:

```bash
exa governance validate           # exit 1 on any mapping error
```

## Coverage report

```bash
exa governance report             # fleet-wide posture across all in-scope systems
exa governance report --model JPCP
```

Each control is marked:

| Status | Meaning |
|---|---|
| **satisfied** | all required evidence present |
| **partial** | some evidence present |
| **gap** | no evidence — a missing source is **always** a gap, never a false pass (R4) |

Evidence that exists but fails its integrity check (a broken audit chain or anchor, or a check
that could not run) is **not** counted toward *satisfied*. It is listed under
`insufficient_evidence`, and `evidence_notes` explains why, including sources that are not
tamper-evident. The statuses are explained in
[Evidence chain](evidence-chain.md#what-a-compliance-pack-will-not-vouch-for).

The report shows the NIST function, the EU AI Act crosswalk, the ISO/IEC 42001 clause, and
which evidence is missing. Reports are versioned, **audited** (D4), and **tenant-scoped**
(D6).

## Crosswalk

```bash
exa governance crosswalk          # control → EU AI Act article + ISO/IEC 42001
```

One control catalogue, three regulatory lenses — the crosswalk lets a single evidence pass
answer NIST RMF, EU AI Act (D1), and ISO/IEC 42001 questions at once.

## Shared evidence layer

The `evidence` keys in the catalogue (e.g. `fairness`, `monitoring`, `record_keeping`)
resolve to the **same collectors** used by D1's Annex-IV technical file. Adding a collector
benefits both frameworks; D1 gained `data_governance`, `risk_management`, and `changes`
collectors as part of D2 so every NIST control resolves.

| NIST function | Example control | Evidence | Source |
|---|---|---|---|
| Map | MAP-1.1 | system_description | model card (A6/D1) |
| Map | MAP-2.3 | development_process | lineage (A2) |
| Measure | MEASURE-2.3 | performance | eval (C2) |
| Measure | MEASURE-2.11 | fairness | subgroup report (C8) |
| Measure | MEASURE-2.7 | integrity | AI-BOM + signature (D3) |
| Manage | MANAGE-2.2 | monitoring | drift + SLO (C5/C6) |
| Manage | MANAGE-4.1 | record_keeping | audit trail (D4) |
| GenAI | GENAI-3.2 | risk_management | guardrails (D8) + drift |

## Programmatic use

```python
from examlops.governance import load_catalogue, validate_mapping, governance_report, crosswalk

assert validate_mapping() == []          # CI gate
rep = governance_report("default", model="JPCP")
print(rep.summary)                        # {'satisfied': N, 'partial': N, 'gap': N}
for cw in crosswalk():
    print(cw["control"], cw["eu_ai_act"], cw["iso_42001"])
```

## See also

- [EU AI Act compliance (D1)](eu-ai-act-compliance.md) — the crosswalk target and shared evidence layer.
- Evidence sources: [Fairness (C8)](fairness.md), [SLOs (C6)](slos.md), [Advanced drift (C5)](drift-advanced.md), [Supply-chain security (D3)](supply-chain-security.md), [Lineage (A2)](lineage.md), [Guardrails (D8)](guardrails.md).
