"""BL-007 — consume the platform's MCP tools directly (single source of truth).

The platform already defines its agent-callable capabilities *once*, in
``examlops.mcp.tools`` — the same registry the ``exa mcp serve`` MCP server exposes to any
external agent. This bridge wraps those tool functions as LangChain tools so the in-repo
Skipper agent can consume the **same** definitions instead of maintaining a parallel set that
can drift.

Opt-in via ``AGENT_USE_MCP_TOOLS``. Write (mutating) tools stay gated by the MCP layer's own
``EXAMLOPS_MCP_ALLOW_WRITES`` — this bridge never widens that gate.
"""

from __future__ import annotations

import functools
import json
from typing import Any

from langchain_core.tools import StructuredTool


def _wrap(spec: Any) -> StructuredTool:
    """Wrap one ``examlops.mcp`` ToolSpec as a LangChain StructuredTool.

    The wrapper preserves the underlying function's typed signature (via ``functools.wraps``,
    so the tool's arg schema is inferred correctly) and serialises the tool's ``dict`` result
    to JSON — the string the agent reasons over.

    **Layered write-safety (ADR 0102).** For a *mutating* spec the wrapper interposes a
    LangGraph ``interrupt()`` HITL gate **before** invoking the function — closing the gap where
    MCP-bridged writes previously skipped the confirmation gate that in-repo ``@confirmed_write``
    tools get. The spec's own ``_agent_write_gate`` (policy) + audit still run inside ``fn``. So a
    bridged write now gets: exposure (``EXAMLOPS_MCP_ALLOW_WRITES``) + HITL interrupt + policy +
    audit. Tier-C specs are never wrapped (they are excluded upstream in :func:`mcp_tools`).
    """
    fn = spec.fn
    mutating = getattr(spec, "mutating", False)

    if mutating:
        from langgraph.types import interrupt

        from skipper.confirm import WRITE_TOOLS, _is_affirmative

        WRITE_TOOLS.add(spec.name)

        @functools.wraps(fn)
        def _call(*args: Any, **kwargs: Any) -> str:
            if kwargs.get("dry_run"):
                # A dry run (ADR 0081 rule 3) changes nothing, so it needs no human decision.
                return json.dumps(fn(*args, **kwargs), default=str)
            # The model cannot confirm for itself: its own `confirm` is dropped, and the tool's
            # confirmation gate is satisfied only by the human's answer to the interrupt below.
            kwargs.pop("confirm", None)
            summary = f"{spec.name}({', '.join(f'{k}={v!r}' for k, v in kwargs.items())})"
            decision = interrupt({"action": spec.name, "args": kwargs, "summary": summary})
            if not _is_affirmative(decision):
                return json.dumps({"ok": False, "cancelled": True, "action": spec.name})
            return json.dumps(_run_confirmed(fn, args, kwargs), default=str)
    else:

        @functools.wraps(fn)
        def _call(*args: Any, **kwargs: Any) -> str:
            return json.dumps(fn(*args, **kwargs), default=str)

    return StructuredTool.from_function(_call, name=spec.name, description=spec.description)


def _run_confirmed(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Run ``fn`` marked as human-confirmed (the interrupt answered yes), when supported."""
    try:
        from examlops.mcp.write_safety import confirmed
    except ImportError:  # an examlops without write safety: nothing to satisfy
        return fn(*args, **kwargs)
    with confirmed():
        return fn(*args, **kwargs)


def mcp_tools(include_writes: bool | None = None) -> list[StructuredTool]:
    """LangChain tools bridged from the ``examlops.mcp`` registry.

    ``include_writes`` is passed through to ``iter_tools`` (``None`` ⇒ read from
    ``EXAMLOPS_MCP_ALLOW_WRITES``), so mutating tools are only exposed when writes are enabled.
    **Tier-C** tools (human-CLI-only, e.g. access grants / key revocation) are never bound to the
    autonomous agent — they are filtered out here regardless of the write switch (ADR 0102).
    """
    from examlops.mcp.tools import iter_tools

    return [
        _wrap(spec)
        for spec in iter_tools(include_writes=include_writes)
        if getattr(spec, "tier", "read") != "C"
    ]
