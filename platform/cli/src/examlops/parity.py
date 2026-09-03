"""Portability gate — numeric parity across an execution-target change (ADR 0117 · G5.8).

Every gate this platform had before was **single-target**: `exa pipeline validate-model`
smoke-tests a model on the backend it is already running on, and `exa pipeline promote
--if-<metric>` reads a metric from the run that produced it. A portability gate is inherently
**two-target** — it compares the *same* model across *two* execution targets — and nothing did
that. Meanwhile `quantize_model()` registers, signs, BOMs and audits a requantised version
**with no numeric comparison at all**, and quantisation is a *deliberate* numeric change.

So a promotion that changed the numerics shipped on a green latency check.

**The vacuous-pass trap, and why this module is mostly about it.** On a host without CUDA,
`quantize_model()` takes the provenance-only path and warns that *"the weights are unchanged"*.
A comparator that simply diffed outputs would then report **perfect parity** and green-light the
promotion, having measured nothing at all. So the gate reads that provenance flag and reports
**`inert`** — never `passed`. This is P5 (*absent ≠ pass*) reaching a point the principle had not
previously reached: **an identical result is only evidence of parity when a transformation
actually occurred.**

Three verdicts, and only one of them lets an autonomous promotion through:

- **`passed`** — a transformation happened, fixtures ran on both targets, divergence is within
  the model's declared tolerance.
- **`blocked`** — divergence exceeded the tolerance. The *measured* value is recorded, not just
  the verdict, so drift toward the boundary is visible before it crosses.
- **`inert`** — nothing was compared: no transformation, no fixtures, or no reachable second
  target. It must never be read as a pass.

**Tolerance is per-model and declared** (`parity_tolerance` in the model's YAML), never global: a
ranking model tolerates far more numeric drift than a regression model whose output is a physical
quantity. A tolerance that never fires is evidence it is too loose — which is ADR 0111's principle
applied to this gate rather than to a judge.

Scope: this is the **quantisation arm** of ADR 0117 decision 3, the arm the roadmap moved into W1
because it needs no second accelerator family — `quantize_model()` changes numerics on the same
host. The engine arm needs a GPU host and the accelerator-family arm needs silicon we do not have;
both are deliberately out of scope here, and the gate reports `inert` rather than pretending.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BLOCKED",
    "DEFAULT_TOLERANCE",
    "INERT",
    "PASSED",
    "ParityResult",
    "divergence",
    "model_tolerance",
    "parity_gate",
    "quantization_provenance",
    "record_parity_check",
    "run_quantization_parity_gate",
    "target_change_for_version",
]

PASSED = "passed"
BLOCKED = "blocked"
INERT = "inert"

#: Used only when a model declares none. Deliberately tight: a model whose numerics move more
#: than this should have to say so in its YAML rather than inherit a permissive default.
DEFAULT_TOLERANCE = 1e-3


@dataclass(frozen=True)
class ParityResult:
    verdict: str
    reason: str
    model: str = ""
    source_version: str = ""
    target_version: str = ""
    #: The measured divergence, recorded whatever the verdict — a gate that reports only
    #: pass/fail hides drift toward its own boundary until the moment it crosses.
    max_abs_divergence: float | None = None
    max_rel_divergence: float | None = None
    n_fixtures: int = 0
    tolerance: float | None = None
    #: Did a transformation actually occur? ``False`` forces ``inert``.
    transformed: bool = False

    @property
    def permits_autonomous_promotion(self) -> bool:
        """Only a real, measured pass does. ``inert`` is not a pass (ADR 0117 verification 3)."""
        return self.verdict == PASSED

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "model": self.model,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "max_abs_divergence": self.max_abs_divergence,
            "max_rel_divergence": self.max_rel_divergence,
            "n_fixtures": self.n_fixtures,
            "tolerance": self.tolerance,
            "transformed": self.transformed,
        }


def divergence(
    source: Sequence[float], target: Sequence[float]
) -> tuple[float | None, float | None]:
    """Largest absolute and relative divergence between two fixture output vectors.

    A non-finite value on either side is itself a divergence, not something to average away, so
    it returns ``inf`` rather than skipping the pair. Relative divergence is scaled by the
    source magnitude, falling back to absolute where the source is zero — a change from 0 to
    0.5 is not a 0% change.
    """
    if len(source) != len(target):
        return float("inf"), float("inf")
    if not source:
        return None, None
    max_abs = 0.0
    max_rel = 0.0
    for s, t in zip(source, target, strict=True):
        s_f, t_f = float(s), float(t)
        if not math.isfinite(s_f) or not math.isfinite(t_f):
            if s_f != t_f and not (math.isnan(s_f) and math.isnan(t_f)):
                return float("inf"), float("inf")
            continue
        abs_d = abs(t_f - s_f)
        max_abs = max(max_abs, abs_d)
        max_rel = max(max_rel, abs_d / abs(s_f) if s_f != 0 else abs_d)
    return max_abs, max_rel


def parity_gate(
    source_outputs: Sequence[float] | None,
    target_outputs: Sequence[float] | None,
    *,
    tolerance: float,
    transformed: bool,
    model: str = "",
    source_version: str = "",
    target_version: str = "",
) -> ParityResult:
    """Compare two targets' fixture outputs and return the gate verdict.

    ``transformed`` is the load-bearing argument: when the artefact was never actually changed
    (the provenance-only quantisation path), identical outputs prove nothing and the verdict is
    ``inert`` **before** any comparison is attempted.
    """
    if not transformed:
        return ParityResult(
            verdict=INERT,
            reason=(
                "the artefact was not transformed (provenance-only quantisation) — an identical "
                "result is not evidence of parity when no transformation occurred"
            ),
            transformed=False,
            tolerance=tolerance,
            model=model,
            source_version=source_version,
            target_version=target_version,
        )
    if not source_outputs or not target_outputs:
        return ParityResult(
            verdict=INERT,
            reason="no fixture outputs from one or both targets — nothing was compared",
            transformed=True,
            tolerance=tolerance,
            n_fixtures=len(source_outputs or []),
            model=model,
            source_version=source_version,
            target_version=target_version,
        )

    max_abs, max_rel = divergence(source_outputs, target_outputs)
    if max_rel is None:
        return ParityResult(
            verdict=INERT,
            reason="no fixtures to compare",
            tolerance=tolerance,
            transformed=True,
            model=model,
            source_version=source_version,
            target_version=target_version,
        )
    if max_rel > tolerance:
        return ParityResult(
            verdict=BLOCKED,
            reason=(
                f"relative divergence {max_rel:.6g} exceeds the declared tolerance "
                f"{tolerance:.6g} over {len(source_outputs)} fixtures"
            ),
            max_abs_divergence=max_abs,
            max_rel_divergence=max_rel,
            n_fixtures=len(source_outputs),
            tolerance=tolerance,
            transformed=True,
            model=model,
            source_version=source_version,
            target_version=target_version,
        )
    return ParityResult(
        verdict=PASSED,
        reason=(
            f"relative divergence {max_rel:.6g} within tolerance {tolerance:.6g} "
            f"over {len(source_outputs)} fixtures"
        ),
        max_abs_divergence=max_abs,
        max_rel_divergence=max_rel,
        n_fixtures=len(source_outputs),
        tolerance=tolerance,
        transformed=True,
        model=model,
        source_version=source_version,
        target_version=target_version,
    )


def target_change_for_version(version: str) -> str | None:
    """The quantisation method a version string encodes, or ``None`` for an unchanged target.

    ``quantize_model()`` names its output ``<version>-<method>``, so the version itself records
    that the execution target changed. A promotion whose target did *not* change skips the gate
    entirely (ADR 0117 verification 1) — the gate is conditional on a target change, not a tax
    on every promotion.
    """
    from examlops.engines import _VALID_QUANT_METHODS

    for method in _VALID_QUANT_METHODS:
        if str(version).endswith(f"-{method}"):
            return method
    return None


def model_tolerance(model: str) -> float:
    """The model's declared parity tolerance, or :data:`DEFAULT_TOLERANCE`.

    Read from the pack's per-model YAML (``parity_tolerance``) so it is reviewed like any other
    gate threshold — never a global constant, because a ranking model tolerates far more numeric
    drift than a regression model whose output is a physical quantity (ADR 0117 decision 4).
    """
    try:
        import yaml

        from examlops.usecase import models_dir

        path = models_dir() / f"{model.lower()}.yaml"
        if not path.is_file():
            return DEFAULT_TOLERANCE
        doc = yaml.safe_load(path.read_text()) or {}
        value = doc.get("parity_tolerance")
        return float(value) if value is not None else DEFAULT_TOLERANCE
    except Exception:
        return DEFAULT_TOLERANCE


def quantization_provenance(model: str, version: str) -> dict[str, Any] | None:
    """What the quantisation of ``model@version`` actually did, from the evidence chain.

    Returns the recorded detail (including ``weights_transformed``) or ``None`` when no
    quantisation was recorded for that version. Reading it back from `audit_events` rather than
    a side table is deliberate: ADR 0110 makes the evidence chain the place promotion evidence
    lives, and a second store would be a second thing to keep true.
    """
    import json

    from examlops.data import get_db, init_db

    init_db()
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT details FROM audit_events WHERE action='model_quantized' AND target=? "
                "ORDER BY id DESC LIMIT 1",
                (f"{model}@{version}",),
            ).fetchone()
    except Exception:
        return None
    if row is None or not row["details"]:
        return None
    try:
        return json.loads(row["details"])
    except (ValueError, TypeError):
        return None


def record_parity_check(result: ParityResult, actor: str | None = None) -> None:
    """Record the gate's outcome **and its measured divergence** in the evidence chain."""
    from examlops.data import get_db, init_db
    from examlops.data.audit import write_audit_event

    init_db()
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO parity_checks
                       (model, source_version, target_version, verdict, max_abs_divergence,
                        max_rel_divergence, tolerance, n_fixtures, transformed, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.model,
                    result.source_version,
                    result.target_version,
                    result.verdict,
                    result.max_abs_divergence,
                    result.max_rel_divergence,
                    result.tolerance,
                    result.n_fixtures,
                    int(result.transformed),
                    result.reason,
                ),
            )
    except Exception:
        pass
    write_audit_event("exa-parity", actor, "parity_gate_evaluated", result.model, result.as_dict())


def run_quantization_parity_gate(
    model: str,
    target_version: str,
    *,
    runner: Any = None,
    tolerance: float | None = None,
    record: bool = True,
    actor: str | None = None,
) -> ParityResult | None:
    """Run the portability gate for a quantised version, or ``None`` if the target is unchanged.

    ``runner`` is a callable ``(model, version) -> Sequence[float]`` producing fixture outputs on
    one target; it is injected so the gate stays testable off-GPU and so the fixture source can
    be the evaluation suite's existing, versioned fixtures rather than a new set (ADR 0117
    decision 5 — the new code is the comparator, not a subsystem).
    """
    method = target_change_for_version(target_version)
    if method is None:
        return None  # target unchanged — the gate does not apply (verification 1)

    source_version = target_version[: -(len(method) + 1)]
    tol = tolerance if tolerance is not None else model_tolerance(model)
    provenance = quantization_provenance(model, target_version) or {}
    # A version with no recorded quantisation is not evidence that weights changed; absent
    # provenance is treated as untransformed, which yields `inert` rather than a free pass.
    transformed = bool(provenance.get("weights_transformed", False))

    source_out: Sequence[float] | None = None
    target_out: Sequence[float] | None = None
    if transformed and runner is not None:
        try:
            source_out = runner(model, source_version)
            target_out = runner(model, target_version)
        except Exception as exc:  # a runner that cannot reach a target leaves the gate inert
            result = ParityResult(
                verdict=INERT,
                reason=f"could not run fixtures on both targets: {exc}",
                model=model,
                source_version=source_version,
                target_version=target_version,
                tolerance=tol,
                transformed=True,
            )
            if record:
                record_parity_check(result, actor)
            return result

    result = parity_gate(
        source_out,
        target_out,
        tolerance=tol,
        transformed=transformed,
        model=model,
        source_version=source_version,
        target_version=target_version,
    )
    if record:
        record_parity_check(result, actor)
    return result
