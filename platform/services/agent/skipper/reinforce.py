"""Procedural reinforcement (T4, Phase 7, ADR 0106).

Closes the outcome→behaviour loop: with the reactive loop now instrumented (Phase 2), tool
success rates are real, so a stored procedure whose steps rely on a chronically-failing tool
should stop being recalled. This module reads ``examlops.data.agent`` tool stats and flips the
``deprecated`` flag on such procedures (``memory_types.recall`` already filters deprecated
procedures out, so a deprecated procedure silently drops out of retrieval).

Pure over the memory store + ``platform.db`` — no LLM. Every deprecation is audited.
"""

from __future__ import annotations

from typing import Any

from skipper import config, memory_types


def failing_tools(*, threshold: float, min_calls: int) -> set[str]:
    """Tools with a success rate below ``threshold`` over at least ``min_calls`` recorded calls."""
    try:
        from examlops.data.agent import tool_success_rate

        rows = tool_success_rate()
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for r in rows:
        calls = int(r.get("calls", 0) or 0)
        if calls < min_calls:
            continue
        rate = r.get("success_rate")
        if rate is None:
            rate = (int(r.get("ok", 0) or 0) / calls) if calls else 1.0
        if float(rate) < threshold:
            out.add(r["tool"])
    return out


def deprecate_failing_procedures(
    store: Any,
    *,
    threshold: float | None = None,
    min_calls: int | None = None,
) -> list[str]:
    """Deprecate procedures whose steps reference a chronically-failing tool. Returns keys touched."""
    if store is None:
        return []
    threshold = config.AGENT_PROC_DEPRECATE_THRESHOLD if threshold is None else threshold
    min_calls = config.AGENT_PROC_DEPRECATE_MIN_CALLS if min_calls is None else min_calls
    fails = failing_tools(threshold=threshold, min_calls=min_calls)
    if not fails:
        return []

    deprecated: list[str] = []
    for it in memory_types.list_kind(store, memory_types.KIND_PROC, limit=1000):
        data = dict(it.value.get("data", {}))
        if data.get("deprecated"):
            continue
        text = " ".join(data.get("steps", [])).lower()
        if any(f.lower() in text for f in fails):
            data["deprecated"] = True
            store.put(it.namespace, it.key, {**it.value, "data": data})
            scope = it.namespace[1] if len(it.namespace) > 1 else None
            memory_types.audit_memory_op(
                "memory_deprecate", memory_types.KIND_PROC, scope, config.AGENT_ACTOR, it.key, None
            )
            deprecated.append(it.key)
    return deprecated
