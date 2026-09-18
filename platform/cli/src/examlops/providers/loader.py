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

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .base import Provider
from .registry import get_provider

logger = logging.getLogger(__name__)


def degraded_to_default(domain: str, exc: BaseException, *, configured: str | None = None) -> None:
    """Say that a provider failed and the built-in default is being used instead.

    Falling back to the default is the documented behaviour of this layer (ADR 0074) and it is
    right: a calculation must not stop because a site's plugin is broken. But **"nothing is
    configured" and "what you configured is broken" are different events**, and every resolver
    reported them the same way — by silently using the default. A site that had configured a
    stricter promotion gate, a different carbon coefficient or its own placement score got the
    platform's answer instead, with nothing anywhere to say so.

    So the quiet path stays quiet, and this one is audible. It does not raise: the caller's work
    continues on the default, which is the invariant this layer promises.
    """
    logger.warning(
        "%s provider%s failed (%s: %s) — falling back to the built-in default. This is NOT the "
        "same as configuring no provider: the answer you are getting is the platform's, not the "
        "one this site configured.",
        domain,
        f" {configured!r}" if configured else "",
        type(exc).__name__,
        exc,
    )


def _config_dir() -> Path:
    """The examlops config dir. ``EXAMLOPS_CONFIG_DIR`` relocates it (e.g. a shared mount the
    Platform Ops workbench and the platform services both see), then ``<EXAMLOPS_DATA_DIR>/config``
    (ADR 0128), else ``~/.config/examlops``."""
    from examlops.lifecycle.datadir import config_dir

    return config_dir()


CONFIG_DIR = _config_dir()
FINOPS_YAML = CONFIG_DIR / "finops.yaml"
# Platform (non-finops) domains — placement, drift, promotion, … — read here so finops.yaml keeps
# its existing home unchanged (ADR 0077, backward-compat invariant).
PROVIDERS_YAML = CONFIG_DIR / "providers.yaml"


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
    """Return the config block for a domain from its config file (if present).

    Two layouts, one function (ADR 0077):

    * ``group == "finops"`` — nested ``{finops: {<domain>: {...}}}`` in ``finops.yaml`` (unchanged, so
      existing finops users are unaffected).
    * any other group — a flat top-level ``{<domain>: {...}}`` block in ``providers.yaml`` (the home
      for platform domains: ``placement``, ``drift``, ``promotion``, …).

    A block carries ``{provider: ..., coefficients: {...}, formulas: {...}}``.
    """
    if group == "finops":
        data = _load_yaml(FINOPS_YAML)
        block = data.get(group, {})
        if isinstance(block, Mapping):
            domain_block = block.get(domain, {})
            if isinstance(domain_block, Mapping):
                return dict(domain_block)
        return {}
    data = _load_yaml(PROVIDERS_YAML)
    domain_block = data.get(domain, {})
    return dict(domain_block) if isinstance(domain_block, Mapping) else {}


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
