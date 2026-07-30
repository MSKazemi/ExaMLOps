"""Monitoring / baseline memory accessors (T3, Phase 6, ADR 0104).

"What's normal for model X" — a read-only view over the platform's recorded baselines (drift,
input-embedding drift, cost envelope, SLO specs). These are **pointers into `platform.db`, never
copies** (ADR 0033 §5): the agent recalls the *recorded normal* to reason about a deviation, but
must always confirm the *current* value with the live drift/cost tools. Everything degrades to
empty when a table is missing.
"""

from __future__ import annotations

from typing import Any


def whats_normal(model: str) -> dict[str, Any]:
    """Return the recorded 'normal' baselines for a model (drift/input/cost/SLO).

    Read-only; each field is best-effort and independently degrades to ``None``/``[]``.
    """
    out: dict[str, Any] = {"model": model}
    try:
        from examlops.data import init_db
        from examlops.data.drift import get_drift_baseline, get_input_baseline
        from examlops.data.finops import get_model_costs
        from examlops.data.governance import list_slo_specs

        init_db()
        out["drift_baseline"] = _safe(get_drift_baseline, model)
        out["input_baseline"] = _safe(get_input_baseline, model)
        costs = _safe(get_model_costs, model) or []
        out["recent_cost"] = costs[:5]
        out["slo_specs"] = _safe(list_slo_specs, model=model) or []
    except Exception as exc:  # noqa: BLE001 - baselines are best-effort
        out["error"] = str(exc)
    return out


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        return None
