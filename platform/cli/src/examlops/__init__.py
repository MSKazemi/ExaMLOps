"""ExaMLOps — MLOps platform for HPC power prediction.

This package root is also the **public SDK surface** (ADR 0078): the names re-exported here (and in
``examlops.sdk``) are the stable, semver'd contract that the CLI, MCP tools, and third-party code
build against. Anything else (``examlops._*`` and un-exported internals) is private and may change
without a deprecation cycle. The SDK follows SemVer with a ≥1-minor deprecation window; ``api_version``
tracks the SDK contract independently of the package version.
"""

from __future__ import annotations

import importlib.metadata
import sys as _sys

from examlops.sdk import (
    PlatformStatus,
    ServiceHealth,
    audit,
    drift,
    hpc,
    list_providers,
    models,
    place,
    resolve_provider,
    status,
)
from examlops.sdk.errors import SDKError

# `import examlops.models` / `from examlops.drift import status` resolve to the SDK namespaces too,
# not only attribute access. These names are reserved for the SDK: `test_sdk_namespaces.py` fails
# if a real `examlops/<name>` module or package is ever added, which this alias would shadow.
for _ns in (models, drift, audit, hpc):
    _sys.modules.setdefault(f"examlops.{_ns.__name__.rsplit('.', 1)[-1]}", _ns)
del _ns

# The SDK contract version — bump on any breaking change to the public surface, independently of the
# packaged distribution version. 0.2 (additive): the models/drift/audit/hpc namespaces, the typed
# error hierarchy and the reflected reference (ADR 0078 clauses 1, 3, 4).
_API_VERSION = "0.2"

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
    "SDKError",
    "models",
    "drift",
    "audit",
    "hpc",
]
