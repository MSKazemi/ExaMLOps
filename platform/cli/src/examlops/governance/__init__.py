"""Next-Gen 40 · D2 — NIST AI RMF control backbone (ADR 0027).

A versioned NIST AI RMF control catalogue (`catalogue.yaml`: Govern / Map / Measure /
Manage + GenAI Profile), a CI-validated feature→control mapping, and an auto-generated
evidence-coverage report crosswalked to the EU AI Act (D1) and ISO/IEC 42001.

Two layers (catalogue 2.0.0): the **framework** — all 72 AI RMF 1.0 subcategories — and the
**platform controls** that can be evidenced, each naming the subcategories it contributes to. A
subcategory no control reaches is reported as *organisational* (policy, training, stakeholder
evidence the platform does not hold), never as covered. Features declare the controls they serve
and the evidence they emit in `features.yaml` (decision 2), validated in both directions.

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
_FEATURES_PATH = Path(__file__).parent / "features.yaml"

#: AI RMF 1.0 (NIST AI 100-1) subcategory counts per function. The catalogue must encode the
#: framework whole: a truncated or padded list fails validation instead of silently changing the
#: denominator of the framework coverage figure.
NIST_SUBCATEGORY_COUNTS: dict[str, int] = {"Govern": 19, "Map": 18, "Measure": 22, "Manage": 13}
FUNCTIONS = ("Govern", "Map", "Measure", "Manage")

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
    #: Retired ids that still resolve to this control (see :func:`resolve_control`).
    aliases: list[str] = field(default_factory=list)
    profile: str | None = None
    #: The AI RMF subcategories this control contributes evidence to. Defaults to the control's
    #: own id when that id is a subcategory.
    nist_subcategories: list[str] = field(default_factory=list)


@dataclass
class Subcategory:
    id: str
    function: str
    title: str


@dataclass
class Feature:
    """One platform feature's governance declaration (``features.yaml``, decision 2)."""

    id: str
    name: str
    module: str
    emits: list[str]
    controls: list[str]


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
    #: Features (features.yaml) that declare this control — who owns closing it when it is a gap.
    features: list[str] = field(default_factory=list)


@dataclass
class SubcategoryCoverage:
    subcategory: Subcategory
    #: satisfied | partial | gap (rolled up from its controls) | organisational (none maps to it)
    status: str
    controls: list[str] = field(default_factory=list)


_SUB_STATUSES = ("satisfied", "partial", "gap", "organisational")


@dataclass
class CoverageReport:
    tenant: str
    version: str
    model: str | None
    controls: list[ControlCoverage] = field(default_factory=list)
    subcategories: list[SubcategoryCoverage] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        out = {"satisfied": 0, "partial": 0, "gap": 0}
        for c in self.controls:
            out[c.status] += 1
        return out

    @property
    def framework_summary(self) -> dict[str, Any]:
        """Coverage over every AI RMF subcategory — the framework, not only platform controls."""
        counts = dict.fromkeys(_SUB_STATUSES, 0)
        by_function: dict[str, dict[str, int]] = {}
        for sc in self.subcategories:
            counts[sc.status] += 1
            fn = by_function.setdefault(sc.subcategory.function, dict.fromkeys(_SUB_STATUSES, 0))
            fn[sc.status] += 1
        return {
            "subcategories": len(self.subcategories),
            "platform_evidenced": len(self.subcategories) - counts["organisational"],
            **counts,
            "by_function": by_function,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "catalogue_version": self.version,
            "model": self.model,
            "disclaimer": COVERAGE_DISCLAIMER,
            "summary": self.summary,
            "framework": self.framework_summary,
            "controls": [
                {
                    "id": c.control.id,
                    "aliases": c.control.aliases,
                    "function": c.control.function,
                    "status": c.status,
                    "eu_ai_act": c.control.eu_ai_act,
                    "iso_42001": c.control.iso_42001,
                    "nist_subcategories": c.control.nist_subcategories,
                    "features": c.features,
                    "present_evidence": c.present_evidence,
                    "missing_evidence": c.missing_evidence,
                    "insufficient_evidence": c.insufficient_evidence,
                    "evidence_notes": c.evidence_notes,
                }
                for c in self.controls
            ],
            "subcategories": [
                {
                    "id": sc.subcategory.id,
                    "function": sc.subcategory.function,
                    "title": sc.subcategory.title,
                    "status": sc.status,
                    "controls": sc.controls,
                }
                for sc in self.subcategories
            ],
        }


def _load_raw() -> dict[str, Any]:
    import yaml

    with open(_CATALOGUE_PATH) as fh:
        return yaml.safe_load(fh)


def catalogue_version() -> str:
    return _load_raw().get("version", "0")


def load_subcategories() -> list[Subcategory]:
    """The AI RMF 1.0 subcategories encoded in the catalogue (function derived from the id)."""
    out = []
    for sc in _load_raw().get("subcategories") or []:
        sid = str(sc["id"])
        out.append(
            Subcategory(
                id=sid, function=sid.split("-", 1)[0].capitalize(), title=str(sc.get("title", ""))
            )
        )
    return out


def load_catalogue() -> list[Control]:
    """Load the versioned NIST AI RMF catalogue (R1, GWT-1)."""
    raw = _load_raw()
    subcategory_ids = {str(sc["id"]) for sc in raw.get("subcategories") or []}
    controls = []
    for c in raw.get("controls", []):
        nist = [str(x) for x in c.get("nist_subcategories") or []]
        if not nist and c["id"] in subcategory_ids:
            nist = [c["id"]]
        controls.append(
            Control(
                id=c["id"],
                function=c["function"],
                description=c["description"],
                evidence=list(c.get("evidence", [])),
                eu_ai_act=c.get("eu_ai_act"),
                iso_42001=c.get("iso_42001"),
                aliases=[str(a) for a in c.get("aliases") or []],
                profile=c.get("profile"),
                nist_subcategories=nist,
            )
        )
    return controls


def resolve_control(control_id: str) -> Control | None:
    """Look a control up by id or by a retired alias (``GOVERN-4.1`` → ``GOVERN-2.1``)."""
    for c in load_catalogue():
        if control_id == c.id or control_id in c.aliases:
            return c
    return None


def load_features() -> list[Feature]:
    """The per-feature governance declarations (``features.yaml``, ADR 0027 decision 2)."""
    import yaml

    with open(_FEATURES_PATH) as fh:
        raw = yaml.safe_load(fh) or {}
    return [
        Feature(
            id=str(f["id"]),
            name=str(f.get("name", "")),
            module=str(f.get("module", "")),
            emits=[str(e) for e in f.get("emits") or []],
            controls=[str(c) for c in f.get("controls") or []],
        )
        for f in raw.get("features") or []
    ]


@dataclass
class MappingError:
    control_id: str
    problem: str


def validate_mapping() -> list[MappingError]:
    """CI check over the catalogue, the framework layer and the feature declarations (R2, GWT-2).

    Controls must reference known functions and real evidence collectors; the subcategory layer
    must encode AI RMF 1.0 whole (:data:`NIST_SUBCATEGORY_COUNTS`); and ``features.yaml`` must
    agree with both, in each direction (see that file's header).
    """
    from examlops.compliance import _COLLECTORS

    valid_functions = set(FUNCTIONS)
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
    for c in controls:
        for alias in c.aliases:
            if alias in seen_ids:
                errors.append(MappingError(c.id, f"alias {alias!r} is also a control id"))
    errors.extend(_validate_subcategories(controls))
    errors.extend(_validate_features(controls, set(_COLLECTORS)))
    return errors


def _validate_subcategories(controls: list[Control]) -> list[MappingError]:
    try:
        subs = load_subcategories()
    except Exception as e:  # noqa: BLE001
        return [MappingError("<subcategories>", f"failed to load: {e}")]
    errors: list[MappingError] = []
    ids = [s.id for s in subs]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(MappingError(dup, "duplicate subcategory id"))
    for s in subs:
        if s.function not in NIST_SUBCATEGORY_COUNTS:
            errors.append(MappingError(s.id, f"unknown function prefix in {s.id!r}"))
    for fn, expected in NIST_SUBCATEGORY_COUNTS.items():
        got = len({s.id for s in subs if s.function == fn})
        if got != expected:
            errors.append(
                MappingError(
                    "<subcategories>",
                    f"{fn}: {got} subcategories encoded, AI RMF 1.0 defines {expected}",
                )
            )
    known = set(ids)
    for c in controls:
        if not c.nist_subcategories:
            errors.append(MappingError(c.id, "maps to no AI RMF subcategory"))
        for sid in c.nist_subcategories:
            if sid not in known:
                errors.append(MappingError(c.id, f"unknown AI RMF subcategory {sid!r}"))
    return errors


def _module_resolves(module: str) -> bool:
    import importlib.util

    if not module:
        return False
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _validate_features(controls: list[Control], collectors: set[str]) -> list[MappingError]:
    try:
        features = load_features()
    except FileNotFoundError:
        return [MappingError("<features>", "features.yaml is missing")]
    except Exception as e:  # noqa: BLE001
        return [MappingError("<features>", f"failed to load: {e}")]
    if not features:
        return [MappingError("<features>", "no feature declares any control")]
    by_id = {c.id: c for c in controls}
    errors: list[MappingError] = []
    seen: set[str] = set()
    emitted: set[str] = set()
    for f in features:
        tag = f"feature {f.id}"
        if f.id in seen:
            errors.append(MappingError(tag, "duplicate feature id"))
        seen.add(f.id)
        if not _module_resolves(f.module):
            errors.append(MappingError(tag, f"module {f.module!r} does not resolve"))
        if not f.emits:
            errors.append(MappingError(tag, "emits no evidence"))
        for ev in f.emits:
            if ev not in collectors:
                errors.append(MappingError(tag, f"emits {ev!r}, which has no collector"))
        emitted.update(f.emits)
        if not f.controls:
            errors.append(MappingError(tag, "declares no control"))
        for cid in f.controls:
            control = by_id.get(cid)
            if control is None:
                errors.append(MappingError(tag, f"declares unknown control {cid!r}"))
            elif not set(f.emits) & set(control.evidence):
                errors.append(
                    MappingError(
                        tag,
                        f"claims {cid} but emits none of its evidence "
                        f"({', '.join(control.evidence)})",
                    )
                )
    for c in controls:
        for ev in c.evidence:
            if not any(c.id in f.controls and ev in f.emits for f in features):
                errors.append(
                    MappingError(c.id, f"evidence {ev!r} is emitted by no feature declaring it")
                )
    for ev in sorted(collectors - emitted):
        errors.append(MappingError("<features>", f"collector {ev!r} is emitted by no feature"))
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
    try:
        features = load_features()
    except Exception:  # noqa: BLE001 - attribution is context; `validate` reports the file
        features = []
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
                features=[f.id for f in features if control.id in f.controls],
            )
        )
    report.subcategories = _subcategory_coverage(report.controls)

    if persist_by is not None:
        framework = {k: v for k, v in report.framework_summary.items() if k != "by_function"}
        platform_db.write_audit_event(
            "cli",
            persist_by,
            "governance_report",
            model or "<fleet>",
            {
                "catalogue_version": report.version,
                "tenant": tenant,
                **report.summary,
                "framework": framework,
            },
            tenant=tenant,
        )
    return report


def _subcategory_coverage(controls: list[ControlCoverage]) -> list[SubcategoryCoverage]:
    """Roll control statuses up to AI RMF subcategories.

    *satisfied* only when every platform control mapped to the subcategory is; *gap* when none
    of them has evidence; otherwise *partial*. A subcategory no control maps to is
    *organisational* — listed, never counted as covered.
    """
    out: list[SubcategoryCoverage] = []
    for sub in load_subcategories():
        mapped = [c for c in controls if sub.id in c.control.nist_subcategories]
        statuses = {c.status for c in mapped}
        if not mapped:
            status = "organisational"
        elif statuses == {"satisfied"}:
            status = "satisfied"
        elif statuses == {"gap"}:
            status = "gap"
        else:
            status = "partial"
        out.append(SubcategoryCoverage(sub, status, [c.control.id for c in mapped]))
    return out


def crosswalk() -> list[dict[str, Any]]:
    """Control → EU AI Act article + ISO/IEC 42001 crosswalk (R5, GWT-5)."""
    return [
        {
            "control": c.id,
            "nist_subcategories": c.nist_subcategories,
            "eu_ai_act": c.eu_ai_act,
            "iso_42001": c.iso_42001,
        }
        for c in load_catalogue()
    ]


__all__ = [
    "COVERAGE_DISCLAIMER",
    "FUNCTIONS",
    "NIST_SUBCATEGORY_COUNTS",
    "Control",
    "ControlCoverage",
    "CoverageReport",
    "Feature",
    "MappingError",
    "Subcategory",
    "SubcategoryCoverage",
    "load_catalogue",
    "load_features",
    "load_subcategories",
    "resolve_control",
    "catalogue_version",
    "validate_mapping",
    "governance_report",
    "crosswalk",
]
