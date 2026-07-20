# Dataset & Model Cards (A6)

> Next-Gen 40 · feature **A6** · ADR 0037 · spec `design/vision/specs/A6-croissant-model-cards.md`

A6 produces two machine-readable governance artifacts, both **auto-populated from live
platform data** and feeding the D1 EU AI Act technical file and D2 NIST RMF evidence:

- **Croissant** dataset cards — standardized JSON-LD dataset metadata.
- **Structured model cards** — governance cards with intended use, risk class, metrics,
  fairness, and lineage, where any missing field renders as **"not provided"** — never
  fabricated.

## Croissant dataset cards

```bash
exa cards dataset FData
exa cards dataset FData --revision <rev> --license CC-BY-4.0 --out fdata_croissant.json
```

The record maps the **real FData columns** (`pclass`, `mbwidth`, `embedding`) into a
Croissant `recordSet`, with distribution, license, and provenance. It is validated against
the pinned Croissant spec version before being saved — `exa cards dataset` exits non-zero
if validation fails.

## Structured model cards

```bash
exa cards model JPCP
exa cards model JPCP --out jpcp_card.md
exa --json cards model JPCP
```

The card is auto-populated from platform data:

| Field | Source |
|---|---|
| Intended use | D1 compliance (`intended_purpose`) |
| Risk class | D1 compliance (`risk_tier`) |
| Dataset revision | A1 dataset versioning |
| Metrics | C2 eval results |
| Fairness | C8 subgroup report |
| Lineage | A2 lineage events |
| Limitations | manual / not provided |

**Nothing is fabricated (R4):** a field with no backing data is rendered literally as
`not provided`. Cards are versioned and persisted (`model_card_records`).

## Completeness gate

```bash
exa cards completeness JPCP
exa cards completeness JPCP --require 0.7    # exit 1 if below 70%
```

`card_completeness` scores 0..1 by how many card fields are populated. A D5 policy (or a
CI step) can require a minimum completeness before promotion (C3), so incomplete models
can't ship silently.

## Programmatic use

```python
from examlops.cards import croissant_record, validate_croissant, build_model_card, card_completeness

rec = croissant_record("FData", revision="abc123")
assert validate_croissant(rec) == []

card = build_model_card("JPCP")
print(card.to_markdown())          # 'not provided' for any missing field
print(card.completeness)           # 0..1

if card_completeness("JPCP") < 0.7:
    ...  # block promotion
```

## Governance

- Cards are **versioned** and **audited** (D4) and feed **D1** (Annex-IV system
  description) + **D2** (NIST RMF `system_description` evidence).
- Publishing is opt-in and applies the naming-scrub path, excluding PII/internal fields.

## See also

- [EU AI Act (D1)](eu-ai-act-compliance.md) / [NIST AI RMF (D2)](governance-nist-rmf.md) — consumers.
- [Fairness (C8)](fairness.md), [Lineage (A2)](lineage.md) — card data sources.
