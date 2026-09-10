"""Carbon-aware placement must earn its place (ADR 0112, V16 amendment: R-ec and R-ed).

Sukprasert et al. (EuroSys '24, carbon-intensity data from 123 regions) measured what carbon-aware
workload shifting is worth: *"simple scheduling policies often yield most of these reductions,
with more sophisticated techniques yielding little additional benefit"*, and that benefit
*"will decrease as the energy supply becomes 'greener'"*. ADR 0112 turned those findings into
two obligations, and this module is both of them:

- **R-ec — simple-baseline dominance.** A carbon-weighing placement policy is evaluated against
  the simple baselines on the same workload and intensity trace. Unless it beats the best simple
  policy by a *declared margin*, the simple policy ships.
- **R-ed — declining-benefit re-test.** The measured benefit is re-evaluated on a stated cadence,
  and below a stated *retirement* threshold the capability is switched off rather than maintained.

The measurement (:func:`evaluate`) is a trace-driven simulation, pure and deterministic. Its
result is recorded as an **evaluation event in the hash-chained audit log** (ADR 0110 decision 1:
evaluations are chained synchronously), so the evidence a placement policy runs on cannot be
edited afterwards. The runtime gate (:func:`gate_decision`) reads the latest one.

Model and assumptions — deliberately the idealised ones Sukprasert et al. use for upper bounds:

- A job ``j`` needs ``d_j`` whole hours and ``E_j`` kWh, spread evenly, may start any hour in
  ``[s_j, s_j + slack_j]`` and may run in any of its allowed regions.
- Emissions of running ``j`` in region ``r`` from hour ``t``:
  ``C(j, r, t) = (E_j / d_j) · Σ_{h=t}^{t+d_j-1} I_r(h)``, with ``I_r`` in gCO₂e/kWh.
- **No capacity limits and no migration cost.** This overstates what shifting can achieve, which
  is the conservative direction for this test: a policy that cannot beat a simple baseline even
  with free, unlimited capacity will not beat it in production.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

EVALUATION_ACTION = "carbon_policy_evaluated"

#: The two simple baselines ADR 0112 names, and the runtime policy that stands for them.
SIMPLE_POLICIES = ("lowest-average-region", "threshold-shift")
RUNTIME_SIMPLE = "carbon-simple"
AGNOSTIC = "carbon-agnostic"
ORACLE = "oracle"  # perfect foresight — the headroom bound, never deployable
BUILTIN_CANDIDATES = ("forecast-greedy",)

DEFAULT_MARGIN_PP = 5.0
DEFAULT_RETEST_DAYS = 90
DEFAULT_RETIRE_BELOW_PCT = 2.0
DEFAULT_THRESHOLD_PERCENTILE = 30.0
GATE_MODES = ("enforce", "warn")


class TraceError(ValueError):
    """A workload or intensity trace that cannot be evaluated."""


# ── configuration ─────────────────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not a number") from exc


def margin_pp() -> float:
    """Percentage points of carbon-agnostic emissions a policy must beat the best simple one by."""
    return _env_float("EXAMLOPS_CARBON_POLICY_MARGIN_PP", DEFAULT_MARGIN_PP)


def retest_days() -> float:
    """R-ed cadence: an evaluation older than this is overdue."""
    return _env_float("EXAMLOPS_CARBON_POLICY_RETEST_DAYS", DEFAULT_RETEST_DAYS)


def retire_below_pct() -> float:
    """R-ed retirement: a shipped policy saving less than this share of emissions is switched off."""
    return _env_float("EXAMLOPS_CARBON_POLICY_RETIRE_BELOW_PCT", DEFAULT_RETIRE_BELOW_PCT)


def gate_mode() -> str:
    mode = (os.getenv("EXAMLOPS_CARBON_POLICY_GATE") or "enforce").strip().lower()
    if mode not in GATE_MODES:
        raise ValueError(f"EXAMLOPS_CARBON_POLICY_GATE={mode!r} not in {GATE_MODES}")
    return mode


# ── the workload and the grid ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Job:
    id: str
    submit: int
    duration: int
    energy_kwh: float
    slack: int = 0
    regions: tuple[str, ...] | None = None
    home: str | None = None


@dataclass(frozen=True)
class Trace:
    """Hourly carbon intensity per region (gCO₂e/kWh), all series the same length."""

    intensity: Mapping[str, Sequence[float]]
    method: str = "average_grid_mix"
    synthetic: bool = False

    @property
    def regions(self) -> list[str]:
        return sorted(self.intensity)

    @property
    def horizon(self) -> int:
        return len(next(iter(self.intensity.values()))) if self.intensity else 0

    def validate(self) -> None:
        if not self.intensity:
            raise TraceError("the intensity trace names no region")
        lengths = {len(v) for v in self.intensity.values()}
        if len(lengths) != 1 or 0 in lengths:
            raise TraceError(
                f"every region's series must have the same, non-zero length: {lengths}"
            )
        for r, series in self.intensity.items():
            for x in series:
                if not math.isfinite(float(x)) or float(x) < 0:
                    raise TraceError(f"region {r!r} has an invalid intensity value {x!r}")

    def signal_type(self) -> str:
        """``decision`` or ``accounting``, derived from the method exactly as ADR 0112 types it."""
        try:
            from examlops.finops.carbon_signal import signal_type_for_method

            return signal_type_for_method(self.method)
        except Exception:  # noqa: BLE001 - an unknown method is reported, not guessed
            return "unknown"


def _allowed(job: Job, trace: Trace) -> list[str]:
    allowed = [r for r in (job.regions or tuple(trace.regions)) if r in trace.intensity]
    if not allowed:
        raise TraceError(f"job {job.id!r} may run in no region of the trace")
    return allowed


def _home(job: Job, trace: Trace) -> str:
    allowed = _allowed(job, trace)
    return job.home if job.home in allowed else allowed[0]


def _starts(job: Job, trace: Trace) -> range:
    last = min(job.submit + job.slack, trace.horizon - job.duration)
    if job.submit > last:
        raise TraceError(
            f"job {job.id!r} (submit {job.submit}, {job.duration} h) does not fit in the "
            f"{trace.horizon}-hour trace"
        )
    return range(job.submit, last + 1)


def emissions_g(job: Job, trace: Trace, region: str, start: int) -> float:
    """``C(j, r, t)``: grams CO₂e of running ``job`` in ``region`` from hour ``start``."""
    series = trace.intensity[region]
    per_hour_kwh = job.energy_kwh / job.duration
    return per_hour_kwh * float(sum(series[start : start + job.duration]))


# ── policies: (job, trace) → (region, start) ──────────────────────────────────

Placement = tuple[str, int]
Policy = Callable[[Job, Trace], Placement]


def carbon_agnostic(job: Job, trace: Trace) -> Placement:
    """The reference every benefit is measured against: home region, at submission."""
    return _home(job, trace), job.submit


def lowest_average_region(job: Job, trace: Trace) -> Placement:
    """Simple spatial baseline: the allowed region with the lowest mean intensity; no waiting.

    The mean over the trace stands in for the published annual average an operator would know in
    advance. Ties break by region name.
    """
    allowed = _allowed(job, trace)
    means = {r: sum(trace.intensity[r]) / trace.horizon for r in allowed}
    return min(allowed, key=lambda r: (means[r], r)), job.submit


def _percentile(values: Sequence[float], pct: float) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def threshold_shift(
    job: Job, trace: Trace, *, percentile: float = DEFAULT_THRESHOLD_PERCENTILE
) -> Placement:
    """Simple temporal baseline: in the home region, start at the first hour in the slack window
    whose run-window mean intensity is at or below the region's ``percentile``-th percentile;
    if none qualifies, start at submission (waiting without cause buys nothing)."""
    home = _home(job, trace)
    threshold = _percentile(trace.intensity[home], percentile)
    for t in _starts(job, trace):
        window = trace.intensity[home][t : t + job.duration]
        if sum(window) / job.duration <= threshold:
            return home, t
    return home, job.submit


def oracle(job: Job, trace: Trace) -> Placement:
    """Perfect foresight: the lowest-emission (region, start) in the window. Not deployable — it
    reads the future — and reported only as the headroom any policy could possibly reach."""
    options = [
        (emissions_g(job, trace, r, t), r, t)
        for r in _allowed(job, trace)
        for t in _starts(job, trace)
    ]
    _, region, start = min(options)
    return region, start


def forecast_greedy(job: Job, trace: Trace) -> Placement:
    """A deployable spatio-temporal policy: the oracle's search, scored on a 24-hour persistence
    forecast — each future hour is predicted by the most recent same-hour value observed before
    submission. It uses nothing a real scheduler would not know at submit time."""

    def predicted(region: str, hour: int) -> float:
        series = trace.intensity[region]
        back = hour
        while back >= job.submit:
            back -= 24
        return float(series[back]) if back >= 0 else float(series[job.submit])

    def score(region: str, start: int) -> float:
        return sum(predicted(region, h) for h in range(start, start + job.duration))

    options = [(score(r, t), r, t) for r in _allowed(job, trace) for t in _starts(job, trace)]
    _, region, start = min(options)
    return region, start


def provider_policy(provider_name: str) -> Policy:
    """Evaluate a registered **placement provider** as a trace policy.

    At submission the provider scores every allowed region as a cluster with equal headroom and
    that region's *current* intensity as ``carbon_intensity`` (a live signal), and the job runs
    in the best-scoring region immediately — which is what the provider does in production.
    """
    from examlops.hpc_placement_providers import DOMAIN, register_builtins
    from examlops.providers.loader import resolve_provider

    register_builtins()
    provider = resolve_provider(DOMAIN, override=provider_name, group=DOMAIN)

    def _policy(job: Job, trace: Trace) -> Placement:
        scores: dict[str, float] = {}
        for r in _allowed(job, trace):
            inputs = {
                "idle_gpus": 1,
                "total_gpus": 1,
                "idle_nodes": 1,
                "total_nodes": 1,
                "ask_gpus": 0,
                "ask_cpus": 0,
                "ask_nodes": 0,
                "carbon_intensity": float(trace.intensity[r][job.submit]),
            }
            scores[r] = float(provider.compute(inputs)["score"])
        # Highest score wins; ties break by region name, so the result is deterministic.
        return min(scores, key=lambda r: (-scores[r], r)), job.submit

    return _policy


def resolve_policy(name: str) -> Policy:
    builtins: dict[str, Policy] = {
        AGNOSTIC: carbon_agnostic,
        "lowest-average-region": lowest_average_region,
        "threshold-shift": threshold_shift,
        ORACLE: oracle,
        "forecast-greedy": forecast_greedy,
    }
    if name in builtins:
        return builtins[name]
    return provider_policy(name)


# ── evaluation (R-ec) ─────────────────────────────────────────────────────────


def trace_digest(jobs: Sequence[Job], trace: Trace) -> str:
    canonical = json.dumps(
        {
            "jobs": [asdict(j) for j in jobs],
            "intensity": {r: [float(x) for x in trace.intensity[r]] for r in trace.regions},
            "method": trace.method,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class Evaluation:
    candidate: str
    emissions_g: dict[str, float]
    reduction_pct: dict[str, float]
    best_simple: str
    margin_pp: float
    advantage_pp: float
    decision: str  # "candidate" | "simple"
    shipped: str
    shipped_benefit_pct: float
    retire_below_pct: float
    retired: bool
    headroom_pct: float
    jobs: int
    horizon_h: int
    regions: list[str]
    trace_digest: str
    trace_method: str
    signal_type: str
    synthetic: bool
    evaluated_at: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate(
    jobs: Sequence[Job],
    trace: Trace,
    candidate: str,
    *,
    margin: float | None = None,
    retire_below: float | None = None,
    now: datetime | None = None,
) -> Evaluation:
    """Run the candidate, both simple baselines, the carbon-agnostic reference and the oracle over
    the same workload, and decide what ships (R-ec) and whether it is worth running at all (R-ed).

    ``reduction_pct[p] = 100 · (1 − C_p / C_agnostic)``; the candidate ships only if
    ``reduction[candidate] − reduction[best simple] ≥ margin``; the shipped policy is retired if
    its reduction is below ``retire_below``.
    """
    if not jobs:
        raise TraceError("the workload is empty")
    trace.validate()
    if candidate in (AGNOSTIC, ORACLE, *SIMPLE_POLICIES):
        raise ValueError(f"'{candidate}' is a reference policy, not a candidate")
    m = margin_pp() if margin is None else float(margin)
    retire = retire_below_pct() if retire_below is None else float(retire_below)
    if m < 0 or retire < 0:
        raise ValueError("margin and retirement threshold must be non-negative")

    names = [AGNOSTIC, *SIMPLE_POLICIES, candidate, ORACLE]
    totals: dict[str, float] = {}
    for name in names:
        policy = resolve_policy(name)
        total = 0.0
        for job in jobs:
            if job.duration < 1 or job.energy_kwh < 0 or job.slack < 0:
                raise TraceError(f"job {job.id!r}: duration >= 1, energy and slack >= 0")
            region, start = policy(job, trace)
            total += emissions_g(job, trace, region, start)
        totals[name] = total
    base = totals[AGNOSTIC]
    if base <= 0:
        raise TraceError("carbon-agnostic emissions are zero — no benefit can be measured")
    reduction = {n: 100.0 * (1.0 - totals[n] / base) for n in names}
    best_simple = max(SIMPLE_POLICIES, key=lambda n: (reduction[n], n))
    advantage = reduction[candidate] - reduction[best_simple]
    decision = "candidate" if advantage >= m else "simple"
    shipped = candidate if decision == "candidate" else best_simple
    shipped_benefit = reduction[shipped]
    notes: list[str] = []
    if trace.synthetic:
        notes.append("synthetic trace — demonstrates the method, gates nothing")
    st = trace.signal_type()
    if st != "decision":
        notes.append(
            f"trace method '{trace.method}' gives a signal of type '{st}'; ADR 0112 decides on marginal "
            "(decision) signals, so the measured reductions are accounting-basis figures"
        )
    return Evaluation(
        candidate=candidate,
        emissions_g={n: round(v, 6) for n, v in totals.items()},
        reduction_pct={n: round(v, 4) for n, v in reduction.items()},
        best_simple=best_simple,
        margin_pp=m,
        advantage_pp=round(advantage, 4),
        decision=decision,
        shipped=shipped,
        shipped_benefit_pct=round(shipped_benefit, 4),
        retire_below_pct=retire,
        retired=shipped_benefit < retire,
        headroom_pct=round(reduction[ORACLE], 4),
        jobs=len(jobs),
        horizon_h=trace.horizon,
        regions=trace.regions,
        trace_digest=trace_digest(jobs, trace),
        trace_method=trace.method,
        signal_type=st,
        synthetic=trace.synthetic,
        evaluated_at=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
        notes=notes,
    )


def record_evaluation(ev: Evaluation, actor: str | None = None) -> None:
    """Chain the evaluation into the audit log — the evidence a policy's eligibility rests on."""
    from examlops.data.audit import write_audit_event

    write_audit_event("finops", actor, EVALUATION_ACTION, ev.candidate, ev.as_dict())


def list_evaluations(candidate: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
    """Recorded evaluations, newest first (read back from the audit chain)."""
    from examlops.data import get_db, init_db

    init_db()
    q = "SELECT id, ts, actor, target, details FROM audit_events WHERE action = ?"
    params: list[Any] = [EVALUATION_ACTION]
    if candidate:
        q += " AND target = ?"
        params.append(candidate)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    out: list[dict[str, Any]] = []
    with get_db() as conn:
        for r in conn.execute(q, tuple(params)).fetchall():
            try:
                d = json.loads(r["details"] or "{}")
            except ValueError:
                continue
            d["event_id"] = r["id"]
            d["recorded_by"] = r["actor"]
            out.append(d)
    return out


# ── the runtime gate (R-ec + R-ed) ────────────────────────────────────────────


@dataclass
class GateDecision:
    """What placement may do with a carbon-weighing policy.

    ``action``: ``allow`` (use it as requested), ``substitute`` (run :data:`RUNTIME_SIMPLE`
    instead), ``withhold`` (run the requested policy with the carbon input removed — its other
    objectives still apply) or ``agnostic`` (the capability is retired: no carbon input at all).
    """

    requested: str
    action: str
    reason: str
    evaluation_event: int | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _age_days(evaluated_at: str, now: datetime) -> float:
    try:
        ts = datetime.fromisoformat(evaluated_at)
    except (TypeError, ValueError):
        return math.inf
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds() / 86400.0


def gate_decision(
    policy: str,
    *,
    carbon_primary: bool,
    evaluations: Sequence[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
    mode: str | None = None,
    cadence_days: float | None = None,
) -> GateDecision:
    """Decide whether ``policy`` may weigh carbon. Pure given ``evaluations`` (newest first).

    Synthetic evaluations are ignored: a demonstration trace proves nothing about a real grid.
    Absence of an evaluation is not eligibility (the rule ADR 0111 applies to judges).
    """
    now = now or datetime.now(UTC)
    cadence = retest_days() if cadence_days is None else float(cadence_days)
    mode = mode or gate_mode()
    real = [e for e in (evaluations or []) if not e.get("synthetic")]
    only_synthetic = bool(evaluations) and not real
    fallback = "substitute" if carbon_primary else "withhold"

    def decide(action: str, reason: str, ev: Mapping[str, Any] | None = None) -> GateDecision:
        notes: list[str] = []
        if mode == "warn" and action != "allow":
            notes.append(f"gate in warn mode — would have applied: {action} ({reason})")
            action, reason = "allow", f"allowed by EXAMLOPS_CARBON_POLICY_GATE=warn: {reason}"
        return GateDecision(policy, action, reason, (ev or {}).get("event_id"), notes)

    if policy == RUNTIME_SIMPLE:
        # The simple baseline is what R-ec ships by default, so it needs no win to run — but R-ed
        # still retires it when the latest measurement says it no longer pays.
        latest = real[0] if real else None
        if latest is None:
            return GateDecision(
                policy,
                "allow",
                "simple baseline; no measurement recorded yet",
                notes=["unmeasured — run: exa finops carbon policy evaluate"],
            )
        simple_benefit = float(latest["reduction_pct"].get(latest["best_simple"], 0.0))
        if simple_benefit < float(latest.get("retire_below_pct", retire_below_pct())):
            return decide(
                "agnostic",
                f"retired (R-ed): the simple baseline saved {simple_benefit:.2f}% < "
                f"{latest.get('retire_below_pct')}% at the last measurement",
                latest,
            )
        age = _age_days(str(latest.get("evaluated_at")), now)
        notes = [] if age <= cadence else [f"re-test overdue (R-ed): measured {age:.0f} days ago"]
        return GateDecision(policy, "allow", "simple baseline", latest.get("event_id"), notes)

    mine = [e for e in real if e.get("candidate") == policy]
    if not mine:
        suffix = (
            " (only synthetic evaluations on record — they gate nothing)" if only_synthetic else ""
        )
        return decide(
            fallback, f"no R-ec evaluation of '{policy}' against the simple baselines{suffix}"
        )
    latest = mine[0]
    if latest.get("retired"):
        return decide(
            "agnostic",
            f"retired (R-ed): the shipped policy saved {latest.get('shipped_benefit_pct')}% < "
            f"{latest.get('retire_below_pct')}%",
            latest,
        )
    age = _age_days(str(latest.get("evaluated_at")), now)
    if age > cadence:
        return decide(
            fallback,
            f"re-test overdue (R-ed): last evaluated {age:.0f} days ago > {cadence:g}",
            latest,
        )
    if latest.get("decision") != "candidate":
        return decide(
            fallback,
            f"does not beat '{latest.get('best_simple')}' by the declared margin "
            f"({latest.get('advantage_pp')} pp < {latest.get('margin_pp')} pp) — the simple "
            "policy ships (R-ec)",
            latest,
        )
    return GateDecision(
        policy,
        "allow",
        f"beats '{latest.get('best_simple')}' by {latest.get('advantage_pp')} pp "
        f"(margin {latest.get('margin_pp')} pp)",
        latest.get("event_id"),
    )


def placement_gate(policy: str, *, carbon_primary: bool) -> GateDecision:
    """:func:`gate_decision` over the recorded evaluations. A failure to read them is recorded as
    a reason and treated as *no evaluation* — never as a pass."""
    try:
        evaluations = list_evaluations(None if policy == RUNTIME_SIMPLE else policy, limit=20)
    except Exception as exc:  # noqa: BLE001
        d = gate_decision(policy, carbon_primary=carbon_primary, evaluations=[])
        d.notes.append(f"could not read evaluations: {type(exc).__name__}: {exc}")
        return d
    return gate_decision(policy, carbon_primary=carbon_primary, evaluations=evaluations)


# ── trace I/O ─────────────────────────────────────────────────────────────────


def load_trace(data: Mapping[str, Any]) -> tuple[list[Job], Trace]:
    """``{"method": …, "regions": {name: [g/kWh…]}, "jobs": [{id, submit, duration, energy_kwh,
    slack?, regions?, home?}], "synthetic"?: bool}`` → (jobs, trace)."""
    try:
        trace = Trace(
            intensity={str(k): [float(x) for x in v] for k, v in dict(data["regions"]).items()},
            method=str(data.get("method") or "average_grid_mix"),
            synthetic=bool(data.get("synthetic", False)),
        )
        jobs = [
            Job(
                id=str(j["id"]),
                submit=int(j["submit"]),
                duration=int(j["duration"]),
                energy_kwh=float(j["energy_kwh"]),
                slack=int(j.get("slack", 0)),
                regions=tuple(j["regions"]) if j.get("regions") else None,
                home=j.get("home"),
            )
            for j in data["jobs"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise TraceError(f"malformed trace: {exc}") from exc
    trace.validate()
    return jobs, trace


def synthetic_trace(*, days: int = 14, jobs: int = 60, seed: int = 7) -> dict[str, Any]:
    """A deterministic demonstration trace: three regions with a daily solar dip, one of them
    cleaner on average, and a mix of rigid and flexible jobs. Marked ``synthetic`` — evaluations
    over it are recorded as such and never gate placement."""
    import random

    rng = random.Random(seed)
    hours = days * 24
    profiles = {"north": (120.0, 60.0), "central": (280.0, 110.0), "south": (340.0, 180.0)}
    regions: dict[str, list[float]] = {}
    for name, (mean, swing) in profiles.items():
        regions[name] = [
            round(
                max(
                    5.0,
                    mean
                    + swing * math.cos(2 * math.pi * ((h % 24) - 13) / 24)
                    + rng.gauss(0, mean * 0.08),
                ),
                2,
            )
            for h in range(hours)
        ]
    job_list = []
    for i in range(jobs):
        duration = rng.choice([1, 2, 4, 8])
        job_list.append(
            {
                "id": f"job-{i:03d}",
                "submit": rng.randrange(24, hours - duration - 25),
                "duration": duration,
                "energy_kwh": round(duration * rng.uniform(0.3, 2.4), 3),
                "slack": rng.choice([0, 0, 4, 12, 24]),
                "home": rng.choice(["central", "south"]),
            }
        )
    return {"method": "average_grid_mix", "synthetic": True, "regions": regions, "jobs": job_list}
