"""Next-Gen 40 · D1 — EU AI Act compliance tooling (ADR 0012).

Maps ExaMLOps metadata to EU AI Act obligations: per-system risk classification, an
auto-generated Annex-IV technical file assembled from live artifacts, Art. 12 logging
conformance, and a conformity state machine. Built on a reusable
**control → article → evidence** framework (`FRAMEWORK`) shared with D2 (NIST AI RMF).

**Disclaimer:** this assembles compliance *evidence*; it is **not legal advice or
certification**. Every generated document and UI surface shows `DISCLAIMER`.

Graceful degradation: evidence collectors pull from `platform_db` and other Next-Gen
modules when present and **flag** anything missing (never silently omit, R4). Nothing here
requires an external service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from examlops import data as platform_db

DISCLAIMER = (
    "DISCLAIMER: This document assembles compliance evidence from platform metadata. "
    "It is NOT legal advice, a conformity assessment, or CE certification. Consult a "
    "qualified authority before making any EU AI Act declaration."
)

RISK_TIERS = ("prohibited", "high", "limited", "minimal")

# Conformity state machine: valid forward transitions (R8).
CONFORMITY_STATES = ("draft", "documented", "assessed", "declared")
_VALID_TRANSITIONS = {
    "draft": {"documented"},
    "documented": {"assessed", "draft"},
    "assessed": {"declared", "documented"},
    "declared": {"assessed"},  # allow re-opening for a new version
}

# Art. 12 record-keeping: event types that MUST be in the immutable audit trail (R7).
ART12_REQUIRED_EVENTS = (
    "retrain_triggered",
    "drift_auto_retrain_triggered",
    "promotion",
    "eval_gate_override",
    "approval",
)


@dataclass
class Section:
    key: str
    title: str
    annex_iv: str  # Annex-IV clause reference
    content: str
    present: bool  # False => evidence gap (flagged, R4)


@dataclass
class Document:
    model: str
    tenant: str
    sections: list[Section] = field(default_factory=list)
    gaps: int = 0

    def to_markdown(self) -> str:
        lines = [
            f"# EU AI Act Technical File (Annex IV) — {self.model}",
            "",
            f"> {DISCLAIMER}",
            "",
            f"- **System:** {self.model}",
            f"- **Tenant:** {self.tenant}",
            f"- **Evidence gaps:** {self.gaps}",
            "",
        ]
        for s in self.sections:
            flag = "" if s.present else "  ⚠️ **MISSING EVIDENCE**"
            lines.append(f"## {s.title} ({s.annex_iv}){flag}")
            lines.append("")
            lines.append(s.content)
            lines.append("")
        return "\n".join(lines)


# The reusable control→article→evidence mapping (R9). D2 reuses the same shape with a
# different `article` vocabulary (NIST RMF functions).
FRAMEWORK: list[dict[str, str]] = [
    {"control": "system_description", "article": "Annex IV §1", "evidence": "model_card"},
    {"control": "development_process", "article": "Annex IV §2(b)", "evidence": "lineage"},
    {
        "control": "data_governance",
        "article": "Annex IV §2(d)",
        "evidence": "data_versioning+quality",
    },
    {"control": "performance", "article": "Annex IV §3", "evidence": "eval"},
    {"control": "fairness", "article": "Annex IV §3", "evidence": "fairness_report"},
    {"control": "risk_management", "article": "Annex IV §5", "evidence": "guardrails+drift"},
    {"control": "integrity", "article": "Annex IV §2(e)", "evidence": "ai_bom+signature"},
    {"control": "monitoring", "article": "Annex IV §8", "evidence": "drift+slo"},
    {"control": "record_keeping", "article": "Art. 12", "evidence": "audit_trail"},
    {"control": "changes", "article": "Annex IV §6", "evidence": "change_log"},
]


def classify_system(
    model: str,
    risk_tier: str,
    intended_purpose: str,
    deployment_context: str,
    actor: str,
    *,
    tenant: str = "default",
    source: str = "cli",
) -> None:
    """Record a system's risk classification + intended purpose (R1).

    ``source`` attributes the audit event to the calling surface (``"cli"`` by default; the
    dashboard passes ``"dashboard"``) so the same shared code path serves every face of the platform.
    """
    if risk_tier not in RISK_TIERS:
        raise ValueError(f"risk_tier must be one of {RISK_TIERS}, got {risk_tier!r}")
    platform_db.set_compliance_system(
        model,
        tenant=tenant,
        in_scope=True,
        risk_tier=risk_tier,
        intended_purpose=intended_purpose,
        deployment_context=deployment_context,
        updated_by=actor,
    )
    platform_db.write_audit_event(
        source,
        actor,
        "compliance_classified",
        model,
        {"risk_tier": risk_tier, "intended_purpose": intended_purpose},
    )


def promotion_blocked_reason(model: str) -> str | None:
    """R2/GWT-1: in-scope model without a risk tier cannot be promoted."""
    sys = platform_db.get_compliance_system(model)
    if sys and sys["in_scope"] and not sys["risk_tier"]:
        return "in-scope system has no EU AI Act risk classification (exa compliance classify)"
    return None


# --- evidence collectors (each returns (present, content)) -------------------
def _ev_model_card(model: str, tenant: str) -> tuple[bool, str]:
    try:
        from examlops.data.governance import get_compliance_system

        sys = get_compliance_system(model)
        if sys and sys["intended_purpose"]:
            return True, (
                f"Intended purpose: {sys['intended_purpose']}\n\n"
                f"Deployment context: {sys['deployment_context'] or 'n/a'}\n\n"
                f"Risk tier: {sys['risk_tier']}"
            )
    except Exception:
        pass
    return False, "No model card / intended-purpose metadata found (A6)."


def _ev_lineage(model: str, tenant: str) -> tuple[bool, str]:
    try:
        from examlops.data import get_db

        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM lineage_events WHERE model=? OR job LIKE ?",
                (model, f"%{model}%"),
            ).fetchone()
        if row and row["c"]:
            return True, f"{row['c']} lineage event(s) recorded (A2 OpenLineage)."
    except Exception:
        pass
    return False, "No lineage/provenance events found (A2)."


def _ev_eval(model: str, tenant: str) -> tuple[bool, str]:
    try:
        from examlops.data import get_db

        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM eval_suite_results WHERE model=?", (model,)
            ).fetchone()
        if row and row["c"]:
            return True, f"{row['c']} evaluation result(s) recorded (C2)."
    except Exception:
        pass
    return False, "No evaluation results found (C2)."


def _ev_fairness(model: str, tenant: str) -> tuple[bool, str]:
    try:
        from examlops.fairness import fairness_report

        results = fairness_report(model, tenant=tenant)
        if results and any(r.slices for r in results):
            lines = []
            for r in results:
                lines.append(
                    f"- {r.slice_attr}: DP diff={r.demographic_parity_diff}, "
                    f"exceeded={r.disparity_exceeded}"
                )
            return True, "Subgroup fairness (C8):\n" + "\n".join(lines)
    except Exception:
        pass
    return False, "No fairness/subgroup report found (C8)."


def _ev_integrity(model: str, tenant: str) -> tuple[bool, str]:
    try:
        from examlops.data import get_db

        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM model_boms WHERE model=?", (model,)
            ).fetchone()
        if row and row["c"]:
            return True, f"{row['c']} AI-BOM / signature record(s) (D3)."
    except Exception:
        pass
    return False, "No AI-BOM / model signature found (D3)."


def _ev_monitoring(model: str, tenant: str) -> tuple[bool, str]:
    try:
        from examlops.data import get_db

        with get_db() as conn:
            drift = conn.execute(
                "SELECT COUNT(*) AS c FROM drift_events WHERE model=?", (model,)
            ).fetchone()["c"]
            slo = conn.execute(
                "SELECT COUNT(*) AS c FROM slo_specs WHERE model=?", (model,)
            ).fetchone()["c"]
        if drift or slo:
            return True, f"Monitoring: {drift} drift event(s), {slo} SLO spec(s) (C5/C6)."
    except Exception:
        pass
    return False, "No drift/SLO monitoring configured (C5/C6)."


def _ev_record_keeping(model: str, tenant: str) -> tuple[bool, str]:
    cov = check_art12_logging(model)
    covered = [k for k, v in cov["coverage"].items() if v]
    if covered:
        return True, (
            f"Art. 12 audit coverage: {len(covered)}/{len(cov['coverage'])} event types present. "
            f"Uncovered: {', '.join(cov['uncovered']) or 'none'}."
        )
    return False, "No Art. 12 audit-trail coverage found (D4)."


def _ev_data_governance(model: str, tenant: str) -> tuple[bool, str]:
    try:
        from examlops.data import get_db

        with get_db() as conn:
            revs = conn.execute("SELECT COUNT(*) AS c FROM dataset_revisions").fetchone()["c"]
            checks = conn.execute("SELECT COUNT(*) AS c FROM data_quality_checks").fetchone()["c"]
        if revs or checks:
            return (
                True,
                f"Data governance: {revs} dataset revision(s), {checks} quality check(s) (A1/A5).",
            )
    except Exception:
        pass
    return False, "No dataset versioning / quality checks found (A1/A5)."


def _ev_risk_management(model: str, tenant: str) -> tuple[bool, str]:
    try:
        from examlops.data import get_db

        with get_db() as conn:
            guard = conn.execute("SELECT COUNT(*) AS c FROM guardrail_events").fetchone()["c"]
            drift = conn.execute(
                "SELECT COUNT(*) AS c FROM drift_events WHERE model=?", (model,)
            ).fetchone()["c"]
        if guard or drift:
            return (
                True,
                f"Risk management: {guard} guardrail event(s), {drift} drift event(s) (D8/C5).",
            )
    except Exception:
        pass
    return False, "No guardrail / drift risk controls found (D8/C5)."


def _ev_changes(model: str, tenant: str) -> tuple[bool, str]:
    try:
        with platform_db.get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE target=?", (model,)
            ).fetchone()
        if row and row["c"]:
            return True, f"Change log: {row['c']} audited change(s) for {model} (D4)."
    except Exception:
        pass
    return False, "No change-log / audited changes found (D4)."


_COLLECTORS = {
    "system_description": _ev_model_card,
    "development_process": _ev_lineage,
    "data_governance": _ev_data_governance,
    "performance": _ev_eval,
    "fairness": _ev_fairness,
    "risk_management": _ev_risk_management,
    "integrity": _ev_integrity,
    "monitoring": _ev_monitoring,
    "record_keeping": _ev_record_keeping,
    "changes": _ev_changes,
}


def generate_technical_file(model: str, *, tenant: str = "default") -> Document:
    """Assemble the Annex-IV technical file from live evidence, flagging gaps (R3/R4)."""
    doc = Document(model=model, tenant=tenant)
    for entry in FRAMEWORK:
        control = entry["control"]
        collector = _COLLECTORS.get(control)
        if collector is None:
            present, content = False, f"Section '{control}' not yet automated — evidence pending."
        else:
            present, content = collector(model, tenant)
        doc.sections.append(
            Section(
                key=control,
                title=control.replace("_", " ").title(),
                annex_iv=entry["article"],
                content=content,
                present=present,
            )
        )
    doc.gaps = sum(1 for s in doc.sections if not s.present)
    return doc


#: Annex V requires eight items. Five are statements only the **provider** can make — the
#: platform holds no legal entity, no signatory and no notified-body relationship — so they are
#: inputs, never defaults. Inventing a provider name or a standards reference would produce a
#: document that looks signed and says something untrue, which is worse than an empty field.
PROVIDER_FIELDS = ("provider", "provider_address", "signatory", "signatory_function")

_TODO = "⚠️ **TO BE COMPLETED BY THE PROVIDER**"


@dataclass
class Declaration:
    """An Annex-V EU Declaration of Conformity assembled from live metadata (clause 4).

    ``draft`` is the important field. A declaration is **final only** when the conformity state
    machine has reached ``declared``, every provider-supplied field is present, and the Annex-IV
    technical file it references has no evidence gaps. Anything else is stamped DRAFT with its
    reasons listed on the face of the document — a Declaration of Conformity that looks final
    while resting on an incomplete technical file is exactly the artifact clause 5's disclaimer
    exists to prevent.
    """

    model: str
    tenant: str
    risk_tier: str | None = None
    conformity_state: str = "draft"
    technical_file_version: int | None = None
    technical_file_gaps: int | None = None
    audit_chain_head: str | None = None
    provider: str | None = None
    provider_address: str | None = None
    signatory: str | None = None
    signatory_function: str | None = None
    standards: list[str] = field(default_factory=list)
    notified_body: str | None = None
    processes_personal_data: bool = False
    issued_at: str = ""
    blockers: list[str] = field(default_factory=list)

    @property
    def draft(self) -> bool:
        return bool(self.blockers)

    def _or_todo(self, value: str | None) -> str:
        return value if value else _TODO

    def to_markdown(self) -> str:
        status = "DRAFT — NOT A DECLARATION" if self.draft else "FINAL"
        lines = [
            f"# EU Declaration of Conformity (Annex V) — {self.model}",
            "",
            f"**Status: {status}**",
            "",
            f"> {DISCLAIMER}",
            "",
        ]
        if self.blockers:
            lines += ["## Why this is a draft", ""]
            lines += [f"- {b}" for b in self.blockers]
            lines.append("")
        lines += [
            "## 1. AI system identification",
            "",
            f"- **System:** {self.model}",
            f"- **Tenant:** {self.tenant}",
            f"- **Risk tier:** {self.risk_tier or _TODO}",
            f"- **Conformity state:** {self.conformity_state}",
            "- **Traceability:** Annex-IV technical file "
            f"v{self.technical_file_version if self.technical_file_version else _TODO}"
            + (f" ({self.technical_file_gaps} evidence gap(s))" if self.technical_file_gaps else "")
            + (f"; audit-trail head `{self.audit_chain_head}`" if self.audit_chain_head else ""),
            "",
            "## 2. Provider",
            "",
            f"- **Name:** {self._or_todo(self.provider)}",
            f"- **Address:** {self._or_todo(self.provider_address)}",
            "",
            "## 3. Responsibility",
            "",
            "This declaration is issued under the sole responsibility of the provider named above.",
            "",
            "## 4. Conformity statement",
            "",
            "The provider declares that the AI system identified above is in conformity with "
            "Regulation (EU) 2024/1689 and, where applicable, with other relevant Union law "
            "providing for this declaration.",
            "",
            "## 5. Personal data",
            "",
            (
                "The provider declares that this AI system complies with Regulations (EU) "
                "2016/679 and (EU) 2018/1725 and Directive (EU) 2016/680."
                if self.processes_personal_data
                else "The provider has recorded that this AI system does not process personal "
                "data; no statement under Annex V(5) is made."
            ),
            "",
            "## 6. Standards and common specifications",
            "",
        ]
        lines += [f"- {s}" for s in self.standards] or [_TODO]
        lines += [
            "",
            "## 7. Notified body",
            "",
            self.notified_body or "Not applicable — no notified body involvement recorded.",
            "",
            "## 8. Signature",
            "",
            f"- **Place and date of issue:** {self.issued_at or _TODO}",
            f"- **Name:** {self._or_todo(self.signatory)}",
            f"- **Function:** {self._or_todo(self.signatory_function)}",
            "- **Signed for, or on behalf of:** the provider named in section 2",
            "- **Signature:** ______________________",
            "",
        ]
        return "\n".join(lines)


def generate_declaration(
    model: str,
    *,
    tenant: str = "default",
    issued_at: str,
    provider: str | None = None,
    provider_address: str | None = None,
    signatory: str | None = None,
    signatory_function: str | None = None,
    standards: list[str] | None = None,
    notified_body: str | None = None,
    processes_personal_data: bool = False,
) -> Declaration:
    """Assemble the Annex-V Declaration of Conformity for one system (clause 4).

    Everything the platform can know is read from live metadata — risk tier, conformity state,
    the technical file it references and its gap count, the audit-chain head that makes the
    claim traceable. Everything only the provider can state is passed in and left as an explicit
    placeholder when absent.

    ``issued_at`` is required rather than defaulted to "now": the issue date of a declaration is
    a legal fact about when a person signed, not about when a generator ran.
    """
    system = platform_db.get_compliance_system(model) or {}
    files = platform_db.list_technical_files(model)
    latest = files[0] if files else None

    doc = Declaration(
        model=model,
        tenant=tenant,
        risk_tier=system.get("risk_tier"),
        conformity_state=system.get("conformity_state", "draft"),
        technical_file_version=latest["version"] if latest else None,
        technical_file_gaps=latest["gaps"] if latest else None,
        audit_chain_head=_audit_head(),
        provider=provider,
        provider_address=provider_address,
        signatory=signatory,
        signatory_function=signatory_function,
        standards=list(standards or []),
        notified_body=notified_body,
        processes_personal_data=processes_personal_data,
        issued_at=issued_at,
    )
    doc.blockers = _declaration_blockers(doc, latest)
    return doc


def _declaration_blockers(doc: Declaration, latest: dict[str, Any] | None) -> list[str]:
    """Every reason this declaration is not final. Listed, never silently applied."""
    blockers: list[str] = []
    if doc.conformity_state != "declared":
        blockers.append(
            f"conformity state is '{doc.conformity_state}', not 'declared' "
            f"(advance it with `exa compliance declare {doc.model} --state …`)"
        )
    missing = [f for f in PROVIDER_FIELDS if not getattr(doc, f)]
    if missing:
        blockers.append("provider-supplied field(s) missing: " + ", ".join(missing))
    if latest is None:
        blockers.append(
            "no Annex-IV technical file has been generated "
            f"(`exa compliance technical-file {doc.model}`)"
        )
    elif latest["gaps"]:
        blockers.append(
            f"the referenced technical file (v{latest['version']}) has "
            f"{latest['gaps']} evidence gap(s)"
        )
    return blockers


def _audit_head() -> str | None:
    """The audit chain head hash, so the declaration points at a verifiable record."""
    try:
        from examlops.data.audit import audit_chain_head

        head = audit_chain_head()
        return str(head["hash"])[:16] if head and head.get("hash") else None
    except Exception:  # noqa: BLE001 - traceability is best-effort, the document is not
        return None


def check_art12_logging(model: str) -> dict[str, Any]:
    """Verify Art. 12 record-keeping coverage in the immutable audit trail (R7)."""
    coverage: dict[str, bool] = {}
    with platform_db.get_db() as conn:
        for event in ART12_REQUIRED_EVENTS:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE action LIKE ?",
                (f"%{event}%",),
            ).fetchone()
            coverage[event] = bool(row and row["c"])
        total = conn.execute("SELECT COUNT(*) AS c FROM audit_events").fetchone()["c"]
    uncovered = [k for k, v in coverage.items() if not v]
    return {
        "model": model,
        "total_events": int(total),
        "coverage": coverage,
        "uncovered": uncovered,
        "coverage_pct": ((len(coverage) - len(uncovered)) / len(coverage) if coverage else 0.0),
    }


def set_conformity_state(
    model: str, state: str, actor: str, *, tenant: str = "default", source: str = "cli"
) -> None:
    """Advance the conformity state machine with transition validation (R8, GWT-5).

    ``source`` attributes the audit event to the calling surface (``"cli"`` default; ``"dashboard"``
    from the UI) — one shared, transition-validated code path across surfaces.
    """
    if state not in CONFORMITY_STATES:
        raise ValueError(f"state must be one of {CONFORMITY_STATES}, got {state!r}")
    sys = platform_db.get_compliance_system(model)
    current = sys["conformity_state"] if sys else "draft"
    if state != current and state not in _VALID_TRANSITIONS.get(current, set()):
        raise ValueError(
            f"invalid conformity transition {current!r} → {state!r}; "
            f"allowed: {sorted(_VALID_TRANSITIONS.get(current, set()))}"
        )
    platform_db.set_compliance_system(
        model, tenant=tenant, conformity_state=state, updated_by=actor
    )
    platform_db.write_audit_event(
        source, actor, "compliance_conformity", model, {"from": current, "to": state}
    )


__all__ = [
    "DISCLAIMER",
    "RISK_TIERS",
    "CONFORMITY_STATES",
    "Declaration",
    "PROVIDER_FIELDS",
    "generate_declaration",
    "FRAMEWORK",
    "ART12_REQUIRED_EVENTS",
    "Document",
    "Section",
    "classify_system",
    "promotion_blocked_reason",
    "generate_technical_file",
    "check_art12_logging",
    "set_conformity_state",
]
