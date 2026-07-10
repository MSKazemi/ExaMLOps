"""The provider registry — discovery, resolution, and a safe default per domain.

One :class:`Registry` holds providers grouped by *domain* (``carbon``, ``cost``, …). For any
domain it can resolve a provider from three sources, in priority order:

1. **config** — a name (or an inline ``expression`` block) supplied by the caller/config,
2. **entry points** — third-party plugins under ``exa.providers.<domain>`` (mirrors the existing
   ``examlops.cli_plugins`` idiom in ``cli/_plugins``),
3. **built-ins** — registered in-process; one is marked the **default**.

Resolution never crashes the caller: an unknown name raises :class:`ProviderError`, but discovery
of a broken plugin is captured as ``ProviderInfo(ok=False, error=...)`` and skipped — the default
always remains available so a calculation degrades gracefully rather than failing.
"""

from __future__ import annotations

import importlib
import importlib.metadata
from collections.abc import Callable, Mapping
from typing import Any

from .base import Factory, Provider, ProviderError, ProviderInfo
from .yaml_provider import build_expression_provider

ENTRY_POINT_PREFIX = "exa.providers"  # group per domain: "exa.providers.carbon", ...


def _entry_points(group: str) -> list[importlib.metadata.EntryPoint]:
    eps = importlib.metadata.entry_points()
    try:
        return list(eps.select(group=group))
    except AttributeError:  # pragma: no cover - legacy importlib.metadata
        return list(eps.get(group, []))  # type: ignore[attr-defined]


def _instantiate(factory: Factory, config: Mapping[str, Any] | None) -> Provider:
    """Turn a registered factory (Provider subclass or callable) into a Provider instance."""
    if isinstance(factory, Provider):
        return factory
    if isinstance(factory, type) and issubclass(factory, Provider):
        return factory()
    if callable(factory):
        obj = factory(config or {})
        if not isinstance(obj, Provider):
            raise ProviderError(f"factory {factory!r} did not return a Provider")
        return obj
    raise ProviderError(f"cannot build a provider from {factory!r}")


class Registry:
    """Holds built-in providers and resolves the active one for a domain."""

    def __init__(self) -> None:
        # domain -> {name -> factory}
        self._builtins: dict[str, dict[str, Factory]] = {}
        # domain -> default provider name
        self._defaults: dict[str, str] = {}

    # -- registration -----------------------------------------------------------------
    def register(self, domain: str, name: str, factory: Factory, *, default: bool = False) -> None:
        """Register a built-in ``factory`` for ``domain`` under ``name``."""
        self._builtins.setdefault(domain, {})[name] = factory
        if default or domain not in self._defaults:
            self._defaults[domain] = name

    def default_name(self, domain: str) -> str | None:
        return self._defaults.get(domain)

    # -- discovery --------------------------------------------------------------------
    def discover(self, domain: str) -> list[ProviderInfo]:
        """List every provider available for ``domain`` (built-ins + entry-point plugins).

        Errors loading a plugin are captured, never raised — matching ``cli/_plugins.discover``.
        """
        infos: list[ProviderInfo] = []
        default = self._defaults.get(domain)
        for name, factory in sorted(self._builtins.get(domain, {}).items()):
            try:
                provider = _instantiate(factory, None)
                infos.append(
                    ProviderInfo(
                        name=name,
                        kind="builtin",
                        provider=provider,
                        value=f"{'default' if name == default else 'builtin'}",
                    )
                )
            except Exception as exc:  # pragma: no cover - a builtin should never fail
                infos.append(ProviderInfo(name=name, kind="builtin", ok=False, error=str(exc)))
        for ep in _entry_points(f"{ENTRY_POINT_PREFIX}.{domain}"):
            try:
                provider = _instantiate(ep.load(), None)
                infos.append(
                    ProviderInfo(name=ep.name, kind="entrypoint", provider=provider, value=ep.value)
                )
            except Exception as exc:
                infos.append(
                    ProviderInfo(
                        name=ep.name, kind="entrypoint", ok=False, error=str(exc), value=ep.value
                    )
                )
        return infos

    # -- resolution -------------------------------------------------------------------
    def get(
        self,
        domain: str,
        name: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> Provider:
        """Resolve the active provider for ``domain``.

        ``name`` selects a provider explicitly. When ``name`` is ``None`` the ``config`` block's
        ``provider`` key is used, else the domain default. A ``name`` of ``"expression"`` (or a
        config with ``formulas``) builds a declarative :class:`ExpressionProvider` from ``config``.
        A ``name`` containing ``:`` or ``.`` is treated as a dotted import path
        (``"my_pkg.mod:Factory"``) — the un-packaged local-file path.
        """
        cfg = dict(config or {})
        # An explicit selection (CLI --provider / env / config `provider:`) wins over an implicit
        # `formulas` block; only fall to the inline-expression path when nothing else was chosen.
        explicit = name or cfg.get("provider")
        chosen = explicit or self._defaults.get(domain)
        if not chosen:
            raise ProviderError(f"no provider requested and no default for domain {domain!r}")

        # 1. declarative inline formula (explicitly asked for, or implied when nothing was chosen)
        if chosen == "expression" or (explicit is None and cfg.get("formulas")):
            return build_expression_provider(cfg.get("name", "expression"), cfg)

        # 2. dotted import path (un-packaged local module)
        if ":" in chosen or ("." in chosen and chosen not in self._builtins.get(domain, {})):
            return _instantiate(_import_dotted(chosen), cfg)

        # 3. built-in
        builtins = self._builtins.get(domain, {})
        if chosen in builtins:
            return _instantiate(builtins[chosen], cfg)

        # 4. entry-point plugin
        for ep in _entry_points(f"{ENTRY_POINT_PREFIX}.{domain}"):
            if ep.name == chosen:
                return _instantiate(ep.load(), cfg)

        raise ProviderError(
            f"unknown provider {chosen!r} for domain {domain!r}; "
            f"available: {sorted(builtins)} + entry points"
        )


def _import_dotted(path: str) -> Factory:
    """Import ``pkg.mod:attr`` (or ``pkg.mod.attr``) → the referenced object."""
    module_path, _, attr = path.partition(":")
    if not attr:
        module_path, _, attr = path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
        return getattr(module, attr)
    except Exception as exc:
        raise ProviderError(f"cannot import provider {path!r}: {exc}") from exc


# Process-wide singleton (domains register into it at import time).
_REGISTRY = Registry()


def register_provider(domain: str, name: str, factory: Factory, *, default: bool = False) -> None:
    """Register a built-in provider on the global registry."""
    _REGISTRY.register(domain, name, factory, default=default)


def get_provider(
    domain: str, name: str | None = None, config: Mapping[str, Any] | None = None
) -> Provider:
    """Resolve the active provider for ``domain`` from the global registry."""
    return _REGISTRY.get(domain, name, config)


def list_providers(domain: str) -> list[ProviderInfo]:
    """Discover every provider for ``domain`` on the global registry."""
    return _REGISTRY.discover(domain)


def default_provider_name(domain: str) -> str | None:
    return _REGISTRY.default_name(domain)


# Registered callable factory type alias, for callers that build config-driven providers.
ConfigFactory = Callable[[Mapping[str, Any]], Provider]
