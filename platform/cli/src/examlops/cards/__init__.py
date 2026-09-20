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

# The platform knows no concrete dataset's columns (ADR 0094) — a dataset's field schema is
# use-case content, supplied by the caller (from the pack's datasets/schemas.json). When none is
# provided we emit a single explicit "not provided" field rather than fabricating columns (R4).
_PLACEHOLDER_FIELDS = [{"name": "record", "dataType": "sc:Text", "description": NOT_PROVIDED}]

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
    dataset: str,
    *,
    revision: str | None = None,
    license: str = "CC-BY-4.0",
    schema: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a Croissant JSON-LD record for a dataset version (R1).

    ``schema`` is the dataset's field list (``[{name, dataType, description}, …]``), supplied by
    the use-case pack. When omitted, it is resolved from the active pack's
    ``datasets/schemas.json`` (via ``examlops.usecase.dataset_schema``); if the pack declares no
    schema for the dataset, an explicit "not provided" placeholder field is used — the platform
    reads a concrete dataset's columns from the pack, never fabricates them (ADR 0094 / R4).
    """
    if schema is None:
        from examlops.usecase import dataset_schema

        schema = dataset_schema(dataset)
    source = schema if schema else _PLACEHOLDER_FIELDS
    fields = [
        {
            "@type": "cr:Field",
            "@id": f"{dataset}/{f['name']}",
            "name": f["name"],
            "dataType": f.get("dataType", "sc:Text"),
            "description": f.get("description", NOT_PROVIDED),
        }
        for f in source
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


# ── Publishing (ADR 0037 clause 4) ────────────────────────────────────────────
#
# A card is generated from live platform data, so it inherits whatever that data contains:
# tenant identifiers, absolute paths from a deployment, a stray address in a free-text
# limitation. Publishing one is the moment those leave the building, and the repo's own rule is
# that secrets, personal data, local paths and site-specific infrastructure never do.

#: Fields removed outright. ``tenant`` is a D6 identity — on a shared platform it names which
#: customer the card belongs to, which is not a property of the model at all.
INTERNAL_FIELDS = ("tenant",)

#: Site-specific detail that is not a secret but is nobody's business outside the deployment.
#: Deliberately general patterns rather than a list of this deployment's hostnames: a denylist of
#: known-internal names silently passes the one nobody wrote down.
_LOCATION_PATTERNS: list[tuple[str, Any]] = []


def _location_patterns() -> list[tuple[str, Any]]:
    global _LOCATION_PATTERNS
    if not _LOCATION_PATTERNS:
        import re as _re

        _LOCATION_PATTERNS = [
            # RFC1918 + loopback, with or without a port.
            (
                "private-address",
                _re.compile(
                    r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|127\.0\.0)"
                    r"\.\d{1,3}(?:\.\d{1,3})?(?::\d+)?\b"
                ),
            ),
            ("localhost", _re.compile(r"\blocalhost(?::\d+)?\b")),
            # An absolute POSIX path of two or more segments — a deployment's filesystem layout.
            ("filesystem-path", _re.compile(r"(?<![\w/])/(?:[\w.\-]+/){1,}[\w.\-]*")),
            ("ssh-target", _re.compile(r"\b[\w.\-]+@[\w.\-]+\b(?!\.[a-z]{2,})")),
        ]
    return _LOCATION_PATTERNS


REDACTED = "[REDACTED]"


@dataclass
class CardExport:
    """The publishable form of a card, plus everything that was removed to get there."""

    content: dict[str, Any]
    removed_fields: list[str] = field(default_factory=list)
    redactions: list[str] = field(default_factory=list)
    secret_findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        """Whether this may be published without an explicit override."""
        return not self.secret_findings


def _scrub_text(text: str) -> tuple[str, list[str]]:
    """Redact PII (D8) and site-specific locations from one string."""
    from examlops.guardrails import redact_pii

    scrubbed, found = redact_pii(text)
    kinds = list(found)
    for name, pattern in _location_patterns():
        if pattern.search(scrubbed):
            scrubbed = pattern.sub(REDACTED, scrubbed)
            kinds.append(name)
    return scrubbed, kinds


def _scrub(value: Any, redactions: list[str], path: str = "") -> Any:
    """Walk a card recursively, scrubbing every string it contains.

    Recursive on purpose: a card's ``metrics`` and ``fairness`` fields are nested mappings and a
    top-level-only pass would publish anything one level down. Keys are scrubbed as well as
    values — a slice value can itself be a person's name.
    """
    if isinstance(value, str):
        scrubbed, kinds = _scrub_text(value)
        redactions.extend(f"{path or 'card'}: {k}" for k in kinds)
        return scrubbed
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            new_key = _scrub(k, redactions, f"{path}.{k}" if path else str(k))
            out[new_key] = _scrub(v, redactions, f"{path}.{k}" if path else str(k))
        return out
    if isinstance(value, list):
        return [_scrub(v, redactions, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def export_card(card: dict[str, Any]) -> CardExport:
    """Scrub a card for publication (clause 4). Never raises; the caller decides.

    Three different treatments, because the three findings are not alike:

    * **Internal fields are dropped.** They are structurally not publishable, so there is nothing
      to decide per export.
    * **PII and site-specific locations are redacted.** That is D8's designed behaviour for
      content, and the card is still useful with a name replaced by a placeholder.
    * **A secret is reported, and blocks.** Redacting it would hide the fact that a credential
      reached a generated artifact at all, which is a problem upstream that publishing quietly
      would bury. The finding never carries the matched value.
    """
    from examlops.secrets import scan_text

    content = {k: v for k, v in card.items() if k not in INTERNAL_FIELDS}
    removed = [k for k in card if k in INTERNAL_FIELDS]
    redactions: list[str] = []
    content = _scrub(content, redactions)

    import json as _json

    findings = scan_text(_json.dumps(content, indent=2, default=str))
    return CardExport(
        content=content,
        removed_fields=removed,
        redactions=sorted(set(redactions)),
        secret_findings=findings,
    )


def card_completeness(model: str, *, tenant: str = "default") -> float:
    """Completeness score 0..1 for a model's card — used by the D5/C3 gate (R6)."""
    return build_model_card(model, tenant=tenant).completeness


def lint_datasheet(record: dict[str, Any]) -> list[str]:
    """Lint a dataset card (the platform's datasheet artifact) for missing documentation.

    Returns findings; empty means it passes. It fails on everything :func:`validate_croissant`
    rejects **plus** the gaps that spec check lets through and a datasheet must not have: fields
    whose description is the explicit "not provided" placeholder (the dataset's composition is
    undocumented), an unpinned version (no provenance — which revision is this?), and a missing
    provenance revision. Only what the artifact actually carries is checked — the seven Gebru
    et al. section questionnaire is not modelled by this record.
    """
    findings = list(validate_croissant(record))
    for rs in record.get("recordSet", []) or []:
        for f in rs.get("field", []) or []:
            if f.get("description") in (None, "", NOT_PROVIDED):
                findings.append(f"field {f.get('name', '?')!r} has no description (composition)")
    if record.get("version") in (None, "", "latest"):
        findings.append("version is unpinned ('latest') — pin a dataset revision (provenance)")
    prov = record.get("provenance") or {}
    if prov.get("dataset_revision") in (None, "", NOT_PROVIDED):
        findings.append("provenance.dataset_revision is not provided")
    return findings


__all__ = [
    "CROISSANT_CONTEXT",
    "INTERNAL_FIELDS",
    "NOT_PROVIDED",
    "CardExport",
    "ModelCard",
    "export_card",
    "croissant_record",
    "validate_croissant",
    "lint_datasheet",
    "build_model_card",
    "card_completeness",
]
