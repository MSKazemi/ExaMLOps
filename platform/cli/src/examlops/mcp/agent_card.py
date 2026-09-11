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
from examlops.mcp.tools import capabilities_catalogue, iter_tools


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
            "useCases": list(spec.use_cases),
            "tier": spec.tier,
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
        # A2A capability flags describe *this* agent's protocol support, and each one is a
        # promise a peer may act on. `stateTransitionHistory` means a peer can ask for a
        # task's status-transition history — which needs a task concept, and this surface
        # has none: no task id, no task store, no `tasks/get`. It was hard-coded `True`.
        # `audit_events` is not the same thing; it records what the *platform* did, not the
        # lifecycle of an A2A task. Flip this the day a task store exists, not before.
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": skills,
        "capabilitiesByUseCase": capabilities_catalogue(include_writes=include_writes),
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
        "securitySchemes": _security_schemes(),
    }
    if base_url:
        card["url"] = base_url.rstrip("/")
    return card


def _security_schemes() -> dict[str, Any]:
    """Advertise how a caller authenticates (A2A ``securitySchemes``, item 2.2).

    When OIDC SSO is configured (``EXAMLOPS_OIDC_ISSUER``) the card declares an OAuth2/OIDC bearer
    scheme so peers know to present an IdP-issued token; otherwise it declares the platform bearer
    token. Either way the card no longer implies the agent is unauthenticated — the audit finding
    that Skipper's A2A card lacked ``securitySchemes`` entirely.
    """
    import os

    issuer = os.getenv("EXAMLOPS_OIDC_ISSUER", "").strip()
    if issuer:
        scheme: dict[str, Any] = {
            "type": "openIdConnect",
            # Do not claim verification this surface does not perform. The control plane
            # and dashboard verify IdP tokens (`examlops.iam`, ADR 0120), but the MCP HTTP
            # transport this card describes has no authentication (loopback-only), so the
            # card must say what the token is *for*, not that it is checked. Restore the
            # stronger wording the day `examlops/mcp` actually calls the verifier.
            "description": (
                "IdP-issued OIDC access token, expected by the configured issuer. "
                "NOTE: token verification is not yet enforced by this deployment."
            ),
            "openIdConnectUrl": issuer.rstrip("/") + "/.well-known/openid-configuration",
        }
    else:
        scheme = {
            "type": "http",
            "scheme": "bearer",
            "description": "Platform bearer token (Authorization: Bearer <token>).",
        }
    return {"default": scheme}
