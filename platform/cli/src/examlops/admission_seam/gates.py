"""External admission gates consulted behind the one ``decide()`` call (ADR 0116 decision 5).

ADR 0116 decision 5: *policy, budget, carbon-decision signal, evaluation status and burn-in state
are consulted through one ``decide()`` call*. Before this module ``decide()`` consulted only the
fair-share policy, so a project over its budget, a policy author's ``deny`` for an action, or a
high-carbon hour were all invisible to admission — they were enforced (if at all) somewhere else,
by whichever command remembered to ask.

A gate answers one question about one request and returns a :class:`GateResult` with one of four
verdicts:

* ``allow``   — the gate looked and has no objection;
* ``deny``    — the request must not run (maps to a ``Reject``);
* ``defer``   — not now; it may run later (maps to a ``Queue``);
* ``abstain`` — the gate has nothing to say (no budget declared, no decision-grade carbon signal).
  Abstaining is not allowing by inference: it is recorded, with its reason, in the decision meta.

**Opt-in, then fail closed.** ``EXAMLOPS_ADMISSION_GATES`` names the gates to consult
(``policy,budget,carbon``). Unset, no gate runs and ``decide()`` is byte-identical to what it was —
the default policy's equivalence proof stands. Once a gate is configured it is enforced, and a gate
that *cannot answer* (the budget store raises, the policy engine breaks, an unknown gate name is
configured) denies with ``unverified: …`` rather than letting the job through: ADR 0108 — *a broker
that cannot verify something reports it as unverified rather than failing open*.

**Combination is deny-overrides.** Every configured gate runs (so the meta shows all of them); any
``deny`` rejects, otherwise any ``defer`` queues, otherwise the policy's own decision stands.

Not built here, and said so rather than implied: an *evaluation-status* gate (a ``JobRequest``
names no model, so there is nothing to look an evaluation up by) and a *burn-in* gate (the
platform records no node burn-in state to consult). Both slot in by :func:`register_gate`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from .request import JobRequest

GATES_ENV = "EXAMLOPS_ADMISSION_GATES"
CARBON_MAX_ENV = "EXAMLOPS_ADMISSION_CARBON_MAX_G"
#: The policy-as-code action every admission is checked as (``policy.yaml`` ``action: admission``).
POLICY_ACTION = "admission"

ALLOW = "allow"
DENY = "deny"
DEFER = "defer"
ABSTAIN = "abstain"
VERDICTS = (ALLOW, DENY, DEFER, ABSTAIN)


@dataclass(frozen=True)
class GateResult:
    gate: str
    verdict: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "verdict": self.verdict,
            "reason": self.reason,
            **({"detail": self.detail} if self.detail else {}),
        }


@runtime_checkable
class AdmissionGate(Protocol):
    name: str

    def evaluate(self, request: JobRequest, *, record: bool) -> GateResult: ...


def _context(request: JobRequest) -> dict[str, Any]:
    """The typed decision context a ``policy.yaml`` ``when:`` for ``admission`` can refer to."""
    r = request.resources
    return {
        "target": request.project,
        "project": request.project,
        "tenant": request.tenant,
        "workload_class": request.workload_class,
        "gpus": r.gpus,
        "cpus": r.cpus,
        "memory_gb": r.memory_gb,
        "nodes": r.nodes,
        "gang": request.gang,
        "network_tier": request.network_tier,
        "scale_up_domain": request.scale_up_domain,
        "priority_class": request.priority_class,
        "queue": request.queue or "",
        "flexibility_s": request.flexibility_s,
        "gpu_hours": request.gpu_hours,
    }


class PolicyGate:
    """ADR 0079 policy-as-code, asked about the ``admission`` action.

    ``decide_safe`` fails to ``deny`` if the engine itself breaks. With no policy file (or no rule
    for ``admission``) the engine's documented default is ``allow``; that is reported as ``allow``
    with the engine's own reason, never upgraded or downgraded here.
    """

    name = "policy"

    def evaluate(self, request: JobRequest, *, record: bool) -> GateResult:
        from examlops import policy

        ctx = _context(request)
        decision = policy.decide_safe(POLICY_ACTION, ctx, default_effect=policy.DENY, audit=False)
        if record:
            policy.record_decision(POLICY_ACTION, ctx, decision)
        detail = {"rule": decision.rule} if decision.rule else {}
        if decision.unavailable:
            return GateResult(self.name, DENY, f"unverified: {decision.reason}", detail)
        if decision.effect == policy.ALLOW:
            return GateResult(self.name, ALLOW, decision.reason, detail)
        if decision.effect == policy.REQUIRE_APPROVAL:
            return GateResult(self.name, DEFER, f"requires approval ({decision.reason})", detail)
        return GateResult(self.name, DENY, decision.reason, detail)


class BudgetGate:
    """The project's FinOps budget (ADR 0089): a breached period, or a request that would breach
    it, is denied. A project with no budget declared abstains."""

    name = "budget"

    def evaluate(self, request: JobRequest, *, record: bool) -> GateResult:
        from examlops.project_finops import budget_status

        status = budget_status(request.project)
        budget = status.get("budget") or {}
        if not budget:
            return GateResult(self.name, ABSTAIN, f"project {request.project!r} has no budget")
        breaches = list(status.get("breaches") or [])
        if breaches:
            return GateResult(
                self.name,
                DENY,
                f"project {request.project!r} is over budget: " + "; ".join(breaches),
                {"period": status.get("period")},
            )
        cap = budget.get("gpu_hours_budget")
        if cap is not None and request.gpu_hours > 0:
            used = float(status["consumption"]["gpu_hours"])
            if used + request.gpu_hours > float(cap):
                return GateResult(
                    self.name,
                    DENY,
                    f"would exceed the GPU-hour budget ({used:.2f}+{request.gpu_hours:.2f} > "
                    f"{float(cap):.2f} this {status.get('period')})",
                    {"period": status.get("period")},
                )
        return GateResult(self.name, ALLOW, "within budget", {"period": status.get("period")})


class CarbonGate:
    """Defer *flexible* work while the grid's marginal intensity is above a threshold (ADR 0112).

    Only a **decision**-type signal may drive this: an average/attributional feed abstains, because
    shifting on it can lower the emissions *allocated* to us while raising the system's total (the
    ADR 0112 finding). Inflexible work (``flexibility_s == 0``) is never deferred, and neither is
    work whose deadline leaves no room to wait — carbon shifting moves work in time, it does not
    cancel it.
    """

    name = "carbon"

    def __init__(self, threshold: float | None = None):
        self._threshold = threshold

    def threshold(self) -> float | None:
        if self._threshold is not None:
            return self._threshold
        raw = os.getenv(CARBON_MAX_ENV, "").strip()
        if not raw:
            return None
        value = float(raw)  # a malformed threshold raises -> the gate reports unverified
        if value <= 0:
            raise ValueError(f"{CARBON_MAX_ENV} must be > 0, got {raw!r}")
        return value

    def evaluate(self, request: JobRequest, *, record: bool) -> GateResult:
        from examlops.finops.grid_intensity import current_grid_signal

        limit = self.threshold()
        if limit is None:
            return GateResult(self.name, ABSTAIN, f"no threshold configured ({CARBON_MAX_ENV})")
        signal = current_grid_signal(default=0.0)
        detail = signal.as_dict()
        if not signal.is_decision:
            return GateResult(
                self.name,
                ABSTAIN,
                f"no decision-grade carbon signal (method {signal.method!r} is "
                f"{signal.signal_type}); not shifting work on it",
                detail,
            )
        if signal.grams_per_kwh <= limit:
            return GateResult(
                self.name, ALLOW, f"{signal.grams_per_kwh:.0f} <= {limit:.0f} gCO2/kWh", detail
            )
        if request.flexibility_s <= 0:
            return GateResult(
                self.name,
                ALLOW,
                f"{signal.grams_per_kwh:.0f} > {limit:.0f} gCO2/kWh, but the request is not "
                "flexible (flexibility_s=0); inflexible work is never carbon-deferred",
                detail,
            )
        if request.deadline is not None:
            deadline = datetime.fromisoformat(str(request.deadline))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            runtime = timedelta(seconds=float(request.est_runtime_s or 0.0))
            if datetime.now(UTC) + runtime >= deadline:
                return GateResult(
                    self.name,
                    ALLOW,
                    "marginal intensity is high but the deadline leaves no room to defer",
                    detail,
                )
        return GateResult(
            self.name,
            DEFER,
            f"marginal grid intensity {signal.grams_per_kwh:.0f} gCO2/kWh is above "
            f"{limit:.0f}; flexible work (flexibility_s={request.flexibility_s:.0f}) is deferred",
            detail,
        )


_REGISTRY: dict[str, Callable[[], AdmissionGate]] = {
    PolicyGate.name: PolicyGate,
    BudgetGate.name: BudgetGate,
    CarbonGate.name: CarbonGate,
}


def register_gate(name: str, factory: Callable[[], AdmissionGate]) -> None:
    """Add a gate (trusted-tier code, e.g. a site's evaluation or burn-in check)."""
    if not name or not name.strip():
        raise ValueError("gate name must be non-empty")
    _REGISTRY[name.strip().lower()] = factory


def gate_names() -> list[str]:
    return sorted(_REGISTRY)


def configured_gate_names(raw: str | None = None) -> list[str]:
    """The gates to consult, in configured order, de-duplicated. An unknown name raises."""
    text = os.getenv(GATES_ENV, "") if raw is None else raw
    names: list[str] = []
    for part in text.split(","):
        name = part.strip().lower()
        if not name or name in names:
            continue
        if name not in _REGISTRY:
            raise ValueError(f"unknown admission gate {name!r}; choose from {gate_names()}")
        names.append(name)
    return names


def evaluate_gates(
    request: JobRequest,
    *,
    names: list[str] | None = None,
    record: bool = True,
    gates: list[AdmissionGate] | None = None,
) -> list[GateResult]:
    """Run every configured gate. A gate that raises, or a bad configuration, yields ``deny``."""
    if gates is None:
        try:
            chosen = configured_gate_names() if names is None else names
            gates = [_REGISTRY[n]() for n in chosen]
        except (ValueError, KeyError) as exc:
            return [GateResult("configuration", DENY, f"unverified: {exc}")]
    results: list[GateResult] = []
    for gate in gates:
        try:
            result = gate.evaluate(request, record=record)
        except Exception as exc:  # noqa: BLE001 - a gate that cannot answer does not let work through
            result = GateResult(gate.name, DENY, f"unverified: {type(exc).__name__}: {exc}")
        if result.verdict not in VERDICTS:
            result = GateResult(gate.name, DENY, f"unverified: unknown verdict {result.verdict!r}")
        results.append(result)
    return results


def combine(results: list[GateResult]) -> GateResult | None:
    """Deny-overrides: the first ``deny``, else the first ``defer``, else ``None`` (no objection)."""
    for verdict in (DENY, DEFER):
        for result in results:
            if result.verdict == verdict:
                return result
    return None


def gates_configured() -> bool:
    """True when any gate is configured — including a *bad* configuration, which must then reach
    :func:`evaluate_gates` and deny rather than be skipped as "nothing configured"."""
    return bool(os.getenv(GATES_ENV, "").strip().strip(","))
