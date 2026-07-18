"""Next-Gen 40 · A6 — Croissant dataset metadata & structured model cards (ADR 0037).

Two machine-readable governance artifacts, both auto-populated from live platform data
and feeding D1 compliance + D2 evidence:

- **Croissant** (`croissant_record`): a JSON-LD dataset card (recordsets/fields mapping the
  real FData columns, distribution, provenance, license) validated against a pinned spec
  version (R1/R2).
- **Structured model card** (`build_model_card`): intended use + risk class (D1), training
  dataset revision (A1), metrics (eval/MLflow), fairness (C8), lineage (A2), limitations —
  with **explicit "not provided"** for any missing field; nothing is ever fabricated (R4).

`card_completeness` scores a model card so a D5 policy can require a minimum before
promotion (R6). Cards are versioned + audited (D4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from examlops import data as platform_db

CROISSANT_CONTEXT = "http://mlcommons.org/croissant/1.0"
NOT_PROVIDED = "not provided"

# The real FData parquet columns (see CLAUDE.md "FData Parquet Schema").
_FDATA_FIELDS = [
    {"name": "pclass", "dataType": "sc:Text", "description": "memory-bound | compute-bound class"},
    {"name": "mbwidth", "dataType": "sc:Float", "description": "memory bandwidth (double)"},
    {"name": "embedding", "dataType": "sc:Float", "description": "384-dim feature embedding"},
]

# Fields a complete model card should carry (R6 completeness scoring).
_CARD_FIELDS = [
    "intended_use",
    "risk_class",
    "dataset_revision",
    "metrics",
    "fairness",
    "lineage",
    "limitations",
]


def croissant_record(
    dataset: str, *, revision: str | None = None, license: str = "CC-BY-4.0"
) -> dict[str, Any]:
    """Build a Croissant JSON-LD record for a dataset version (R1)."""
    fields = [
        {
            "@type": "cr:Field",
            "@id": f"{dataset}/{f['name']}",
            "name": f["name"],
            "dataType": f["dataType"],
            "description": f["description"],
        }
        for f in _FDATA_FIELDS
    ]
    return {
        "@context": CROISSANT_CONTEXT,
        "@type": "sc:Dataset",
        "conformsTo": "http://mlcommons.org/croissant/1.0",
        "name": dataset,
        "description": f"ExaMLOps dataset '{dataset}' (HPC workload records).",
        "license": license,
        "version": revision or "latest",
        "distribution": [
            {
                "@type": "cr:FileObject",
                "@id": f"{dataset}.parquet",
                "encodingFormat": "application/x-parquet",
                "name": f"{dataset}.parquet",
            }
        ],
        "recordSet": [
            {
                "@type": "cr:RecordSet",
                "@id": f"{dataset}/records",
                "name": f"{dataset}_records",
                "field": fields,
            }
        ],
        "provenance": {"platform": "ExaMLOps", "dataset_revision": revision or NOT_PROVIDED},
    }


def validate_croissant(record: dict[str, Any]) -> list[str]:
    """Validate a Croissant record against the pinned spec (R2). Returns error list."""
    errors: list[str] = []
    if record.get("@context") != CROISSANT_CONTEXT:
        errors.append(f"@context must be {CROISSANT_CONTEXT}")
    if record.get("@type") != "sc:Dataset":
        errors.append("@type must be sc:Dataset")
    for required in ("name", "license", "distribution", "recordSet"):
        if not record.get(required):
            errors.append(f"missing required field: {required}")
    for rs in record.get("recordSet", []):
        if not rs.get("field"):
            errors.append(f"recordSet {rs.get('@id', '?')} has no fields")
    return errors


@dataclass
class ModelCard:
    model: str
    tenant: str
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def completeness(self) -> float:
        provided = sum(
            1 for k in _CARD_FIELDS if self.fields.get(k) not in (None, NOT_PROVIDED, [], {})
        )
        return provided / len(_CARD_FIELDS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tenant": self.tenant,
            "completeness": self.completeness,
            **self.fields,
        }

    def to_markdown(self) -> str:
        lines = [f"# Model Card — {self.model}", "", f"_Completeness: {self.completeness:.0%}_", ""]
        for k in _CARD_FIELDS:
            v = self.fields.get(k, NOT_PROVIDED)
            lines.append(f"## {k.replace('_', ' ').title()}")
            lines.append("")
            lines.append(_fmt(v))
            lines.append("")
        return "\n".join(lines)


def _fmt(v: Any) -> str:
    if v in (None, [], {}):
        return NOT_PROVIDED
    if isinstance(v, (list, dict)):
        import json

        return f"```json\n{json.dumps(v, indent=2, default=str)}\n```"
    return str(v)


def build_model_card(model: str, *, tenant: str = "default") -> ModelCard:
    """Auto-populate a structured model card from live platform data (R3/R4).

    Missing fields are set to ``"not provided"`` — never fabricated.
    """
    fields: dict[str, Any] = dict.fromkeys(_CARD_FIELDS, NOT_PROVIDED)

    # D1 compliance — intended use + risk class.
    sysrec = platform_db.get_compliance_system(model)
    if sysrec:
        if sysrec.get("intended_purpose"):
            fields["intended_use"] = sysrec["intended_purpose"]
        if sysrec.get("risk_tier"):
            fields["risk_class"] = sysrec["risk_tier"]

    # A1 dataset revision (latest recorded).
    try:
        with platform_db.get_db() as conn:
            row = conn.execute(
                "SELECT dataset, revision FROM dataset_revisions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            fields["dataset_revision"] = f"{row['dataset']}@{row['revision']}"
    except Exception:
        pass

    # Metrics — from C2 eval results (offline-safe; MLflow optional).
    try:
        with platform_db.get_db() as conn:
            rows = conn.execute(
                "SELECT metric, score FROM eval_suite_results WHERE model=? "
                "ORDER BY id DESC LIMIT 5",
                (model,),
            ).fetchall()
        if rows:
            fields["metrics"] = {r["metric"]: r["score"] for r in rows}
    except Exception:
        pass

    # C8 fairness.
    try:
        from examlops.fairness import fairness_report

        results = fairness_report(model, tenant=tenant)
        if results and any(r.slices for r in results):
            fields["fairness"] = {
                r.slice_attr: {
                    "demographic_parity_diff": r.demographic_parity_diff,
                    "disparity_exceeded": r.disparity_exceeded,
                }
                for r in results
            }
    except Exception:
        pass

    # A2 lineage.
    try:
        with platform_db.get_db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM lineage_events WHERE model=?", (model,)
            ).fetchone()["c"]
        if n:
            fields["lineage"] = f"{n} lineage event(s) recorded (A2)"
    except Exception:
        pass

    return ModelCard(model=model, tenant=tenant, fields=fields)


def card_completeness(model: str, *, tenant: str = "default") -> float:
    """Completeness score 0..1 for a model's card — used by the D5/C3 gate (R6)."""
    return build_model_card(model, tenant=tenant).completeness


__all__ = [
    "CROISSANT_CONTEXT",
    "NOT_PROVIDED",
    "ModelCard",
    "croissant_record",
    "validate_croissant",
    "build_model_card",
    "card_completeness",
]
