"""SM2 — long-term memory tools (store-backed).

Let Skipper recall learned procedures / past incidents / operator preferences and
record new ones. Built natively on the injected LangGraph ``SqliteStore`` (no langmem
dep). These tools are added to the graph **only when a store is available** (see
``skipper.graph``), so ``store`` is always a real store when they run.

Write-gating + audit are added in SM3 (ADR 0034). For now preference writes are direct
(low-risk) and procedure writes are marked for gating.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedStore
from langgraph.store.base import BaseStore
from langgraph.types import interrupt

from skipper import config, memory_types
from skipper.confirm import WRITE_TOOLS, _is_affirmative
from skipper.memory_gate import retrieval_allowed

_KIND_LABEL = {
    "proc": "learned procedure",
    "episode": "past incident",
    "pref": "operator preference",
    "kb": "knowledge-base fact",
}


def _format_hits(hits: list) -> str:
    if not hits:
        return "No relevant memories found."
    lines = []
    for h in hits:
        kind = h.value.get("kind", "?")
        label = _KIND_LABEL.get(kind, kind)
        lines.append(f"- [{label}] {h.value.get('text', '')}")
    return "\n".join(lines)


@tool
def recall_memory(
    query: str,
    kind: str = "proc",
    *,
    store: Annotated[BaseStore, InjectedStore],
) -> str:
    """Search Skipper's long-term memory for relevant learned knowledge.

    Use this before planning a non-trivial operation to reuse what worked before.
    Do NOT use it for current platform state (model versions, drift, cost, audit) —
    query those live with the dedicated tools instead.

    Args:
        query: What to look for, in natural language.
        kind: 'proc' (procedures), 'episode' (past incidents), 'pref' (operator
            preferences), or 'kb' (stable facts). Defaults to 'proc'.
    """
    if kind not in memory_types.KINDS:
        return f"Unknown memory kind '{kind}'. Expected one of: {', '.join(memory_types.KINDS)}."
    allowed, reason = retrieval_allowed(query, kind)
    if not allowed:
        return reason
    return _format_hits(memory_types.recall(store, kind, query))


@tool
def remember_preference(
    topic: str,
    value: str,
    *,
    store: Annotated[BaseStore, InjectedStore],
) -> str:
    """Remember an operator preference for future sessions.

    Examples: default canary percentage, preferred dataset for a model, verbosity,
    risk tolerance ("never auto-promote").

    Args:
        topic: What the preference is about (e.g. 'canary percentage').
        value: The preferred value (e.g. '5%').
    """
    memory_types.record_preference(store, topic, value, operator=config.AGENT_ACTOR)
    return f"Noted preference for {config.AGENT_ACTOR}: {topic} = {value}"


@tool
def record_procedure(
    task_class: str,
    steps: list[str],
    success_conditions: str = "",
    *,
    store: Annotated[BaseStore, InjectedStore],
) -> str:
    """Save a reusable operational procedure learned from a successful run.

    Only record procedures that actually succeeded. Gated behind operator
    confirmation (SM3, ADR 0034) unless AGENT_MEMORY_REQUIRE_CONFIRM=false. When
    AGENT_MEMORY_REVIEW_QUEUE is set, the write is queued for batch operator review
    instead (approve/reject via `exa agent memory review`).

    Args:
        task_class: The kind of task (e.g. 'safe-promote', 'drift-response').
        steps: Ordered steps of the procedure.
        success_conditions: How to tell the procedure succeeded (optional).
    """
    if config.AGENT_MEMORY_REVIEW_QUEUE:
        from skipper.memory_review import enqueue

        rid = enqueue(task_class, steps, success_conditions, operator=config.AGENT_ACTOR)
        return (
            f"Queued procedure for '{task_class}' as review #{rid} — pending operator approval "
            f"(`exa agent memory review approve {rid}`)."
        )
    if config.AGENT_MEMORY_REQUIRE_CONFIRM:
        # Exclude the injected store from the interrupt payload — it is not
        # JSON-serialisable and must not enter the checkpoint.
        decision = interrupt(
            {
                "action": "record_procedure",
                "summary": f"Save a reusable procedure for '{task_class}' ({len(steps)} steps)",
                "args": {"task_class": task_class, "steps": steps},
            }
        )
        if not _is_affirmative(decision):
            return "Cancelled — procedure not saved."
    memory_types.record_procedure(
        store, task_class, steps, success_conditions, operator=config.AGENT_ACTOR
    )
    return f"Recorded procedure for '{task_class}' ({len(steps)} steps)."


# record_procedure gates on confirmation like the other write tools.
WRITE_TOOLS.add("record_procedure")

TOOLS = [recall_memory, remember_preference, record_procedure]
