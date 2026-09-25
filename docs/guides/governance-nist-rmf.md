# AI Governance — NIST AI RMF Control Backbone (D2)

> Next-Gen 40 · feature **D2** · ADR 0027 · spec `design/vision/specs/D2-nist-ai-rmf.md`

> This produces an **evidence-coverage** report, **not** a certification or conformity
> assessment.

D2 gives ExaMLOps a versioned **NIST AI RMF** control catalogue, a CI-validated
feature→control mapping, and an auto-generated coverage report crosswalked to the **EU AI
Act** (D1) and **ISO/IEC 42001**. It shares D1's evidence-collector layer, so one pass of
evidence collection serves both regulatory frameworks.

## The catalogue

`examlops/governance/catalogue.yaml` (version **2.0.0**) is a versioned YAML with two layers:

- **The framework** — all **72** NIST AI RMF 1.0 subcategories (Govern 19 · Map 18 ·
  Measure 22 · Manage 13), with short paraphrased titles. The normative text is NIST AI 100-1;
  the titles are there to navigate by.
- **The platform controls** — the 12 things this platform can collect live evidence for. Each
  has an id, function, description, the evidence it requires, the subcategories it contributes
  to (`nist_subcategories`), and the EU AI Act / ISO/IEC 42001 crosswalk. A null crosswalk
  cell means no clause is claimed, rather than a guessed one.

```bash
exa governance catalogue                  # platform controls + their evidence
exa governance catalogue --subcategories  # all 72 subcategories and the controls reaching each
exa --json governance catalogue
```

A subcategory that no platform control reaches is reported as **organisational**. It needs
evidence the platform does not hold, such as policies, training records or stakeholder
engagement. It is never counted as covered. On catalogue 2.0.0, 13 of the 72 subcategories are
reachable by a platform control and 59 are organisational.

**2.0.0 changes.** The RBAC control moved from `GOVERN-4.1` to `GOVERN-2.1`. NIST's GOVERN 4.1
is about a safety-first culture, and GOVERN 2.1 covers roles and responsibilities. The old id
is still accepted as an alias (`resolve_control("GOVERN-4.1")`). The control now needs real
authorization evidence. Before this change, audit-trail coverage alone satisfied it.
MEASURE-2.7 also needs secrets evidence, MAP-2.3 also needs data governance, and MANAGE-4.1
also needs the change log. GOVERN-1.6 (AI-system inventory) and MEASURE-2.12 (environmental
impact) are new.

## Feature declarations

`examlops/governance/features.yaml` records, for each platform feature, the controls it serves
(`controls`), the evidence it emits (`emits`) and the module that emits it (`module`). The
report uses it to list the features behind each control, so every gap has a named owner.

```bash
exa governance features           # feature → controls / evidence / module
```

## Validating the mapping (CI gate)

`exa governance validate` checks the following, and so does `tests/unit/test_governance*.py`
in CI. It exits 1 on any failure.

- every control uses a known function and names evidence that has a real collector in
  `examlops.compliance`;
- the framework layer holds exactly 19/18/22/13 subcategories with no duplicates, and every
  subcategory a control names exists;
- every feature `module` resolves, every `emits` key has a collector, and every declared
  control exists;
- a feature may only claim a control if it emits at least one of that control's evidence
  keys;
- every evidence key each control requires is emitted by a feature that declares that
  control;
- every collector is emitted by some feature.

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

The report shows the NIST function, the features behind each control, the EU AI Act
crosswalk, the ISO/IEC 42001 clause, and which evidence is missing. It also rolls the controls
up to the framework:

- a **subcategory** is *satisfied* only when every control mapped to it is satisfied;
- it is a *gap* when none of those controls has evidence, and *partial* otherwise;
- it is *organisational* when no platform control reaches it.

In JSON output, `framework` holds the totals and `subcategories` holds the per-subcategory
rows.

Reports are versioned and **audited** (D4). A persisted report writes a `governance_report`
event under its tenant, with the control summary and framework figures. Reports are also
**tenant-scoped** (D6).

## Authorization and secrets evidence (D6 / D7)

| Evidence | Present when | Not enough on its own |
|---|---|---|
| `access_documented` | at least one **owner** relation in `authz_relations` covers the model: `model:<m>`, `…/model:<m>`, or a project the model is assigned to | viewer or editor grants without an owner (nobody is accountable) |
| `access_enforced` | `EXAMLOPS_MULTITENANCY` is on, so D6 checks are default-deny. The content names the decision point (OpenFGA or the local store) and counts the audited `authz_*` decisions on the system | relations with enforcement off. In single-tenant mode every check allows |
| `secrets_managed` | OpenBao/Vault is configured, or the tenant has Fernet-encrypted secrets in the local store | plain environment variables |
| `secrets_rotation` | every stored secret for the tenant was written or rotated within `EXAMLOPS_GOVERNANCE_SECRET_MAX_AGE_DAYS` (default 90; malformed or out-of-range values fall back to 90). Only a written value counts: `exa secrets rewrap` re-encrypts the same value under a new key, so it keeps the secret's `updated_at` and is not counted as a rotation | a store held only in OpenBao. Its version history is not visible to the platform, so it is reported as a gap and has to be evidenced from the vault |
| `environmental_impact` | `carbon_records` exist for the model (`exa finops carbon`) | — |

`access_enforced` and `secrets_managed` read the environment of the process that produces the
report. The report says so, and the sufficiency layer never marks these sections *verified*,
because configuration is not a tamper-evident record. Secret audit events now carry their
tenant on the row as well as in the details.

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
| Map | MAP-2.3 | development_process, data_governance | lineage (A2) + dataset versioning/quality (A1/A5) |
| Measure | MEASURE-2.3 | performance | eval (C2) |
| Measure | MEASURE-2.11 | fairness | subgroup report (C8) |
| Manage | MANAGE-2.2 | monitoring | drift + SLO (C5/C6) |
| Manage | MANAGE-4.1 | record_keeping, changes | audit trail (D4) |
| Govern | GOVERN-2.1 | access_documented, access_enforced | relationship RBAC (D6) |
| Measure | MEASURE-2.7 | integrity, secrets_managed, secrets_rotation | AI-BOM (D3) + secrets (D7) |
| Measure | MEASURE-2.12 | environmental_impact | carbon accounting (FinOps) |
| GenAI | GENAI-3.2 | risk_management | guardrails (D8) + drift |

## Programmatic use

```python
from examlops.governance import load_catalogue, validate_mapping, governance_report, crosswalk

assert validate_mapping() == []          # CI gate
rep = governance_report("default", model="JPCP")
print(rep.summary)                        # {'satisfied': N, 'partial': N, 'gap': N}
print(rep.framework_summary)              # 72 subcategories: satisfied/partial/gap/organisational
for cw in crosswalk():
    print(cw["control"], cw["eu_ai_act"], cw["iso_42001"])
```

## Known limits

- The dashboard's governance page (`GET /api/v1/governance/overview`, `posture`) still derives
  its own four-control approximation. It does not read this catalogue yet.
- The report checks that evidence exists and that its records are intact. It does not check
  that the evidence is adequate: an owner relation shows that someone is accountable, not that
  they are the right person.

## See also

- [EU AI Act compliance (D1)](eu-ai-act-compliance.md) — the crosswalk target and shared evidence layer.
- Evidence sources: [Fairness (C8)](fairness.md), [SLOs (C6)](slos.md), [Advanced drift (C5)](drift-advanced.md), [Supply-chain security (D3)](supply-chain-security.md), [Lineage (A2)](lineage.md), [Guardrails (D8)](guardrails.md).
