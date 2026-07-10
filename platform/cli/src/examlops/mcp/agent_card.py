"""A2A (Agent-to-Agent) Agent Card for ExaMLOps.

Builds a discovery document that describes the ExaMLOps agent surface to *other* agents,
following the shape of the emerging Agent-to-Agent protocol's Agent Card (skills,
capabilities, endpoints). It is derived directly from the MCP tool :data:`REGISTRY`, so a
peer agent that reads this card sees exactly the tools it can invoke.

The card is intentionally transport-agnostic JSON — it can be served at
``/.well-known/agent.json`` by any HTTP front end or printed by ``exa mcp agent-card``.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

from examlops.mcp.prompts import iter_prompts
from examlops.mcp.resources import iter_resources
from examlops.mcp.tools import iter_tools


def _version() -> str:
    try:
        return importlib.metadata.version("examlops")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "dev"


def build_agent_card(
    *,
    base_url: str | None = None,
    include_writes: bool | None = None,
) -> dict[str, Any]:
    """Return an A2A-style Agent Card describing the ExaMLOps agent.

    Args:
        base_url: Public base URL where the agent is reachable (for the ``url`` field).
        include_writes: Whether mutating tools are advertised. ``None`` => env-driven.
    """
    skills = [
        {
            "id": spec.name,
            "name": spec.name.replace("_", " ").title(),
            "description": spec.description,
            "tags": list(spec.tags),
            "mutating": spec.mutating,
        }
        for spec in iter_tools(include_writes=include_writes)
    ]
    card: dict[str, Any] = {
        "protocolVersion": "0.2.0",
        "name": "ExaMLOps",
        "description": (
            "End-to-end MLOps platform for HPC workloads — training pipelines, model "
            "registry & lifecycle, multi-model serving, drift detection, governance and "
            "FinOps/Green-AI accounting. This agent exposes those capabilities to other "
            "agents and MCP clients."
        ),
        "version": _version(),
        "provider": {"organization": "SEANERGYS", "url": "https://seanergys.eu"},
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": skills,
        "resources": [
            {
                "uri": r.uri,
                "name": r.name,
                "description": r.description,
                "mimeType": r.mime_type,
                "templated": r.templated,
            }
            for r in iter_resources()
        ],
        "prompts": [
            {"name": p.name, "description": p.description, "tags": list(p.tags)}
            for p in iter_prompts()
        ],
        "interfaces": {
            "mcp": {"transports": ["stdio", "http"]},
        },
    }
    if base_url:
        card["url"] = base_url.rstrip("/")
    return card
