"""Next-Gen 40 · C6 — model-quality SLOs/SLIs & burn-rate alerting (ADR 0023).

A declarative model-quality SLO layer on top of the existing Prometheus/Alertmanager
stack. OpenSLO-style YAML → generated Prometheus recording rules + error-budget series
→ multi-window/multi-burn-rate alerts (Sloth-style). Also computes live SLO status and
remaining error budget from `slo_samples`, and exposes `budget_exhausted` so the C3
promotion gate can block on an exhausted budget.

Graceful degradation: rule generation is pure string/dict assembly (no Prometheus
required); `slo_status`/`budget_exhausted` read `platform_db` samples. If PyYAML is
present, specs load from YAML; otherwise pass dict specs directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from examlops import data as platform_db

# Multi-window burn-rate config (Google SRE workbook 2-window defaults for a 30d SLO).
# (short_window, long_window, burn_rate_factor, severity, for_duration)
BURN_RATE_WINDOWS = [
    ("5m", "1h", 14.4, "critical", "2m"),  # fast burn — pages
    ("30m", "6h", 6.0, "critical", "15m"),
    ("2h", "1d", 3.0, "warning", "1h"),
    ("6h", "3d", 1.0, "warning", "3h"),  # slow burn — ticket
]


@dataclass
class SLOStatus:
    model: str
    name: str
    tenant: str
    target: float
    sli: float  # observed good/total ratio
    budget_total: float  # 1 - target (allowed error fraction)
    budget_remaining: float  # fraction of the error budget left (0..1); <0 => exhausted
    burn_rate: float  # current error rate / allowed error rate
    ok: bool | None  # sli >= target; None when nothing has been measured
    n: int
    measured: bool  # n > 0 — whether any of the numbers above rest on evidence

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "name": self.name,
            "tenant": self.tenant,
            "target": self.target,
            "sli": self.sli,
            "budget_total": self.budget_total,
            "budget_remaining": self.budget_remaining,
            "burn_rate": self.burn_rate,
            "ok": self.ok,
            "n": self.n,
            "measured": self.measured,
        }


@dataclass
class PrometheusRules:
    groups: list[dict[str, Any]] = field(default_factory=list)

    def to_yaml(self) -> str:
        try:
            import yaml

            return yaml.safe_dump(
                {"groups": self.groups}, sort_keys=False, default_flow_style=False
            )
        except Exception:  # pragma: no cover - PyYAML expected in this repo
            import json

            return json.dumps({"groups": self.groups}, indent=2)


def _slug(*parts: str) -> str:
    return ":".join(p.replace(" ", "_").replace("-", "_") for p in parts if p)


def generate_rules(slo_spec: dict[str, Any]) -> PrometheusRules:
    """Generate promtool-valid recording + multi-window burn-rate rules (R2/R3).

    ``slo_spec`` fields: ``model``, ``name``, ``target`` (0..1), ``window`` (e.g. 30d),
    ``sli_query`` (a PromQL expression that yields the *good-events* ratio in 0..1), and
    optional ``tenant``. The generated group contains:

    - a recording rule for the SLI ratio,
    - a recording rule for the error budget (``1 - sli``),
    - one burn-rate alert per multi-window pair (fast burn pages; slow burn tickets), each
      requiring the recorded error ratio to exceed the threshold over *both* windows.
    """
    model = slo_spec["model"]
    name = slo_spec["name"]
    tenant = slo_spec.get("tenant", "default")
    target = float(slo_spec["target"])
    window = slo_spec.get("window", "30d")
    sli_query = slo_spec.get("sli_query") or (f'examlops:sli_ratio{{model="{model}",slo="{name}"}}')
    budget = round(1.0 - target, 6)

    prefix = _slug("examlops:slo", model, name)
    labels = {"model": model, "slo": name, "tenant": tenant}

    recording = {
        "name": _slug("examlops_slo", model, name, "records"),
        "rules": [
            {
                "record": f"{prefix}:sli_ratio",
                "expr": sli_query,
                "labels": labels,
            },
            {
                "record": f"{prefix}:error_ratio",
                "expr": f"1 - ({sli_query})",
                "labels": labels,
            },
            {
                "record": f"{prefix}:error_budget",
                "expr": str(budget),
                "labels": labels,
            },
        ],
    }

    # Burn-rate alerts range over the *recorded* error ratio, not over ``sli_query`` itself.
    # PromQL can only subscript a selector, so a range over an arbitrary ratio expression is not
    # expressible — which is how both windows once ended up as the same string, leaving every
    # alert as ``(X > t) and (X > t)``: a single-window alert with a two-window name and a
    # two-window annotation. The recording rule above publishes the ratio as a series precisely
    # so both windows can be taken from it.
    error_series = f"{prefix}:error_ratio"

    alert_rules = []
    for short_w, long_w, factor, severity, for_dur in BURN_RATE_WINDOWS:
        # Burn-rate alert: error rate over BOTH windows exceeds factor * budget. The short window
        # makes it responsive; the long one is what stops a momentary spike from paging.
        threshold = round(factor * budget, 6)
        expr = (
            f"(avg_over_time({error_series}[{short_w}]) > {threshold}) "
            f"and (avg_over_time({error_series}[{long_w}]) > {threshold})"
        )
        alert_rules.append(
            {
                "alert": _slug("SLO", model, name, "BurnRate", short_w),
                "expr": expr,
                "for": for_dur,
                "labels": {**labels, "severity": severity, "burn_rate": str(factor)},
                "annotations": {
                    "summary": f"{model}/{name} error budget burning {factor}x ({short_w}/{long_w})",
                    "description": (
                        f"SLO {name} for {model} is consuming its {window} error budget "
                        f"at {factor}x over {short_w}+{long_w}."
                    ),
                },
            }
        )

    return PrometheusRules(
        groups=[
            recording,
            {"name": _slug("examlops_slo", model, name, "alerts"), "rules": alert_rules},
        ]
    )


def slo_status(model: str, name: str | None = None, *, tenant: str = "default") -> list[SLOStatus]:
    """Live SLO status: SLI, remaining error budget, and burn rate (R5).

    Reads accumulated ``slo_samples`` per SLO for the model. ``budget_remaining`` is the
    fraction of the allowed error budget still available (1.0 = untouched, <0 = exhausted).
    ``burn_rate`` is observed-error / allowed-error.
    """
    specs = platform_db.list_slo_specs(model=model, tenant=tenant)
    if name:
        specs = [s for s in specs if s["name"] == name]
    out: list[SLOStatus] = []
    for spec in specs:
        good, total = platform_db.slo_sli_ratio(model, spec["name"], tenant=tenant)
        # With no samples this ratio has no value to report. It is left at 1.0 so the numeric
        # fields keep their shape for existing readers, but `measured` is what says whether any
        # of them rest on evidence — without it, "we have not looked" and "it is perfect" are
        # the same number, and every reader downstream published the second one.
        measured = total > 0
        sli = (good / total) if total else 1.0
        target = float(spec["target"])
        budget_total = 1.0 - target
        observed_error = 1.0 - sli
        if budget_total <= 0:
            budget_remaining = 1.0 if observed_error <= 0 else 0.0
            burn_rate = 0.0 if observed_error <= 0 else float("inf")
        else:
            budget_remaining = 1.0 - (observed_error / budget_total)
            burn_rate = observed_error / budget_total
        out.append(
            SLOStatus(
                model=model,
                name=spec["name"],
                tenant=tenant,
                target=target,
                sli=sli,
                budget_total=budget_total,
                budget_remaining=budget_remaining,
                burn_rate=burn_rate,
                ok=(sli >= target) if measured else None,
                n=int(total),
                measured=measured,
            )
        )
    return out


def budget_exhausted(model: str, slo: str, *, tenant: str = "default") -> bool:
    """True if the SLO's error budget is spent — used by the C3 promotion gate (R6).

    An *unmeasured* SLO is not exhausted, which is literally true and is why this stays ``False``.
    It is not evidence of health either, and a caller that reads ``False`` as "checked and fine"
    is wrong about a gate-flagged SLO with no samples — see :func:`unmeasured_gates`, which the
    promote path reports so that gap is visible rather than silent.
    """
    statuses = slo_status(model, slo, tenant=tenant)
    if not statuses:
        return False
    return statuses[0].measured and statuses[0].budget_remaining <= 0.0


def unmeasured_gates(model: str, *, tenant: str = "default") -> list[str]:
    """Gate-flagged SLOs for ``model`` that have no samples, so the gate cannot evaluate them.

    The operator asked these specifically to block promotion on a spent error budget. With no
    data the gate passes every time, and used to do so without saying anything — indistinguishable
    from a gate that ran and found the budget intact.

    They are reported, not enforced: a model cannot produce SLI samples before it serves and
    cannot serve before it is promoted, so refusing here would deadlock every model's first
    promotion.
    """
    return [
        s.name
        for s in slo_status(model, tenant=tenant)
        if not s.measured
        and any(
            spec["name"] == s.name and spec["gate_promotion"]
            for spec in platform_db.list_slo_specs(model=model, tenant=tenant)
        )
    ]


def apply_spec(spec: dict[str, Any]) -> None:
    """Persist one SLO spec (versioned, per-tenant) (R1/R7)."""
    platform_db.upsert_slo_spec(
        spec["model"],
        spec["name"],
        tenant=spec.get("tenant", "default"),
        sli_source=spec.get("sli_source", "prometheus"),
        sli_query=spec.get("sli_query"),
        target=float(spec["target"]),
        window=spec.get("window", "30d"),
        higher_is_better=bool(spec.get("higher_is_better", True)),
        gate_promotion=bool(spec.get("gate_promotion", False)),
    )


def load_specs(path: str) -> list[dict[str, Any]]:
    """Load OpenSLO-style specs from a YAML file (R1). Returns the parsed spec list."""
    import yaml

    with open(path) as fh:
        doc = yaml.safe_load(fh)
    if isinstance(doc, dict) and "slos" in doc:
        return list(doc["slos"])
    if isinstance(doc, list):
        return doc
    return [doc]


__all__ = [
    "SLOStatus",
    "PrometheusRules",
    "generate_rules",
    "slo_status",
    "budget_exhausted",
    "unmeasured_gates",
    "apply_spec",
    "load_specs",
]
