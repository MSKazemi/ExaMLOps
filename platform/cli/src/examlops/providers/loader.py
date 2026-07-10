"""Resolve a provider from user configuration (files + env), domain-agnostically.

Config precedence (highest first), mirroring the CLI's ``_config`` philosophy (env → file →
default):

1. an explicit ``EXAMLOPS_<DOMAIN>_PROVIDER`` env var (e.g. ``EXAMLOPS_CARBON_PROVIDER``),
2. a ``finops.yaml`` next to the CLI config, or a ``[finops.<domain>]`` block in ``config.toml``,
3. the domain's registered default.

The config block is passed through to :func:`get_provider`, so it can carry ``coefficients`` and
inline ``formulas`` for the declarative ``expression`` path. A missing/empty config resolves to
the built-in default — nothing breaks when the user has configured nothing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .base import Provider
from .registry import get_provider

CONFIG_DIR = Path.home() / ".config" / "examlops"
FINOPS_YAML = CONFIG_DIR / "finops.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import yaml

        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except Exception:  # pragma: no cover - a malformed file must not crash a calculation
        return {}


def load_domain_config(domain: str, group: str = "finops") -> dict[str, Any]:
    """Return the config block for ``group.domain`` from ``finops.yaml`` (if present).

    Shape: ``{<group>: {<domain>: {provider: ..., coefficients: {...}, formulas: {...}}}}``.
    """
    data = _load_yaml(FINOPS_YAML)
    block = data.get(group, {})
    if isinstance(block, Mapping):
        domain_block = block.get(domain, {})
        if isinstance(domain_block, Mapping):
            return dict(domain_block)
    return {}


def resolve_provider(
    domain: str,
    *,
    override: str | None = None,
    group: str = "finops",
    config: Mapping[str, Any] | None = None,
) -> Provider:
    """Resolve the active provider for ``domain`` from override → env → file → default.

    ``override`` is an explicit CLI ``--provider`` value (wins over everything). ``config`` lets a
    caller inject a block directly (used in tests) instead of reading ``finops.yaml``.
    """
    block = dict(config) if config is not None else load_domain_config(domain, group)
    env_name = os.getenv(f"EXAMLOPS_{domain.upper()}_PROVIDER")
    name = override or env_name or block.get("provider")
    return get_provider(domain, name=name, config=block)
