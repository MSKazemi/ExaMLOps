"""Next-Gen 40 · D2 — NIST AI RMF control backbone (ADR 0027).

A versioned NIST AI RMF control catalogue (`catalogue.yaml`: Govern / Map / Measure /
Manage + GenAI Profile), a CI-validated feature→control mapping, and an auto-generated
evidence-coverage report crosswalked to the EU AI Act (D1) and ISO/IEC 42001.

The evidence layer is **shared with D1**: each control's ``evidence`` keys resolve to the
same collectors in `examlops.compliance`, so one evidence pass serves both frameworks. A
control is *satisfied* when all its evidence is present, *partial* when some is, and a
*gap* when none is — a missing source is always a gap, never a false pass (R4).

This is **evidence coverage, not certification**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from examlops import data as platform_db

_CATALOGUE_PATH = Path(__file__).parent / "catalogue.yaml"

COVERAGE_DISCLAIMER = (
    "This is an AI-governance EVIDENCE COVERAGE report (NIST AI RMF), not a certification "
    "or conformity assessment."
)


@dataclass
class Control:
    id: str
    function: str
    description: str
    evidence: list[str]
    eu_ai_act: str | None = None
    iso_42001: str | None = None


@dataclass
class ControlCoverage:
    control: Control
    status: str  # satisfied | partial | gap
    present_evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    # ADR 0110 decision 6: evidence that exists but whose integrity check failed or could not
    # run. It does NOT count toward `satisfied` — a control satisfied by records from a broken
    # audit chain is the confident partial report the decision forbids.
    insufficient_evidence: list[str] = field(default_factory=list)
    evidence_notes: list[str] = field(default_factory=list)


@dataclass
class CoverageReport:
    tenant: str
    version: str
    model: str | None
    controls: list[ControlCoverage] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        out = {"satisfied": 0, "partial": 0, "gap": 0}
        for c in self.controls:
            out[c.status] += 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "catalogue_version": self.version,
            "model": self.model,
            "disclaimer": COVERAGE_DISCLAIMER,
            "summary": self.summary,
            "controls": [
                {
                    "id": c.control.id,
                    "function": c.control.function,
                    "status": c.status,
                    "eu_ai_act": c.control.eu_ai_act,
                    "iso_42001": c.control.iso_42001,
                    "present_evidence": c.present_evidence,
                    "missing_evidence": c.missing_evidence,
                    "insufficient_evidence": c.insufficient_evidence,
                    "evidence_notes": c.evidence_notes,
                }
                for c in self.controls
            ],
        }


def _load_raw() -> dict[str, Any]:
    import yaml

    with open(_CATALOGUE_PATH) as fh:
        return yaml.safe_load(fh)


def catalogue_version() -> str:
    return _load_raw().get("version", "0")


def load_catalogue() -> list[Control]:
    """Load the versioned NIST AI RMF catalogue (R1, GWT-1)."""
    raw = _load_raw()
    controls = []
    for c in raw.get("controls", []):
        controls.append(
            Control(
                id=c["id"],
                function=c["function"],
                description=c["description"],
                evidence=list(c.get("evidence", [])),
                eu_ai_act=c.get("eu_ai_act"),
                iso_42001=c.get("iso_42001"),
            )
        )
    return controls


@dataclass
class MappingError:
    control_id: str
    problem: str


def validate_mapping() -> list[MappingError]:
    """CI check: every control references known functions + real evidence collectors (R2, GWT-2)."""
    from examlops.compliance import _COLLECTORS

    valid_functions = {"Govern", "Map", "Measure", "Manage"}
    errors: list[MappingError] = []
    try:
        controls = load_catalogue()
    except Exception as e:  # malformed YAML => a single hard error
        return [MappingError("<catalogue>", f"failed to load: {e}")]
    if not controls:
        errors.append(MappingError("<catalogue>", "catalogue is empty"))
    seen_ids: set[str] = set()
    for c in controls:
        if c.id in seen_ids:
            errors.append(MappingError(c.id, "duplicate control id"))
        seen_ids.add(c.id)
        if c.function not in valid_functions:
            errors.append(MappingError(c.id, f"unknown function {c.function!r}"))
        if not c.evidence:
            errors.append(MappingError(c.id, "no evidence mapped"))
        for ev in c.evidence:
            if ev not in _COLLECTORS:
                errors.append(
                    MappingError(c.id, f"evidence {ev!r} has no collector in examlops.compliance")
                )
    return errors


def governance_report(
    tenant: str = "default", *, model: str | None = None, persist_by: str | None = None
) -> CoverageReport:
    """Collect live evidence per control and mark satisfied/partial/gap (R3/R4).

    Evidence is collected against ``model`` when given; otherwise a control is satisfied
    if *any* in-scope model provides its evidence (fleet-level posture).
    """
    from examlops.compliance import _COLLECTORS
    from examlops.compliance.sufficiency import INSUFFICIENT, assess_section, integrity_state

    state = integrity_state()  # verified once for the whole report, not once per control
    controls = load_catalogue()
    models = (
        [model]
        if model
        else [s["model"] for s in platform_db.list_compliance_systems(tenant=tenant)]
    )

    report = CoverageReport(tenant=tenant, version=catalogue_version(), model=model)
    for control in controls:
        present: list[str] = []
        missing: list[str] = []
        insufficient: list[str] = []
        notes: list[str] = []
        for ev in control.evidence:
            collector = _COLLECTORS.get(ev)
            ok = False
            if collector is not None:
                for m in models or []:
                    try:
                        if collector(m, tenant)[0]:
                            ok = True
                            break
                    except Exception:
                        continue
            if not ok:
                missing.append(ev)
                continue
            verdict = assess_section(ev, True, state)
            if verdict.status == INSUFFICIENT:
                insufficient.append(ev)
            else:
                present.append(ev)
            notes.extend(r for r in verdict.reasons if r not in notes)
        if not present:
            status = "gap"
        elif missing or insufficient:
            status = "partial"
        else:
            status = "satisfied"
        report.controls.append(
            ControlCoverage(
                control,
                status,
                present_evidence=present,
                missing_evidence=missing,
                insufficient_evidence=insufficient,
                evidence_notes=notes,
            )
        )

    if persist_by is not None:
        summary = report.summary
        platform_db.write_audit_event(
            "cli",
            persist_by,
            "governance_report",
            model or "<fleet>",
            {"catalogue_version": report.version, **summary},
        )
    return report


def crosswalk() -> list[dict[str, Any]]:
    """Control → EU AI Act article + ISO/IEC 42001 crosswalk (R5, GWT-5)."""
    return [
        {"control": c.id, "eu_ai_act": c.eu_ai_act, "iso_42001": c.iso_42001}
        for c in load_catalogue()
    ]


__all__ = [
    "COVERAGE_DISCLAIMER",
    "Control",
    "ControlCoverage",
    "CoverageReport",
    "MappingError",
    "load_catalogue",
    "catalogue_version",
    "validate_mapping",
    "governance_report",
    "crosswalk",
]
