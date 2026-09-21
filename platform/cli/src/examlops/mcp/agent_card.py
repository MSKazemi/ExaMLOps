"""A2A (Agent-to-Agent) Agent Card for ExaMLOps.

Builds a discovery document that describes the ExaMLOps agent surface to *other* agents,
following the shape of the emerging Agent-to-Agent protocol's Agent Card (skills,
capabilities, endpoints). It is derived directly from the MCP tool :data:`REGISTRY`, so a
peer agent that reads this card sees exactly the tools it can invoke.

The card is intentionally transport-agnostic JSON — it can be served at
``/.well-known/agent.json`` by any HTTP front end or printed by ``exa mcp agent-card``.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from typing import Any

from examlops.mcp.prompts import iter_prompts
from examlops.mcp.resources import iter_resources
from examlops.mcp.tools import capabilities_catalogue, iter_tools

#: The A2A protocol version the cards are shaped for (ADR 0141 d5: "moves from 0.2.0 to 1.0").
#: Only the version string and the fields this module already emitted are asserted; the full
#: A2A 1.0 JSON schema is NOT verified offline (no copy in the evidence base), so the card does
#: not claim schema conformance beyond what tests/unit/test_a2a_card_conformance.py checks.
A2A_PROTOCOL_VERSION = "1.0"

#: Card capability -> ``module:attribute`` of the code that backs it, or ``None`` when nothing
#: does. A card advertises a capability only when that resolves (ADR 0141 d5, ADR 0147 d6/d8).
#: There is no task store, no SSE stream and no push sender, so all are ``None`` today; the
#: day one is built, put its path here and the card starts advertising it - not before.
IMPLEMENTED_CAPABILITIES: dict[str, str | None] = {
    "streaming": None,
    "pushNotifications": None,
    "stateTransitionHistory": None,
}
#: A2A ``extensions`` the card declares: none are implemented.
IMPLEMENTED_EXTENSIONS: tuple[str, ...] = ()


def _backed(path: str | None) -> bool:
    if not path:
        return False
    import importlib

    mod, _, attr = path.rpartition(":")
    try:
        return hasattr(importlib.import_module(mod), attr)
    except ImportError:
        return False


def derive_capabilities() -> dict[str, Any]:
    """Capability flags derived from :data:`IMPLEMENTED_CAPABILITIES`, never hand-set."""
    caps: dict[str, Any] = {k: _backed(v) for k, v in IMPLEMENTED_CAPABILITIES.items()}
    caps["extensions"] = list(IMPLEMENTED_EXTENSIONS)
    return caps


def card_digest(card: dict[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical JSON of ``card`` (sorted keys, no whitespace)."""
    body = json.dumps(card, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_agent_version_card(version: Any) -> dict[str, Any]:
    """A2A-shaped card for one registered ``AgentVersion`` (ADR 0146; ADR 0141 d5).

    Content-addressed: ``version`` is the version id, so the card changes iff the agent's pinned
    tuple does. Skills are the version's pinned tools, described from the MCP registry. Nothing
    serves this agent over a network route yet, so it declares no ``url``, no interfaces and no
    security scheme: it describes the agent, it does not claim a reachable endpoint.
    """
    from examlops.mcp.tools import REGISTRY

    by_name = {spec.name: spec for spec in REGISTRY}
    m = version.manifest
    skills = []
    for t in m["tools"]["tools"]:
        spec = by_name.get(t["name"])
        skills.append(
            {
                "id": t["name"],
                "name": t["name"].replace("_", " ").title(),
                "description": spec.description if spec else "(tool not in this registry)",
                "tags": list(spec.tags) if spec else [],
                "schemaHash": t["schema_hash"],
            }
        )
    prompts = ", ".join(f"{p['name']}@{p['version']}" for p in m["prompts"])
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": version.agent,
        "description": (
            f"ExaMLOps agent {version.agent!r}, version {version.version_id}; autonomy "
            f"{m['policy']['autonomy']}; prompts {prompts}."
        ),
        "version": version.version_id,
        "provider": {"organization": "ExaMLOps", "url": "https://github.com/MSKazemi/ExaMLOps"},
        "capabilities": derive_capabilities(),
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
        "interfaces": {},
        "securitySchemes": {},
        "signed": bool(version.signed),
    }


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
            "annotations": spec.annotations,
        }
        for spec in iter_tools(include_writes=include_writes)
    ]
    card: dict[str, Any] = {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": "ExaMLOps",
        "description": (
            "End-to-end MLOps platform for HPC workloads — training pipelines, model "
            "registry & lifecycle, multi-model serving, drift detection, governance and "
            "FinOps/Green-AI accounting. This agent exposes those capabilities to other "
            "agents and MCP clients."
        ),
        "version": _version(),
        "provider": {"organization": "ExaMLOps", "url": "https://github.com/MSKazemi/ExaMLOps"},
        # Derived from IMPLEMENTED_CAPABILITIES: a flag is true only if code backs it.
        "capabilities": derive_capabilities(),
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
