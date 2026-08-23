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
from dataclasses import dataclass
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


def _score_errors(samples: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    """Per-sample absolute error for champion and challenger over labelled samples."""
    champ, chall = [], []
    for s in samples:
        if s["label"] is None:
            continue
        if s["champion_pred"] is not None:
            champ.append(abs(s["champion_pred"] - s["label"]))
        if s["challenger_pred"] is not None:
            chall.append(abs(s["challenger_pred"] - s["label"]))
    return champ, chall


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
    samples = platform_db.get_challenger_samples(model, tenant=tenant, labelled_only=True)
    champ_err, chall_err = _score_errors(samples)
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
    policy_met = (
        delta is not None
        and delta >= cfg["min_delta"]
        and significant
        and n >= cfg["min_samples"]
        and slo_ok
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
    )


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
