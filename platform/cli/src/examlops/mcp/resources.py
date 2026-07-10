"""MCP resources for ExaMLOps — readable context an agent can pull on demand.

Where *tools* are actions an agent invokes, *resources* are addressable, cacheable context
(platform status, the model registry, the audit log, a specific model's detail). They reuse
the same functions as :mod:`examlops.mcp.tools`, so there is one source of truth.

Pure and FastMCP-free — the registry is unit-testable on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from examlops.mcp import tools


@dataclass(frozen=True)
class ResourceSpec:
    uri: str
    name: str
    description: str
    fn: Callable[..., Any]
    mime_type: str = "application/json"
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def templated(self) -> bool:
        return "{" in self.uri


RESOURCES: tuple[ResourceSpec, ...] = (
    ResourceSpec(
        uri="examlops://status",
        name="Platform status",
        description="Live health snapshot of the ExaMLOps platform (services, models, approvals).",
        fn=tools.platform_status,
        tags=("status",),
    ),
    ResourceSpec(
        uri="examlops://models",
        name="Model registry",
        description="All registered models with their Production alias and latest version.",
        fn=tools.list_models,
        tags=("registry",),
    ),
    ResourceSpec(
        uri="examlops://audit/recent",
        name="Recent audit events",
        description="The 50 most recent platform audit events (mutations, retrains, promotions).",
        fn=lambda: tools.recent_audit_events(50),
        tags=("governance",),
    ),
    ResourceSpec(
        uri="examlops://model/{name}",
        name="Model detail",
        description="Full detail for one registered model: versions, aliases and metrics.",
        fn=tools.model_detail,
        tags=("registry",),
    ),
)


def iter_resources() -> tuple[ResourceSpec, ...]:
    return RESOURCES
