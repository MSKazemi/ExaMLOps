"""Per-kind unit economics over the ledgers the platform already keeps (ADR 0148 decision 4).

The cost unit is decided by workload kind, and the kinds are **never summed** into one number:
a generative call made by an agent is in both the gateway ledger and the agent-session ledger,
so a cross-kind total would double count. Each kind is reported on its own unit:

* ``predictive`` -- per **prediction**. No inference cost is metered, so the USD unit cost is
  ``None`` and the energy per prediction is reported only from rows that carry a kWh figure.
* ``generative`` -- per **1k tokens** and per **successful call**, from ``gateway_calls``;
  the reasoning share of output comes from ``reasoning_usage`` when it has rows.
* ``agentic`` -- per **task**. Only the *model-call* component is metered. Sandbox-seconds,
  idle-state GB-hours and hot-pool standby share are not, so the per-task figure is labelled a
  **lower bound** and ``complete`` is ``False`` -- it is never presented as the full task cost.

Absent is not zero: with fewer than :func:`min_samples` outcomes a unit cost is ``None`` with a
reason, and an unmetered component is listed as such rather than as ``0``.
"""

from __future__ import annotations

import datetime
import os
from typing import Any

from examlops.data import economics as ledger

KINDS = ("predictive", "generative", "agentic")

#: Components of an agent task's cost (ADR 0148 d4) and whether the platform meters them.
AGENT_COST_COMPONENTS: dict[str, bool] = {
    "model_calls": True,
    "tool_calls": False,  # counted, but a tool call carries no cost figure
    "sandbox_seconds": False,
    "idle_state_gb_hours": False,
    "hot_pool_standby_share": False,
}


def min_samples() -> int:
    """``EXAMLOPS_ECONOMICS_MIN_SAMPLES`` (default 5): fewest outcomes a unit cost rests on."""
    try:
        return max(1, int(os.getenv("EXAMLOPS_ECONOMICS_MIN_SAMPLES", "5")))
    except ValueError:
        return 5


def since_from_days(days: float | None, now: datetime.datetime | None = None) -> str | None:
    if days is None:
        return None
    if days <= 0:
        raise ValueError("days must be positive")
    end = now or datetime.datetime.now(datetime.UTC)
    return (end - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _unit(num: float | None, den: int, n: int, floor: int, scale: float = 1.0) -> dict[str, Any]:
    """A unit cost, or ``None`` plus the reason it is not stated."""
    if n == 0:
        return {"value": None, "reason": "no_data"}
    if n < floor:
        return {"value": None, "reason": f"insufficient_samples ({n} < {floor})"}
    if num is None or den <= 0:
        return {"value": None, "reason": "not_metered"}
    return {"value": num / den * scale, "reason": None}


def predictive(since: str | None, floor: int) -> dict[str, Any]:
    d = ledger.predictive_ledger(since)
    n = d["predictions"]
    kwh = d["kwh"] if d["energy_requests"] else None
    return {
        "kind": "predictive",
        "unit": "prediction",
        "n": n,
        "cost_usd": None,
        "cost_per_unit_usd": {"value": None, "reason": "no_inference_cost_metered"},
        "kwh_per_unit": _unit(kwh, d["energy_requests"], d["energy_requests"], floor),
        "co2e_g_per_unit": _unit(
            d["co2e_g"] if d["energy_requests"] else None,
            d["energy_requests"],
            d["energy_requests"],
            floor,
        ),
        "complete": False,
        "notes": [
            "inference cost and energy are not metered per prediction unless "
            "inference_energy rows exist"
        ],
    }


def generative(since: str | None, floor: int) -> dict[str, Any]:
    d = ledger.generative_ledger(since)
    tokens = d["prompt_tokens"] + d["completion_tokens"]
    ok = d["calls"] - d["errors"]
    out_cost = d["reasoning_cost"] + d["reasoning_output_cost"]
    share = None
    if d["reasoning_rows"] and d["reasoning_tokens"] + d["reasoning_output_tokens"] > 0:
        share = d["reasoning_tokens"] / (d["reasoning_tokens"] + d["reasoning_output_tokens"])
    return {
        "kind": "generative",
        "unit": "token",
        "n": d["calls"],
        "errors": d["errors"],
        "tokens": tokens,
        "cost_usd": d["cost_usd"] if d["calls"] else None,
        "cost_per_unit_usd": _unit(d["cost_usd"], tokens, d["calls"], floor, 1000.0),
        "cost_per_success_usd": _unit(d["cost_usd"], ok, d["calls"], floor),
        "reasoning_token_share": share,
        "reasoning_cost_usd": out_cost if d["reasoning_rows"] else None,
        "complete": True,
        "notes": ["cost_per_unit_usd is per 1000 tokens (prompt + completion)"],
    }


def agentic(since: str | None, floor: int) -> dict[str, Any]:
    d = ledger.agentic_ledger(since)
    n = d["tasks"]
    return {
        "kind": "agentic",
        "unit": "task",
        "n": n,
        "open_sessions": d["sessions"] - n,
        "succeeded": d["succeeded"],
        "success_rate": (d["succeeded"] / n) if n else None,
        "cost_usd": d["model_cost_usd"] if n else None,
        "cost_per_unit_usd": _unit(d["model_cost_usd"], n, n, floor),
        "cost_per_success_usd": _unit(d["model_cost_usd"], d["succeeded"], n, floor),
        "cost_bound": "lower",
        "components": [{"component": k, "metered": v} for k, v in AGENT_COST_COMPONENTS.items()],
        "complete": all(AGENT_COST_COMPONENTS.values()),
        "tool_calls": d["tool_calls"],
        "notes": [
            "model-call cost only: sandbox-seconds, idle-state GB-hours and hot-pool standby "
            "are not metered, so this is a lower bound on the real per-task cost"
        ],
    }


_BUILDERS = {"predictive": predictive, "generative": generative, "agentic": agentic}


def economics(kind: str | None = None, days: float | None = None) -> dict[str, Any]:
    """The per-kind rollup. ``kind=None`` returns all three, side by side, never summed."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    since = since_from_days(days)
    floor = min_samples()
    kinds = [kind] if kind else list(KINDS)
    return {
        "since": since,
        "min_samples": floor,
        "kinds": [_BUILDERS[k](since, floor) for k in kinds],
        "note": "kinds are reported separately and never summed: an agent's model calls also "
        "appear in the gateway ledger",
    }
