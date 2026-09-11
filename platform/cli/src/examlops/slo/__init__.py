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

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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


# ── Breach auditing (ADR 0023 clause 5) ──────────────────────────────────────


def _is_breached(model: str, name: str, tenant: str) -> bool:
    st = slo_status(model, name, tenant=tenant)
    return bool(st) and st[0].measured and st[0].budget_remaining <= 0.0


def record_sample(
    model: str,
    name: str,
    good: float,
    total: float,
    *,
    tenant: str = "default",
    source: str = "exa-slo",
    watermark: str | None = None,
) -> bool:
    """Record one SLI interval and audit the moment an SLO **breaks**. Returns True if it just did.

    Every path that adds SLI data goes through here rather than calling
    ``record_slo_sample`` directly, because the breach is only visible as a *difference*: the
    sample that spends the last of an error budget looks exactly like the thousand before it, and
    the status read afterwards cannot tell you when it happened or what caused it.

    ADR 0023 clause 5 asks for the breach to be audited and it was the one third of the clause not
    built — `exa slo status` would show an exhausted budget with no D4 record of it ever having
    been spent, which is precisely the event a governance layer exists to keep.

    **On transition only.** Auditing every sample recorded while a budget is already spent would
    write one row per interval for as long as the breach lasts, and an audit trail that grows
    without new information is one nobody reads. Recovery is not audited here: a budget that
    refills is a rolling-window artefact, not a decision anyone made.
    """
    from examlops.data.audit import write_audit_event
    from examlops.data.governance import record_slo_sample

    before = _is_breached(model, name, tenant)
    record_slo_sample(model, name, good, total, tenant=tenant, watermark=watermark)
    after = _is_breached(model, name, tenant)
    if after and not before:
        st = slo_status(model, name, tenant=tenant)[0]
        try:
            write_audit_event(
                source=source,
                actor=os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER"),
                action="slo_breached",
                target=f"{model}/{name}",
                details={
                    "sli": round(st.sli, 6),
                    "target": st.target,
                    "budget_remaining": round(st.budget_remaining, 6),
                    "burn_rate": round(st.burn_rate, 6) if st.burn_rate != float("inf") else None,
                    "samples": st.n,
                },
                tenant=tenant,
            )
        except Exception:
            # An unwritable audit log must not swallow the measurement that was just taken —
            # losing the sample would also lose the breach.
            pass
        return True
    return False


# ── SLI ingestion (ADR 0023 clause 3) ────────────────────────────────────────

#: Sources this platform can supply SLI data for today, and why the others cannot yet.
#: Named explicitly rather than silently recording nothing: a spec whose source yields no samples
#: reads downstream as *unmeasured*, and "we have no ingester for this" and "the system is healthy"
#: must not be the same observation.
UNSUPPORTED_SOURCES = {
    "availability": "no serving-availability probe is persisted anywhere in platform_db",
    "prometheus": "needs a live Prometheus; use `exa slo generate` to emit recording rules "
    "and let Prometheus evaluate them there",
}

#: Sources with an ingester. Named so an unrecognised value is reported as a **typo** rather than
#: as "unknown source" — `c5` was in the ADR and not in this module, and the message a user got
#: ("unknown sli_source 'c5'") said the source did not exist rather than that it was unbuilt.
SUPPORTED_SOURCES = ("c1", "c2", "c5", "c8")

#: How many recorded drift verdicts one c5 ingest looks back over. Bounded so a long-lived
#: model's SLI reflects its recent behaviour rather than its whole history — an SLO is a
#: statement about a rolling window, and the window's own length lives on the spec.
_DRIFT_WINDOW = 200


_LATENCY_QUERY = re.compile(r"^latency(?:_ms)?\s*<=\s*(\d+(?:\.\d+)?)$")
_WINDOW = re.compile(r"^(\d+)([mhdw])$")
_WINDOW_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _window_start(window: str) -> str | None:
    """The UTC timestamp a spec's rolling ``window`` (``30d``, ``24h``, ``2w``) begins at, in
    ``gateway_calls.ts``'s own format — or None when the window is not in that form."""
    m = _WINDOW.match((window or "").strip())
    if not m:
        return None
    start = datetime.now(UTC) - timedelta(seconds=int(m.group(1)) * _WINDOW_UNIT[m.group(2)])
    return start.strftime("%Y-%m-%d %H:%M:%S")


#: What an event-sourced ingester returns: ``(good, total, watermark)`` for the events it has
#: not counted before, or a string saying why it could not count any.
Increment = tuple[float, float, str] | str


def _after(spec: dict[str, Any], tenant: str, table: str) -> int | None:
    """The id of the last ``table`` event an earlier ingest of this SLO counted, or None.

    ADR 0023 clause 3. `slo_status` **sums** an SLO's samples, so an ingest that re-recorded its
    whole window every run counted the same events once per run: 120 real gateway calls read as
    620 after six daily ingests, and a fresh outage was diluted by recounting a good month (SLI
    0.948 against a true 0.817). Each ingest now counts only events past this mark. A mark from a
    different table (the spec's source was changed) is no mark.
    """
    from examlops.data.governance import slo_last_watermark

    mark = slo_last_watermark(spec["model"], spec["name"], tenant=tenant)
    prefix, _, value = (mark or "").partition(":")
    return int(value) if prefix == table and value.isdigit() else None


def _up_to_date(what: str, mark: int | None) -> str:
    return f"up to date — no {what} since the last ingest (watermark id {mark})"


def _c1_samples(model: str, spec: dict[str, Any], tenant: str) -> Increment:
    """Good/total for a gateway latency or error SLO (ADR 0023 clause 3, the C1 GenAI source).

    ``--query latency_ms<=800`` — the share of **successful** calls answered within 800 ms (a
    failed call belongs to the error SLI, the usual SRE split). ``--query errors`` — the share of
    calls that did not fail. Only calls that carry a measurement count, over the spec's own
    ``window``; a query is required, as for `c2`, because a guessed default is how an SLO ends up
    measuring something nobody chose.
    """
    from examlops.data.gateway import gateway_call_sli

    query = (spec.get("sli_query") or "").strip().lower().replace(" ", "")
    if query in ("errors", "error_rate", "success"):
        latency_max = None
    elif m := _LATENCY_QUERY.match(query):
        latency_max = float(m.group(1))
    else:
        return (
            "c1 needs --query `latency_ms<=<ms>` for a latency SLO or `errors` for an error SLO"
            + (f" (got '{spec.get('sli_query')}')" if query else "")
        )
    since = _window_start(spec.get("window") or "30d")
    if since is None:
        return f"window '{spec.get('window')}' is not of the form <n>m|h|d|w (e.g. 30d)"
    mark = _after(spec, tenant, "gateway_calls")
    good, total, last = gateway_call_sli(
        model, since, latency_ms_max=latency_max, after_id=mark or 0
    )
    if total == 0:
        if mark is not None:
            return _up_to_date("measured gateway calls", mark)
        return (
            f"no measured gateway calls for {model} in the last {spec.get('window') or '30d'}"
            + (" that succeeded" if latency_max is not None else "")
            + " — calls recorded before latency was measured carry none"
        )
    return (float(good), float(total), f"gateway_calls:{last}")


def _c5_samples(model: str, spec: dict[str, Any], tenant: str) -> Increment:
    """Good/total for a drift SLO, from recorded drift **verdicts** (ADR 0023 clause 3, c5).

    One recorded ``drift_events`` row is one evaluation the real detectors already made, so
    ``good = severity OK`` over ``total = evaluations`` is a proportion without a second copy of
    the threshold rule anywhere. Deriving it instead from raw ``drift_snapshots`` would mean
    re-implementing the scoring the drift provider owns, and the SLI would drift from
    `exa drift status` the moment a provider is swapped.

    ``--query`` optionally pins one ``drift_kind``; without it every kind counts.

    **What this does not cover:** prediction drift, which `exa drift status` computes on the fly
    and never records, so it contributes no events. An SLO here measures the kinds that persist a
    verdict — concept, label and feature drift.
    """
    kind = (spec.get("sli_query") or "").strip() or None
    mark = _after(spec, tenant, "drift_events")
    events = platform_db.list_drift_events(model=model, drift_kind=kind, last_n=_DRIFT_WINDOW)
    if mark is not None:
        events = [e for e in events if int(e.get("id") or 0) > mark]
        if not events:
            return _up_to_date("drift verdicts", mark)
    if not events:
        return (
            f"no drift_events rows for {model}"
            + (f" of kind '{kind}'" if kind else "")
            + " — prediction drift is computed live and records none; run a detector "
            "(`exa drift concept|label|feature`) to persist verdicts"
        )
    total = len(events)
    good = sum(1 for e in events if str(e.get("severity", "")).upper() == "OK")
    return (float(good), float(total), f"drift_events:{max(int(e['id']) for e in events)}")


def _c8_samples(model: str, spec: dict[str, Any], tenant: str) -> tuple[float, float] | str:
    """Good/total for a fairness SLO (ADR 0025 clause 3 — the C6 fairness SLI).

    ``good`` counts the model's declared slice attributes whose disparity is within threshold;
    ``total`` counts the ones that could actually be **measured**. An attribute whose slices are
    all below the min-sample guard has no disparity, and counting it as good would let a model
    with no data score a perfect fairness SLI — unmeasured and healthy must not be the same
    observation, which is the rule the rest of this module already follows.
    """
    from examlops.fairness import effective_fairness_config, fairness_report

    cfg, source = effective_fairness_config(model)
    if not cfg:
        return (
            f"{model} declares no slice registry — add a `fairness:` block to its model YAML "
            "or run `exa fairness config`"
        )
    results = fairness_report(model, tenant=tenant)
    measured = [r for r in results if r.demographic_parity_diff is not None or r.accuracy_range]
    if not measured:
        return (
            f"none of {model}'s {len(cfg['slice_attrs'])} declared slice attribute(s) has enough "
            f"samples to measure a disparity (min_samples={cfg['min_samples']}, registry "
            f"source={source})"
        )
    good = sum(1 for r in measured if not r.disparity_exceeded)
    return (float(good), float(len(measured)))


def _c2_samples(model: str, spec: dict[str, Any], tenant: str) -> Increment:
    """Good/total for an eval-quality SLO, or a string saying why it could not be derived.

    An `eval_suite_results` row is already a proportion over a known sample size, which is the
    one shape an SLI needs — so this source needs no arithmetic beyond turning the stored rate
    back into a count.
    """
    query = (spec.get("sli_query") or "").strip()
    if not query:
        return (
            "c2 needs --query naming the eval metric (e.g. `pass_rate`, or `suite:metric` "
            "to pin one suite); without it there is no way to know which of a model's metrics "
            "this SLO is about"
        )
    suite, _, metric = query.rpartition(":")
    rows = [
        r
        for r in platform_db.get_eval_results(model)
        if r["metric"] == metric and (not suite or r["suite"] == suite)
    ]
    if not rows:
        return f"no eval_suite_results rows for metric '{metric}'" + (
            f" in suite '{suite}'" if suite else ""
        )
    mark = _after(spec, tenant, "eval_suite_results")
    if mark is None:
        fresh = rows[:1]  # a first ingest starts from the newest result, not the whole history
    else:
        fresh = [r for r in rows if int(r.get("id") or 0) > mark]
        if not fresh:
            return _up_to_date(f"'{metric}' results", mark)
    good = total = 0.0
    for row in fresh:
        n = int(row.get("sample_size") or 0)
        if n <= 0:
            return f"the newest '{metric}' result records no sample_size, so it is not a proportion"
        score = float(row["score"])
        if not 0.0 <= score <= 1.0:
            return (
                f"'{metric}' is {score}, which is not a ratio — an SLI must be good/total, so a "
                "unit-bearing metric (latency, tokens, cost) cannot back an SLO directly"
            )
        good += round(score * n)
        total += n
    return (good, total, f"eval_suite_results:{max(int(r['id']) for r in fresh)}")


def ingest_slis(model: str, *, tenant: str = "default") -> list[dict[str, Any]]:
    """Pull SLI samples for ``model`` from the platform's own telemetry (ADR 0023 clause 3).

    Until this existed every SLI arrived by hand through ``exa slo record``, so an SLO measured
    whatever someone remembered to type — and the specs already carried an ``sli_source`` column
    that nothing ever read.

    Returns one row per spec describing what happened, including the ones that were skipped and
    why. Reporting the skips is the point: a source with no ingester records no samples, which
    downstream is indistinguishable from a healthy service that simply has not been asked.
    """
    out: list[dict[str, Any]] = []
    for spec in platform_db.list_slo_specs(model=model, tenant=tenant):
        source = (spec.get("sli_source") or "prometheus").strip().lower()
        row: dict[str, Any] = {"name": spec["name"], "source": source}
        if source in UNSUPPORTED_SOURCES:
            out.append({**row, "ingested": False, "reason": UNSUPPORTED_SOURCES[source]})
            continue
        result: tuple[float, float, str] | tuple[float, float] | str
        if source == "c1":
            result = _c1_samples(model, spec, tenant)
        elif source == "c2":
            result = _c2_samples(model, spec, tenant)
        elif source == "c5":
            result = _c5_samples(model, spec, tenant)
        elif source == "c8":
            result = _c8_samples(model, spec, tenant)
        else:
            result = (
                f"unrecognised sli_source '{source}' — expected one of "
                f"{sorted({*SUPPORTED_SOURCES, *UNSUPPORTED_SOURCES})}"
            )
        if isinstance(result, str):
            done = result.startswith("up to date")
            out.append({**row, "ingested": False, "up_to_date": done, "reason": result})
            continue
        # Event sources return a watermark so the next ingest counts only newer events; c8 is a
        # point-in-time measurement (how many declared attributes are within threshold *now*), so
        # each ingest is one legitimate sample of it and carries none.
        good, total = result[0], result[1]
        watermark = result[2] if len(result) == 3 else None
        breached = record_sample(
            model,
            spec["name"],
            good,
            total,
            tenant=tenant,
            source="exa-slo-ingest",
            watermark=watermark,
        )
        out.append({**row, "ingested": True, "good": good, "total": total, "breached": breached})
    return out


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
