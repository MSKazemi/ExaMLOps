"""Prometheus text-format exposition of metrics the platform records itself.

**The gap this closes spans three ADRs.** The platform ingests SLIs into `slo_samples` (ADR 0023
clause 3) and records vector build/query latency into `vector_metrics` (ADR 0020 clause 5), and
neither is visible to Prometheus. That has a consequence beyond dashboards: `exa slo generate`
emits **burn-rate alert rules that range over a Prometheus series**, so for any SLI the platform
ingests itself — the `c2` eval, `c5` drift and `c8` fairness sources — the alert can never fire.
ADR 0025's clause 3 asks for "a C6 fairness SLI **and alert**"; the SLI landed in iteration 18 and
the alert had nowhere to fire from.

**Why a textfile rather than an HTTP endpoint.** Prometheus' node_exporter textfile collector is
the standard path for metrics produced by batch jobs and CLIs, and it needs no long-lived process:
`exa` writes a `.prom` file, node_exporter serves it. The control plane's own `/metrics` reads a
*different* database (its approval store) and is the wrong owner for platform state. Adding a
second long-lived service to publish rows a CLI can write is cost without a reason.

Everything here is a pure formatter over already-recorded rows: no scraping, no timers, no state.
"""

from __future__ import annotations

from typing import Any

#: Written before each metric family. Prometheus tolerates their absence; a reader does not.
_HELP: dict[str, tuple[str, str]] = {
    "examlops_slo_sli": ("gauge", "Current SLI ratio (good/total) for a declared SLO"),
    "examlops_slo_target": ("gauge", "Objective ratio the SLO is held to"),
    "examlops_slo_budget_remaining": (
        "gauge",
        "Fraction of the error budget still available (1 = untouched, <0 = exhausted)",
    ),
    "examlops_slo_burn_rate": ("gauge", "Observed error over allowed error"),
    "examlops_slo_samples": ("gauge", "Samples the SLI was computed from"),
    "examlops_slo_measured": (
        "gauge",
        "1 when the SLO has samples, 0 when it has none — an unmeasured SLO is not a healthy one",
    ),
    "examlops_vector_latency_ms": (
        "gauge",
        "Most recent latency of a vector-store operation, in milliseconds",
    ),
    "examlops_vector_items": ("gauge", "Items touched by the most recent vector-store operation"),
}


def _escape(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: dict[str, Any]) -> str:
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(pairs.items()) if v is not None)
    return f"{{{inner}}}" if inner else ""


def render(samples: list[tuple[str, dict[str, Any], float]]) -> str:
    """Render ``(metric, labels, value)`` triples as Prometheus text format.

    `# HELP`/`# TYPE` are emitted **once per family**, before its first sample, which is what the
    format requires — repeating them per sample makes the file unparseable rather than merely
    verbose.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for metric, labels, value in samples:
        if metric not in seen:
            seen.add(metric)
            kind, help_text = _HELP.get(metric, ("gauge", metric))
            lines.append(f"# HELP {metric} {help_text}")
            lines.append(f"# TYPE {metric} {kind}")
        lines.append(f"{metric}{_labels(labels)} {float(value)}")
    return "\n".join(lines) + ("\n" if lines else "")


def slo_samples(model: str | None = None, tenant: str = "default") -> list[tuple]:
    """SLO gauges for every declared SLO, or for one model.

    **An unmeasured SLO exports `measured=0` and no SLI.** Publishing its placeholder 1.0 would
    put a perfect ratio on a dashboard for something nobody has measured, and a burn-rate alert
    cannot fire on a perfect ratio — the same trap the control plane's `/metrics` was fixed for.
    """
    from examlops.data.governance import list_slo_specs
    from examlops.slo import slo_status

    models = [model] if model else sorted({str(s["model"]) for s in list_slo_specs(tenant=tenant)})
    out: list[tuple] = []
    for name in models:
        for status in slo_status(name, tenant=tenant):
            labels = {"model": status.model, "slo": status.name, "tenant": status.tenant}
            out.append(("examlops_slo_target", labels, status.target))
            out.append(("examlops_slo_measured", labels, 1.0 if status.measured else 0.0))
            out.append(("examlops_slo_samples", labels, float(status.n)))
            if status.measured:
                out.append(("examlops_slo_sli", labels, status.sli))
                out.append(("examlops_slo_budget_remaining", labels, status.budget_remaining))
                out.append(("examlops_slo_burn_rate", labels, status.burn_rate))
    return out


def vector_samples(tenant: str | None = None) -> list[tuple]:
    """Latest latency/item-count per (collection, operation) — ADR 0020 clause 5.

    The **latest** row per series, not an average: `vector_metrics` is an append-only log, and a
    gauge that averaged a collection's whole history would move less and less as the log grew,
    which is the opposite of what an operator watching a reindex needs.
    """
    from examlops.data.data_assets import latest_vector_metrics

    out: list[tuple] = []
    for row in latest_vector_metrics(tenant):
        labels = {
            "collection": row["collection"],
            "tenant": row["tenant"],
            "operation": row["operation"],
        }
        out.append(("examlops_vector_latency_ms", labels, float(row["latency_ms"])))
        out.append(("examlops_vector_items", labels, float(row["item_count"])))
    return out


def export(model: str | None = None, tenant: str = "default") -> str:
    """Everything the platform can publish, as one Prometheus text-format document.

    Each source is collected independently and a failing one is skipped rather than emptying the
    file: a textfile collector that vanishes takes every series with it, and losing the SLO
    gauges because the vector table is unreadable would be a worse outage than the one it reports.
    """
    samples: list[tuple] = []
    for collect in (lambda: slo_samples(model, tenant), lambda: vector_samples(tenant)):
        try:
            samples.extend(collect())
        except Exception:  # noqa: BLE001 - one unreadable source must not blank the export
            continue
    return render(samples)
