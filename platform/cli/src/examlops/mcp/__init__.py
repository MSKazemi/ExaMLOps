"""ExaMLOps MCP + A2A surface.

Exposes the platform's capabilities to LLM agents and MCP clients (Claude Desktop,
Claude Code, the in-repo skipper agent, and any Agent-to-Agent peer) by reusing the
exact code paths the ``exa`` CLI uses.

- :mod:`examlops.mcp.tools` — a pure, FastMCP-free registry of agent-callable tools.
- :mod:`examlops.mcp.server` — a FastMCP server (lazily imported) built from that registry.
- :mod:`examlops.mcp.agent_card` — an A2A-style Agent Card derived from the registry.
"""

from __future__ import annotations

from examlops.mcp.prompts import PROMPTS, iter_prompts
from examlops.mcp.resources import RESOURCES, iter_resources
from examlops.mcp.tools import REGISTRY, ToolSpec, iter_tools

__all__ = [
    "PROMPTS",
    "REGISTRY",
    "RESOURCES",
    "ToolSpec",
    "iter_prompts",
    "iter_resources",
    "iter_tools",
]
