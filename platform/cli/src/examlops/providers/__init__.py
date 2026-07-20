"""``examlops.providers`` — a general, reusable pluggable-calculation substrate.

Swap the *mathematical formula* behind any calculation ("domain") without touching core code, via
a built-in Python provider, a third-party entry-point plugin, or a declarative YAML/config formula
evaluated safely. First used for ``carbon``; reused for ``cost`` and (later) ``drift``/``promotion``.

Public API::

    from examlops.providers import get_provider, list_providers, register_provider

    register_provider("carbon", "green-ai-default", GreenAIProvider, default=True)
    provider = get_provider("carbon", config={"provider": "green-ai-default"})
    result = provider.compute({"gpu_hours": 10})

See ``.claude/plans/finops-plugins/`` for the design (research, plan, ADR).
"""

from __future__ import annotations

from .authoring import (
    delete_provider,
    list_project_providers,
    load_project_providers,
    provider_path,
    providers_root,
    read_provider_source,
    register_from_source,
    save_provider,
)
from .base import Provider, ProviderError, ProviderInfo, ProviderMeta
from .registry import (
    Registry,
    default_provider_name,
    get_provider,
    list_providers,
    register_provider,
)
from .sandbox import ProviderSecurityError, compile_provider, validate_source
from .yaml_provider import ExpressionProvider, build_expression_provider

__all__ = [
    "Provider",
    "ProviderMeta",
    "ProviderInfo",
    "ProviderError",
    "Registry",
    "ExpressionProvider",
    "build_expression_provider",
    "register_provider",
    "get_provider",
    "list_providers",
    "default_provider_name",
    # Notebook/dashboard authoring (per-project, AST-sandboxed)
    "ProviderSecurityError",
    "validate_source",
    "compile_provider",
    "register_from_source",
    "save_provider",
    "read_provider_source",
    "delete_provider",
    "list_project_providers",
    "load_project_providers",
    "provider_path",
    "providers_root",
]
