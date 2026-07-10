"""ExaMLOps — MLOps platform for HPC power prediction.

This package root is also the **public SDK surface** (ADR 0078): the names re-exported here (and in
``examlops.sdk``) are the stable, semver'd contract that the CLI, MCP tools, and third-party code
build against. Anything else (``examlops._*`` and un-exported internals) is private and may change
without a deprecation cycle. The SDK follows SemVer with a ≥1-minor deprecation window; ``api_version``
tracks the SDK contract independently of the package version.
"""

from __future__ import annotations

import importlib.metadata

from examlops.sdk import (
    PlatformStatus,
    ServiceHealth,
    list_providers,
    place,
    resolve_provider,
    status,
)

# The SDK contract version — bump on any breaking change to the public surface, independently of the
# packaged distribution version.
_API_VERSION = "0.1"

try:
    __version__ = importlib.metadata.version("examlops")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - editable/source checkout
    __version__ = "dev"


def api_version() -> str:
    """Return the stable SDK contract version (SemVer of the public surface)."""
    return _API_VERSION


__all__ = [
    "__version__",
    "api_version",
    "status",
    "place",
    "list_providers",
    "resolve_provider",
    "PlatformStatus",
    "ServiceHealth",
]
