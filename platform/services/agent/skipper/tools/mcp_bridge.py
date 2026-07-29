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
    """
    fn = spec.fn

    @functools.wraps(fn)
    def _call(*args: Any, **kwargs: Any) -> str:
        return json.dumps(fn(*args, **kwargs), default=str)

    return StructuredTool.from_function(_call, name=spec.name, description=spec.description)


def mcp_tools(include_writes: bool | None = None) -> list[StructuredTool]:
    """LangChain tools bridged from the ``examlops.mcp`` registry.

    ``include_writes`` is passed through to ``iter_tools`` (``None`` ⇒ read from
    ``EXAMLOPS_MCP_ALLOW_WRITES``), so mutating tools are only exposed when writes are enabled.
    """
    from examlops.mcp.tools import iter_tools

    return [_wrap(spec) for spec in iter_tools(include_writes=include_writes)]
