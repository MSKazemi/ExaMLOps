"""One SLOSpec per (servable, kind): the objective shape is decided by kind (ADR 0148 decision 3).

============ ==========================================================  ======================
Kind         Objectives (all thresholds are inclusive: exactly-at passes)  Burn signal
============ ==========================================================  ======================
predictive   ``latency_p99_ms`` (max), ``error_rate`` (max),              multi-window burn
             ``availability`` (min); at least one
generative   ``pair`` (a declared TTFT/TPOT pair, p99 - see                goodput below target;
             :mod:`examlops.slo.pairs`), ``goodput_target`` (min)          TTFT includes queue
agentic      ``task_success`` (min; Wilson lower bound, calibrated         success lower bound
             ``judge``), ``jct_p50_s``/``jct_p95_s`` (max),                below target; JCT burn
             ``intervention_rate`` (max), ``cost_per_task_p95_usd`` (max)
============ ==========================================================  ======================

The generative kind *references* the paired record rather than duplicating it: the thresholds,
``tight`` and the percentile stay in ``slo_pairs`` (one source of truth) and the pair evaluator does
the arithmetic; a spec adds the goodput target on top.

Semantics, stated once:

* **Absence is not a pass.** A dimension with fewer than ``min_samples`` valid observations, a judge
  without a passing calibration, a referenced pair that no longer exists, or a kind whose telemetry
  the platform does not keep, is ``no_verdict`` with the reason - never ``met``. A spec is ``met``
  only when *every* declared objective is ``met``; any ``violated`` makes it ``violated``.
* **Malformed observations** (missing / negative / non-finite) are excluded and counted; they never
  silently count as good.
* The promotion gate (``gates: {slo: ...}``) refuses unless the verdict is ``met``.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Any

from examlops.slo.pairs import PairSLO, load_pair, percentile
from examlops.slo.pairs import evaluate as evaluate_pair

KINDS = ("predictive", "generative", "agentic")
_MET, _VIOLATED, _NO_VERDICT = "met", "violated", "no_verdict"
DEFAULT_WINDOW_DAYS = 30.0

# kind -> {field: (type, lower, upper, lower_inclusive, upper_inclusive)}; str fields use None.
_NUM: dict[str, dict[str, tuple[float, float, bool, bool]]] = {
    "predictive": {
        "latency_p99_ms": (0.0, math.inf, False, True),
        "error_rate": (0.0, 1.0, True, False),
        "availability": (0.0, 1.0, False, True),
    },
    "generative": {"goodput_target": (0.0, 1.0, False, True)},
    "agentic": {
        "task_success": (0.0, 1.0, False, True),
        "jct_p50_s": (0.0, math.inf, False, True),
        "jct_p95_s": (0.0, math.inf, False, True),
        "intervention_rate": (0.0, 1.0, True, False),
        "cost_per_task_p95_usd": (0.0, math.inf, False, True),
    },
}
_STR: dict[str, tuple[str, ...]] = {"generative": ("pair",), "agentic": ("judge",)}


class SLOSpecError(ValueError):
    """An invalid spec definition."""


def min_samples() -> int:
    """``EXAMLOPS_SLO_SPEC_MIN_SAMPLES`` (default 20): fewest valid observations a verdict rests on."""
    try:
        return max(1, int(os.getenv("EXAMLOPS_SLO_SPEC_MIN_SAMPLES", "20")))
    except ValueError:
        return 20


def verdict_max_age_hours() -> float:
    """``EXAMLOPS_SLO_SPEC_VERDICT_MAX_AGE_HOURS`` (default 168): how long a recorded verdict counts."""
    try:
        return max(0.0, float(os.getenv("EXAMLOPS_SLO_SPEC_VERDICT_MAX_AGE_HOURS", "168")))
    except ValueError:
        return 168.0


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# -- validation ------------------------------------------------------------------------------


def validate(
    servable: str, kind: str, fields: dict[str, Any], *, tenant: str = "default"
) -> dict[str, Any]:
    """Return the normalized objectives for ``kind`` or raise :class:`SLOSpecError`.

    ``fields`` maps objective name -> value; ``None`` values are treated as not supplied.
    """
    if not servable or not servable.strip():
        raise SLOSpecError("servable is required")
    if kind not in KINDS:
        raise SLOSpecError(f"kind must be one of {'|'.join(KINDS)} (got {kind!r})")
    given = {k: v for k, v in fields.items() if v is not None}
    allowed = {**_NUM[kind], **dict.fromkeys(_STR.get(kind, ()))}
    unknown = sorted(set(given) - set(allowed))
    if unknown:
        raise SLOSpecError(
            f"{kind} does not take {', '.join(unknown)}; a {kind} spec is: {', '.join(allowed)}"
        )
    out: dict[str, Any] = {}
    for name, (lo, hi, lo_in, hi_in) in _NUM[kind].items():
        if name not in given:
            continue
        v = given[name]
        if not _finite(v):
            raise SLOSpecError(f"{name} must be a number (got {v!r})")
        if (v < lo if lo_in else v <= lo) or (v > hi if hi_in else v >= hi):
            left = "[" if lo_in else "("
            right = "]" if hi_in and math.isfinite(hi) else (")" if math.isfinite(hi) else "∞)")
            bound = f"{lo:g}, {hi:g}" if math.isfinite(hi) else f"{lo:g}, "
            raise SLOSpecError(f"{name} must be in {left}{bound}{right} (got {v!r})")
        out[name] = float(v)
    for name in _STR.get(kind, ()):
        if name in given:
            v = given[name]
            if not isinstance(v, str) or not v.strip():
                raise SLOSpecError(f"{name} must be a non-empty string (got {v!r})")
            out[name] = v.strip()
    if kind == "predictive" and not out:
        raise SLOSpecError(
            "a predictive spec needs at least one of latency_p99_ms, error_rate, availability"
        )
    if kind == "generative":
        if "pair" not in out:
            raise SLOSpecError(
                "a generative spec needs pair=<name> of a declared TTFT/TPOT pair "
                "(exa slo pair-set)"
            )
        pair = load_pair(servable, out["pair"], tenant)
        if pair is None:
            raise SLOSpecError(
                f"no paired SLO {out['pair']!r} declared for {servable} in tenant {tenant!r} "
                "(exa slo pair-set)"
            )
        if pair.percentile != 99.0:
            raise SLOSpecError(
                f"pair {out['pair']!r} is p{pair.percentile:g}; the generative spec is p99 "
                "(ttft_p99/tpot_p99)"
            )
    if kind == "agentic":
        if "task_success" not in out:
            raise SLOSpecError("an agentic spec needs task_success (the target success rate)")
        if "judge" not in out:
            raise SLOSpecError(
                "an agentic spec needs judge=<name>: task success is judged, and only a "
                "calibrated judge may gate (ADR 0111)"
            )
        if "jct_p50_s" in out and "jct_p95_s" in out and out["jct_p50_s"] > out["jct_p95_s"]:
            raise SLOSpecError("jct_p50_s must not exceed jct_p95_s")
    return out


# -- verdicts --------------------------------------------------------------------------------


@dataclass
class Dimension:
    name: str
    verdict: str
    threshold: Any = None
    observed: Any = None
    n: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class SpecVerdict:
    servable: str
    kind: str
    verdict: str
    spec_version: int | None = None
    basis: str = "live"  # live | samples | recorded
    dimensions: list[Dimension] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    recorded_id: int | None = None

    @property
    def passed(self) -> bool:
        """Gate view: only ``met`` passes. ``no_verdict`` does not."""
        return self.verdict == _MET

    def as_dict(self) -> dict[str, Any]:
        return {
            "servable": self.servable,
            "kind": self.kind,
            "verdict": self.verdict,
            "passed": self.passed,
            "spec_version": self.spec_version,
            "basis": self.basis,
            "reasons": self.reasons,
            "recorded_id": self.recorded_id,
            "dimensions": [d.__dict__ for d in self.dimensions],
        }


def _aggregate(dims: list[Dimension]) -> str:
    if any(d.verdict == _VIOLATED for d in dims):
        return _VIOLATED
    if dims and all(d.verdict == _MET for d in dims):
        return _MET
    return _NO_VERDICT


def _valid(values: Any, *, lo: float = 0.0) -> list[float]:
    if not isinstance(values, list):
        return []
    return [float(v) for v in values if _finite(v) and v >= lo]


def _upper(
    name: str, thr: float, values: list[float], pct: float, need: int, unit: str
) -> Dimension:
    """A max-threshold objective on the nearest-rank ``pct`` of ``values``."""
    if len(values) < need:
        return Dimension(
            name,
            _NO_VERDICT,
            thr,
            n=len(values),
            reasons=[f"{name}: {len(values)} valid observation(s), need at least {need}"],
        )
    p = percentile(values, pct)
    ok = p <= thr
    return Dimension(
        name,
        _MET if ok else _VIOLATED,
        thr,
        p,
        len(values),
        [] if ok else [f"{name}: p{pct:g} {p:g}{unit} > {thr:g}{unit}"],
    )


def _ratio(name: str, thr: float, num: float, den: float, need: int, *, at_most: bool) -> Dimension:
    if not (_finite(num) and _finite(den)) or den < need or num < 0 or num > den:
        return Dimension(
            name,
            _NO_VERDICT,
            thr,
            n=int(den) if _finite(den) and den >= 0 else 0,
            reasons=[f"{name}: {den:g} valid observation(s), need at least {need}"]
            if _finite(den) and den >= 0
            else [f"{name}: no valid observations"],
        )
    r = num / den
    ok = r <= thr if at_most else r >= thr
    sign = ">" if at_most else "<"
    return Dimension(
        name,
        _MET if ok else _VIOLATED,
        thr,
        r,
        int(den),
        [] if ok else [f"{name}: {r:g} {sign} {thr:g}"],
    )


def _predictive(spec: dict[str, Any], obs: dict[str, Any], need: int) -> list[Dimension]:
    dims: list[Dimension] = []
    if "latency_p99_ms" in spec:
        dims.append(
            _upper(
                "latency_p99_ms",
                spec["latency_p99_ms"],
                _valid(obs.get("latency_ms")),
                99.0,
                need,
                " ms",
            )
        )
    if "error_rate" in spec:
        dims.append(
            _ratio(
                "error_rate",
                spec["error_rate"],
                _num(obs.get("errors")),
                _num(obs.get("requests")),
                need,
                at_most=True,
            )
        )
    if "availability" in spec:
        dims.append(
            _ratio(
                "availability",
                spec["availability"],
                _num(obs.get("availability_good")),
                _num(obs.get("availability_total")),
                need,
                at_most=False,
            )
        )
    return dims


def _num(v: Any) -> float:
    return float(v) if _finite(v) else math.nan


def _generative(
    spec: dict[str, Any], obs: dict[str, Any], need: int, pair: PairSLO | None
) -> list[Dimension]:
    if pair is None:
        return [
            Dimension(
                "pair",
                _NO_VERDICT,
                spec.get("pair"),
                reasons=[f"the referenced pair {spec.get('pair')!r} is not declared"],
            )
        ]
    samples = obs.get("samples")
    pv = evaluate_pair(pair, samples if isinstance(samples, list) else [], min_samples=need)
    if pv.verdict == _NO_VERDICT:
        reasons = list(pv.reasons)
        if not samples:
            reasons.append("the platform does not persist TTFT/TPOT: supply samples or record")
        return [
            Dimension("ttft_p99_ms", _NO_VERDICT, pair.ttft_ms, n=pv.n, reasons=reasons),
            Dimension("tpot_p99_ms", _NO_VERDICT, pair.tpot_ms, n=pv.n, reasons=reasons),
        ]
    dims = [
        Dimension(
            "ttft_p99_ms",
            _MET if pv.ttft_ok else _VIOLATED,
            pair.ttft_ms,
            pv.ttft_pct_ms,
            pv.n,
            [] if pv.ttft_ok else [r for r in pv.reasons if r.startswith("TTFT")],
        ),
        Dimension(
            "tpot_p99_ms",
            _MET if pv.tpot_ok else _VIOLATED,
            pair.tpot_ms,
            pv.tpot_pct_ms,
            pv.n,
            [] if pv.tpot_ok else [r for r in pv.reasons if r.startswith("TPOT")],
        ),
    ]
    if "goodput_target" in spec:
        thr = spec["goodput_target"]
        att = float(pv.attainment or 0.0)
        ok = att >= thr
        dims.append(
            Dimension(
                "goodput",
                _MET if ok else _VIOLATED,
                thr,
                att,
                pv.n,
                [] if ok else [f"goodput: {att:g} < {thr:g}"],
            )
        )
    return dims


def _agentic(
    spec: dict[str, Any],
    obs: dict[str, Any],
    need: int,
    judge_eligible: tuple[bool, list[str]] | None,
) -> list[Dimension]:
    from examlops.evaluation.calibration import wilson_interval

    tasks = [t for t in (obs.get("tasks") or []) if isinstance(t, dict)]
    dims: list[Dimension] = []
    thr = spec["task_success"]
    judged = [t["success"] for t in tasks if isinstance(t.get("success"), bool)]
    if judge_eligible is None or not judge_eligible[0]:
        why = (
            f"judge {spec['judge']!r} has no calibration"
            if judge_eligible is None
            else f"judge {spec['judge']!r} is not gate-eligible: " + ", ".join(judge_eligible[1])
        )
        dims.append(Dimension("task_success", _NO_VERDICT, thr, n=len(judged), reasons=[why]))
    elif len(judged) < need:
        dims.append(
            Dimension(
                "task_success",
                _NO_VERDICT,
                thr,
                n=len(judged),
                reasons=[
                    f"task_success: {len(judged)} judged task(s), need at least {need} "
                    "(the platform does not record a judged outcome: supply samples or record)"
                ],
            )
        )
    else:
        k = sum(1 for s in judged if s)
        lo, hi = wilson_interval(k, len(judged))
        ok = lo >= thr
        dims.append(
            Dimension(
                "task_success",
                _MET if ok else _VIOLATED,
                thr,
                {"rate": k / len(judged), "lower": lo, "upper": hi},
                len(judged),
                [] if ok else [f"task_success: Wilson lower bound {lo:.4f} < {thr:g}"],
            )
        )
    for key, pct, fld in (("jct_p50_s", 50.0, "jct_s"), ("jct_p95_s", 95.0, "jct_s")):
        if key in spec:
            vals = _valid([t.get(fld) for t in tasks])
            dims.append(_upper(key, spec[key], vals, pct, need, " s"))
    if "intervention_rate" in spec:
        flags = [t["intervened"] for t in tasks if isinstance(t.get("intervened"), bool)]
        dims.append(
            _ratio(
                "intervention_rate",
                spec["intervention_rate"],
                float(sum(1 for f in flags if f)),
                float(len(flags)),
                need,
                at_most=True,
            )
        )
    if "cost_per_task_p95_usd" in spec:
        vals = _valid([t.get("cost_usd") for t in tasks])
        dims.append(
            _upper("cost_per_task_p95_usd", spec["cost_per_task_p95_usd"], vals, 95.0, need, " USD")
        )
    return dims


def evaluate(
    kind: str,
    spec: dict[str, Any],
    obs: dict[str, Any],
    *,
    pair: PairSLO | None = None,
    judge_eligible: tuple[bool, list[str]] | None = None,
    min_samples_: int | None = None,
    servable: str = "",
) -> SpecVerdict:
    """Pure evaluation of ``obs`` against ``spec``; no I/O."""
    need = min_samples() if min_samples_ is None else max(1, min_samples_)
    if kind == "predictive":
        dims = _predictive(spec, obs, need)
    elif kind == "generative":
        dims = _generative(spec, obs, need, pair)
    elif kind == "agentic":
        dims = _agentic(spec, obs, need, judge_eligible)
    else:
        raise SLOSpecError(f"unknown kind {kind!r}")
    reasons = [r for d in dims for r in d.reasons]
    return SpecVerdict(servable, kind, _aggregate(dims), dimensions=dims, reasons=reasons)


# -- store-backed operations -----------------------------------------------------------------


def set_spec(
    servable: str,
    kind: str,
    fields: dict[str, Any],
    *,
    tenant: str = "default",
    actor: str | None = None,
) -> tuple[str, int, dict[str, Any]]:
    """Validate and store; returns ``(state, version, objectives)``. Raises :class:`SLOSpecError`."""
    spec = validate(servable, kind, fields, tenant=tenant)
    from examlops.data import slo_kind_specs as store

    state, version = store.put(servable, kind, tenant, spec, actor=actor)
    return state, version, spec


def observe(servable: str, kind: str, tenant: str, window_days: float) -> tuple[dict, list[str]]:
    """Live observations from the platform's own ledgers + notes on what is not recorded."""
    from examlops.data import slo_kind_specs as store

    notes: list[str] = []
    if kind == "predictive":
        if tenant != "default":
            notes.append("gateway_calls carries no tenant, so live latency/error data is not used")
            obs: dict[str, Any] = {}
        else:
            obs = store.gateway_observations(servable, window_days)
        obs.update(store.availability_observations(servable, tenant, window_days))
        return obs, notes
    if kind == "agentic":
        notes.append("task success and intervention are not recorded by the platform")
        return store.agent_session_observations(servable, tenant, window_days), notes
    notes.append("TTFT/TPOT samples are not persisted by the platform")
    return {}, notes


def check_spec(
    servable: str,
    kind: str,
    *,
    tenant: str = "default",
    samples: Any = None,
    window_days: float = DEFAULT_WINDOW_DAYS,
    record: bool = False,
    actor: str | None = None,
    min_samples_: int | None = None,
) -> SpecVerdict:
    """Evaluate the stored spec; a spec that is not declared yields ``no_verdict`` (never ``met``).

    ``samples`` replaces the live observations: a dict of observations for predictive/agentic, or a
    list (of ``(ttft_ms, tpot_ms)`` for generative, of per-task records for agentic).
    """
    from examlops.data import slo_kind_specs as store

    row = store.get(servable, kind, tenant)
    if row is None:
        return SpecVerdict(
            servable,
            kind,
            _NO_VERDICT,
            reasons=[f"no {kind} SLOSpec declared for {servable} (exa slo spec set)"],
        )
    spec = row["spec"]
    notes: list[str] = []
    if samples is not None:
        obs = _shape_samples(kind, samples)
        basis = "samples"
    else:
        obs, notes = observe(servable, kind, tenant, window_days)
        basis = "live"
    pair = load_pair(servable, spec["pair"], tenant) if kind == "generative" else None
    eligible = None
    if kind == "agentic":
        eligible = _judge_eligibility(spec["judge"])
    verdict = evaluate(
        kind,
        spec,
        obs,
        pair=pair,
        judge_eligible=eligible,
        min_samples_=min_samples_,
        servable=servable,
    )
    verdict.spec_version = int(row["version"])
    verdict.basis = basis
    if verdict.verdict == _NO_VERDICT:
        verdict.reasons += [n for n in notes if n not in verdict.reasons]
    if record:
        verdict.recorded_id = store.record_verdict(
            servable,
            kind,
            tenant,
            verdict.spec_version,
            verdict.verdict,
            verdict.as_dict(),
            source=basis,
            actor=actor,
        )
    return verdict


def _shape_samples(kind: str, samples: Any) -> dict[str, Any]:
    if kind == "generative":
        if not isinstance(samples, list):
            raise SLOSpecError("generative samples must be a JSON list of TTFT/TPOT pairs")
        return {"samples": samples}
    if kind == "agentic":
        tasks = samples.get("tasks") if isinstance(samples, dict) else samples
        if not isinstance(tasks, list):
            raise SLOSpecError("agentic samples must be a JSON list of task records")
        return {"tasks": tasks}
    if not isinstance(samples, dict):
        raise SLOSpecError(
            "predictive samples must be a JSON object: latency_ms[], requests, errors, "
            "availability_good, availability_total"
        )
    return samples


def _judge_eligibility(judge: str) -> tuple[bool, list[str]] | None:
    """``None`` when the judge has never been measured (absence is not eligibility)."""
    from examlops.evaluation.calibration import is_gate_eligible

    ok, failures = is_gate_eligible(judge)
    if not ok and failures == ["no_calibration"]:
        return None
    return ok, failures


def _fresh_recorded(servable: str, kind: str, tenant: str, version: int) -> dict[str, Any] | None:
    from examlops.data import slo_kind_specs as store

    rec = store.latest_verdict(servable, kind, tenant)
    if rec is None or int(rec["spec_version"]) != version:
        return None
    if rec["verdict"] not in (_MET, _VIOLATED):
        return None
    if time.time() - float(rec["ts"]) > verdict_max_age_hours() * 3600.0:
        return None
    return rec


def gate_verdicts(
    servable: str,
    *,
    tenant: str = "default",
    kinds: tuple[str, ...] | None = None,
    window_days: float = DEFAULT_WINDOW_DAYS,
) -> list[SpecVerdict]:
    """The effective verdict of each declared spec of ``servable``.

    Live evidence first; when it cannot decide, the latest *recorded* verdict counts if it is for
    the current spec version and no older than ``EXAMLOPS_SLO_SPEC_VERDICT_MAX_AGE_HOURS``.
    """
    from examlops.data import slo_kind_specs as store

    out: list[SpecVerdict] = []
    for row in store.list_specs(servable=servable, tenant=tenant):
        if kinds and row["kind"] not in kinds:
            continue
        v = check_spec(servable, row["kind"], tenant=tenant, window_days=window_days)
        if v.verdict == _NO_VERDICT:
            rec = _fresh_recorded(servable, row["kind"], tenant, int(row["version"]))
            if rec is not None:
                v = SpecVerdict(
                    servable,
                    row["kind"],
                    rec["verdict"],
                    spec_version=int(row["version"]),
                    basis="recorded",
                    dimensions=v.dimensions,
                    reasons=[f"recorded verdict #{rec['id']} ({rec['source']})"]
                    if rec["verdict"] == _MET
                    else list(rec["evidence"].get("reasons", [])),
                    recorded_id=int(rec["id"]),
                )
        out.append(v)
    return out


def promotion_decision(
    servable: str,
    *,
    tenant: str = "default",
    kinds: tuple[str, ...] | None = None,
    require_spec: bool = True,
    sink: dict[str, Any] | None = None,
) -> Any:
    """The ``slo`` gate's decision: allow only when a spec exists and every verdict is ``met``.

    ``require_spec=False`` (the agent-version road) allows when no spec of ``kinds`` exists.
    Any failure to evaluate denies: an unmeasurable SLO is not a met one. ``sink`` receives the
    verdicts for the caller's evidence trail.
    """
    from examlops.policy_engine import slo_gate

    try:
        verdicts = gate_verdicts(servable, tenant=tenant, kinds=kinds)
    except Exception as exc:  # noqa: BLE001 - fail closed
        return slo_gate(servable, [f"SLO could not be evaluated: {exc}"], tenant=tenant)
    if sink is not None:
        sink["slo"] = [v.as_dict() for v in verdicts]
    if not verdicts:
        if not require_spec:
            return slo_gate(servable, [], tenant=tenant)
        return slo_gate(
            servable,
            [f"no SLOSpec is declared for {servable} (exa slo spec set)"],
            tenant=tenant,
        )
    reasons = [
        f"{v.kind} SLO {v.verdict}"
        + (f" ({v.basis})" if v.basis != "live" else "")
        + (": " + "; ".join(v.reasons) if v.reasons else "")
        for v in verdicts
        if not v.passed
    ]
    return slo_gate(servable, reasons, tenant=tenant)
