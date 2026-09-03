"""Next-Gen 40 · C7 — shadow deployment & champion-challenger (ADR 0024).

Mirror production traffic to an isolated shadow/challenger model without returning its
responses, score it against the champion with the phase-24 A/B-stats engine
(`analysis/ab_stats.welch_t_test`) as labels arrive, and propose promotion (C3) only
when the challenger wins with significance and no C6 SLO regression.

Isolation & safety:
- **R2 isolation** — `run_shadow(fn)` swallows any shadow exception so a shadow crash or
  latency never affects the production path.
- **R3 side-effect-free** — inside `shadow_context()` a thread-local flag is set;
  `guard_write()` raises `ShadowWriteError` if a shadow tries to write, so side effects
  are prevented/flagged.

Graceful degradation: scoring falls back to a pure-Python mean/variance z-approximation
if SciPy/NumPy (used by `ab_stats`) is unavailable.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from examlops import data as platform_db

logger = logging.getLogger(__name__)

_shadow_local = threading.local()


class ShadowWriteError(RuntimeError):
    """Raised when a shadow/challenger model attempts a side effect (R3)."""


def shadow_context():
    """Context manager marking the current thread as running shadow inference (R3)."""

    class _Ctx:
        def __enter__(self):
            _shadow_local.active = True
            return self

        def __exit__(self, *exc):
            _shadow_local.active = False
            return False

    return _Ctx()


def in_shadow() -> bool:
    return getattr(_shadow_local, "active", False)


def guard_write(op: str = "write") -> None:
    """Call at every side-effecting boundary; raises if invoked under shadow (R3)."""
    if in_shadow():
        raise ShadowWriteError(f"shadow model attempted a side effect: {op}")


def run_shadow(fn: Callable[[], Any]) -> tuple[Any, Exception | None]:
    """Run shadow inference in isolation (R2). Returns (result, error); never raises."""
    with shadow_context():
        try:
            return fn(), None
        except Exception as e:  # isolation: never propagate to production
            return None, e


def enable_shadow(
    model: str,
    challenger_version: str,
    mirror_pct: int = 100,
    *,
    tenant: str = "default",
    min_delta: float = 0.0,
    alpha: float = 0.05,
    min_samples: int = 100,
    auto_promote: bool = False,
    actor: str | None = None,
) -> None:
    """Enable a challenger for a model and declare its promotion policy (R1/R5)."""
    platform_db.set_challenger_config(
        model,
        challenger_version,
        tenant=tenant,
        mirror_pct=max(0, min(100, mirror_pct)),
        min_delta=min_delta,
        alpha=alpha,
        min_samples=min_samples,
        auto_promote=auto_promote,
        enabled=True,
        updated_by=actor,
    )
    platform_db.write_audit_event(
        "cli",
        actor,
        "challenger_enable",
        model,
        {"challenger_version": challenger_version, "mirror_pct": mirror_pct, "tenant": tenant},
    )


def _score_errors(samples: list[dict[str, Any]]) -> tuple[list[float], list[float], str]:
    """Per-sample error for champion and challenger, and which evidence produced it.

    **Ground truth wins wherever it exists.** A judge is the fallback for samples no label ever
    arrived for — clause 2's "when labels arrive (ground-truth) **or** via a C2 judge" — not a
    second opinion on samples that have one. Preferring the judge where a label exists would
    replace a measurement with an estimate.

    Judge scores are quality in [0,1], so the error is ``1 - score``: higher is better for a
    judge and lower is better for an error, and the Welch machinery downstream compares errors.

    Returns ``(champion_errors, challenger_errors, evidence)`` where evidence is
    ``labels`` / ``judge`` / ``mixed`` / ``none`` — a scoreboard that cannot say what it rests on
    is a promotion decision of unknown provenance.
    """
    champ: list[float] = []
    chall: list[float] = []
    used_label = used_judge = False
    for s in samples:
        if s["label"] is not None:
            used_label = True
            if s["champion_pred"] is not None:
                champ.append(abs(s["champion_pred"] - s["label"]))
            if s["challenger_pred"] is not None:
                chall.append(abs(s["challenger_pred"] - s["label"]))
            continue
        cj, gj = s.get("champion_judge"), s.get("challenger_judge")
        if cj is None and gj is None:
            continue
        used_judge = True
        if cj is not None:
            champ.append(1.0 - float(cj))
        if gj is not None:
            chall.append(1.0 - float(gj))
    evidence = (
        "mixed"
        if used_label and used_judge
        else "labels"
        if used_label
        else "judge"
        if used_judge
        else "none"
    )
    return champ, chall, evidence


def judge_of(samples: list[dict[str, Any]]) -> str | None:
    """The judge whose scores are in this scoreboard, or None if no judge contributed."""
    for s in samples:
        if s.get("judge_model") and s.get("label") is None:
            return str(s["judge_model"])
    return None


def score_samples_with_judge(
    model: str,
    judge_fn: Any,
    *,
    judge_model: str = "judge",
    tenant: str = "default",
    limit: int = 200,
) -> dict[str, Any]:
    """Score unlabelled challenger samples with a C2 judge (ADR 0024 clause 2).

    ``judge_fn(champion_pred, challenger_pred) -> (champion_score, challenger_score)``, each in
    [0,1]. The seam is the same shape C2 uses: any callable, wired to the B2 gateway in
    production and mocked in tests.

    Only samples that have **no label** are scored — see :func:`_score_errors`. A sample the
    judge fails on is left unscored rather than scored 0: a judge error is not a bad prediction,
    and recording it as one would move the promotion decision.
    """
    samples = platform_db.get_challenger_samples(model, tenant=tenant, last_n=limit)
    scored = failed = 0
    for s in samples:
        if s["label"] is not None or s.get("judge_model"):
            continue
        try:
            champion, challenger = judge_fn(s["champion_pred"], s["challenger_pred"])
        except Exception:  # noqa: BLE001 - a judge error is not a bad prediction
            failed += 1
            continue
        platform_db.set_challenger_judge_scores(
            int(s["id"]),
            champion=_clamp(champion),
            challenger=_clamp(challenger),
            judge_model=judge_model,
        )
        scored += 1
    platform_db.write_audit_event(
        "cli",
        None,
        "challenger_judge_scored",
        model,
        {"scored": scored, "failed": failed, "judge": judge_model, "tenant": tenant},
    )
    return {"model": model, "judge": judge_model, "scored": scored, "failed": failed}


def _clamp(value: Any) -> float | None:
    """A judge score is a quality in [0,1]; anything else is not scoreable."""
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return None


def _welch(a: list[float], b: list[float]) -> dict[str, Any]:
    """Welch's t-test via ab_stats; pure-Python normal approximation as a fallback."""
    try:
        from examlops.analysis.ab_stats import welch_t_test

        return welch_t_test(a, b)
    except Exception:
        na, nb = len(a), len(b)
        ma = sum(a) / na
        mb = sum(b) / nb
        va = sum((x - ma) ** 2 for x in a) / max(na - 1, 1)
        vb = sum((x - mb) ** 2 for x in b) / max(nb - 1, 1)
        se = math.sqrt(va / na + vb / nb) or 1e-9
        z = (ma - mb) / se
        # Two-sided p from the standard normal survival function.
        p = math.erfc(abs(z) / math.sqrt(2))
        return {
            "test": "z_approx",
            "t_stat": z,
            "p_value": p,
            "mean_a": ma,
            "mean_b": mb,
            "n_a": na,
            "n_b": nb,
        }


@dataclass
class ChallengerStatus:
    model: str
    challenger_version: str
    n: int
    champion_error: float | None
    challenger_error: float | None
    delta: float | None  # champion_error - challenger_error (positive = challenger better)
    p_value: float | None
    significant: bool
    slo_ok: bool
    slo_reason: str
    policy_met: bool
    #: Which evidence produced the scores — ``labels`` / ``judge`` / ``mixed`` / ``none``.
    evidence: str = "labels"
    judge: str | None = None
    judge_eligible: bool = True
    judge_failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "challenger_version": self.challenger_version,
            "n": self.n,
            "champion_error": self.champion_error,
            "challenger_error": self.challenger_error,
            "delta": self.delta,
            "p_value": self.p_value,
            "significant": self.significant,
            "slo_ok": self.slo_ok,
            "slo_reason": self.slo_reason,
            "policy_met": self.policy_met,
            "evidence": self.evidence,
            "judge": self.judge,
            "judge_eligible": self.judge_eligible,
            "judge_failures": list(self.judge_failures),
        }


def _slo_verdict(model: str, tenant: str) -> tuple[bool, str]:
    """Whether C6 says it is safe to promote, and the phrase that says why.

    The caller writes this phrase into an audit event. It used to be the fixed string ``SLO OK``,
    appended whenever this function returned ``True`` — and this function returned ``True`` for
    every way of not knowing: C6 not installed, the store unreadable, the query raising, no SLO
    configured, an SLO configured but never measured. A promotion could therefore be recorded,
    permanently, as having cleared an SLO check that never ran.

    Absent and broken are now different answers. The original docstring licensed fail-open for
    "C6 absent" — an optional feature nobody installed — and that is kept, as is the permissive
    answer when there is no SLO to consult or no data yet. A check that ran and *failed* is not
    absence: it is an unknown about the thing being gated, and promotion is the risky direction,
    so it blocks. Either way the returned phrase states what was actually established.
    """
    try:
        from examlops.slo import slo_status
    except ImportError:
        return True, "SLO checks unavailable (C6 not installed)"
    try:
        statuses = slo_status(model, tenant=tenant)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "SLO check failed for %s; refusing to treat that as no regression: %s", model, exc
        )
        return False, f"SLO check failed: {exc}"

    if not statuses:
        return True, "no SLO configured"
    exhausted = [s.name for s in statuses if s.n > 0 and s.budget_remaining <= 0]
    if exhausted:
        return False, f"SLO budget exhausted: {', '.join(exhausted)}"
    # An SLO with no samples scores a perfect SLI (`good/total` with total 0 falls back to 1.0),
    # so it would otherwise arrive here indistinguishable from one measured and meeting target.
    measured = [s for s in statuses if s.n > 0]
    if not measured:
        return True, "SLO configured but not yet measured"
    if len(measured) < len(statuses):
        return True, f"{len(measured)}/{len(statuses)} SLO budgets within target, rest unmeasured"
    return True, "SLO budgets within target"


def challenger_status(model: str, *, tenant: str = "default") -> ChallengerStatus | None:
    """Score the challenger against the champion (R4). Returns None if not configured."""
    cfg = platform_db.get_challenger_config(model)
    if not cfg:
        return None
    # Not `labelled_only=True`: a judge-scored sample has no label and is exactly what clause 2
    # exists to score. `_score_errors` skips samples with neither, so a deployment where no judge
    # has run produces the identical scoreboard it did before.
    samples = platform_db.get_challenger_samples(model, tenant=tenant, labelled_only=False)
    champ_err, chall_err, evidence = _score_errors(samples)
    n = min(len(champ_err), len(chall_err))
    champion_error = (sum(champ_err) / len(champ_err)) if champ_err else None
    challenger_error = (sum(chall_err) / len(chall_err)) if chall_err else None
    delta = (
        (champion_error - challenger_error)
        if (champion_error is not None and challenger_error is not None)
        else None
    )
    p_value = None
    significant = False
    if len(champ_err) >= 2 and len(chall_err) >= 2:
        res = _welch(champ_err, chall_err)
        p_value = res["p_value"]
        significant = p_value < cfg["alpha"]
    slo_ok, slo_reason = _slo_verdict(model, tenant)
    judge = judge_of(samples)
    judge_eligible, judge_failures = _judge_eligibility(judge)
    policy_met = (
        delta is not None
        and delta >= cfg["min_delta"]
        and significant
        and n >= cfg["min_samples"]
        and slo_ok
        and judge_eligible
    )
    return ChallengerStatus(
        model=model,
        challenger_version=cfg["challenger_version"],
        n=n,
        champion_error=champion_error,
        challenger_error=challenger_error,
        delta=delta,
        p_value=p_value,
        significant=significant,
        slo_ok=slo_ok,
        slo_reason=slo_reason,
        policy_met=policy_met,
        evidence=evidence,
        judge=judge,
        judge_eligible=judge_eligible,
        judge_failures=judge_failures,
    )


def _judge_eligibility(judge: str | None) -> tuple[bool, list[str]]:
    """ADR 0111 on this road too: no uncalibrated judge may decide a promotion.

    A scoreboard resting on ground truth has no judge and is unaffected. One resting on a judge
    is an instrument deciding what reaches production, and the MVVP applies wherever that is
    true — `exa eval gate` and `exa pipeline promote` already refuse it, and a challenger
    promotion is the same decision reached by a different road.
    """
    if not judge:
        return True, []
    try:
        from examlops.evaluation.calibration import is_gate_eligible

        return is_gate_eligible(judge)
    except Exception:  # noqa: BLE001 - an unanswerable question is not a pass
        return False, ["judge_calibration_unavailable"]


@dataclass
class PromotionProposal:
    model: str
    challenger_version: str
    delta: float
    p_value: float | None
    n: int
    auto: bool
    reason: str


def maybe_promote(
    model: str, *, tenant: str = "default", actor: str | None = None
) -> PromotionProposal | None:
    """Propose promotion via C3 if the policy is met and no SLO regression (R5/R6)."""
    status = challenger_status(model, tenant=tenant)
    if status is None or not status.policy_met:
        return None
    cfg = platform_db.get_challenger_config(model)
    proposal = PromotionProposal(
        model=model,
        challenger_version=status.challenger_version,
        delta=status.delta or 0.0,
        p_value=status.p_value,
        n=status.n,
        auto=bool(cfg and cfg["auto_promote"]),
        reason=(
            f"challenger beats champion by Δ={status.delta:.4f} error "
            f"(p={status.p_value:.4f}, n={status.n}); {status.slo_reason}"
        ),
    )
    platform_db.write_audit_event(
        "cli",
        actor,
        "challenger_promotion_proposed",
        model,
        {
            "challenger_version": status.challenger_version,
            "delta": status.delta,
            "p_value": status.p_value,
            "n": status.n,
            "auto": proposal.auto,
        },
    )
    return proposal


__all__ = [
    "ShadowWriteError",
    "ChallengerStatus",
    "PromotionProposal",
    "shadow_context",
    "in_shadow",
    "guard_write",
    "run_shadow",
    "enable_shadow",
    "challenger_status",
    "maybe_promote",
]
