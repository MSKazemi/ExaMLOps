"""``gateway.yaml`` — the declarative routing config (ADR 0155).

Strict and total: every problem is reported at once, each with the path of the key at fault
(``models.chat.deployments[0].provider``), and nothing is built from a config that has any. The
same validator serves ``exa gateway validate`` and the service's reload, so a file that passes
offline is a file the running gateway accepts.

With no file the service starts from :func:`generated_config` — the Ollama it can reach, one route
per chat model it reports — which is what makes a local start need no setup.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from examlops.gateway.egress import EgressDenied, validate_base_url
from examlops.gateway.providers import OllamaProvider, Provider
from examlops.gateway.providers.base import Capabilities
from examlops.gateway.resilience import RetryBudget
from examlops.gateway.routing import Catalog, Deployment, GatewayCore, Route

logger = logging.getLogger(__name__)

Locality = Literal["local", "site", "external"]


class ConfigError(ValueError):
    """An invalid config. ``errors`` is every problem found, each prefixed with its path."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid gateway config:\n  " + "\n  ".join(errors))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderCfg(_Model):
    type: Literal["ollama"]
    base_url: str
    locality: Locality = "local"
    external_ok: bool = False
    max_concurrency: int | None = Field(None, ge=1)
    max_queue: int = Field(8, ge=0)
    queue_timeout_s: float = Field(5.0, gt=0)
    connect_timeout_s: float = Field(3.0, gt=0)
    read_timeout_s: float = Field(300.0, gt=0)  # includes a cold model load (ADR 0153 d7)
    keep_alive: str | int | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    think: bool | None = None
    discover: bool = False


class DeploymentCfg(_Model):
    provider: str
    model: str
    weight: float = Field(1.0, gt=0)
    priority: int = 0
    external_ok: bool | None = None  # inherits the provider's
    price_per_1k: float | None = Field(None, ge=0)  # USD/1k tokens, for `cost_aware` (ADR 0083)


class ModelCfg(_Model):
    strategy: Literal["priority", "weighted", "least_inflight", "lowest_latency", "cost_aware"] = (
        "priority"
    )
    deployments: list[DeploymentCfg] = Field(min_length=1)
    fallbacks: list[str] = Field(default_factory=list)
    required: bool = False
    total_timeout_s: float = Field(300.0, gt=0)


def _default_localities() -> list[Locality]:
    return ["local", "site"]


class DefaultsCfg(_Model):
    allowed_localities: list[Locality] = Field(default_factory=_default_localities)


class GatewayCfg(_Model):
    version: Literal[1] = 1
    defaults: DefaultsCfg = Field(default_factory=DefaultsCfg)
    providers: dict[str, ProviderCfg] = Field(default_factory=dict)
    models: dict[str, ModelCfg] = Field(default_factory=dict)
    aliases: dict[str, str] = Field(default_factory=dict)


ProviderFactory = Callable[[str, ProviderCfg], Provider]


def _path(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def _parse(raw: Any) -> tuple[GatewayCfg | None, list[str]]:
    try:
        return GatewayCfg.model_validate(raw), []
    except ValidationError as exc:
        return None, [f"{_path(e['loc'])}: {e['msg']}" for e in exc.errors()]


def _mapping(raw: dict[str, Any], key: str) -> dict[Any, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _reference_errors(raw: Any) -> list[str]:
    """Dangling references, fallback cycles and alias problems, over the *raw* mapping.

    Defensive about shape on purpose: it runs even when the schema check failed, so an operator
    sees a misspelt provider name and a wrong type in one pass instead of two.
    """
    if not isinstance(raw, dict):
        return []
    providers, models, aliases = (_mapping(raw, k) for k in ("providers", "models", "aliases"))
    discovering = any(isinstance(p, dict) and p.get("discover") for p in providers.values())
    errs: list[str] = []

    def fallbacks_of(name: str) -> list[Any]:
        m = models.get(name)
        fb = m.get("fallbacks") if isinstance(m, dict) else None
        return fb if isinstance(fb, list) else []

    for mname, m in models.items():
        if not isinstance(m, dict):
            continue
        for i, d in enumerate(m.get("deployments") or []):
            if isinstance(d, dict) and d.get("provider") not in providers:
                errs.append(
                    f"models.{mname}.deployments[{i}].provider: unknown provider {d.get('provider')!r}"
                )
        for i, fb in enumerate(fallbacks_of(mname)):
            if fb not in models:
                errs.append(f"models.{mname}.fallbacks[{i}]: unknown model {fb!r}")

    for start in models:
        seen: set[Any] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            for nxt in fallbacks_of(cur):
                if nxt == start:
                    errs.append(f"models.{start}.fallbacks: fallback cycle back to {start!r}")
                    stack.clear()
                    break
                if nxt in models and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)

    for alias, target in aliases.items():
        if alias in models:
            errs.append(f"aliases.{alias}: shadows a model of the same name")
        elif target not in models and not discovering:
            errs.append(f"aliases.{alias}: unknown model {target!r}")
    return errs


def _policy_errors(cfg: GatewayCfg) -> list[str]:
    """Egress and locality — needs the typed config, so it runs only once the schema is valid."""
    errs: list[str] = []
    allowed = set(cfg.defaults.allowed_localities)
    for name, p in cfg.providers.items():
        try:
            validate_base_url(p.base_url, locality=p.locality)
        except EgressDenied as exc:
            errs.append(f"providers.{name}.base_url: {exc}")
    for mname, m in cfg.models.items():
        if not all(d.provider in cfg.providers for d in m.deployments):
            continue  # a dangling provider is already reported; don't pile a second message on it
        permitted = 0
        for d in m.deployments:
            prov = cfg.providers[d.provider]
            external_ok = prov.external_ok if d.external_ok is None else d.external_ok
            if prov.locality in allowed and (prov.locality != "external" or external_ok):
                permitted += 1
        if permitted == 0:
            errs.append(
                f"models.{mname}: no deployment is permitted for locality "
                f"{sorted(allowed)} (external providers also need external_ok: true)"
            )
    return errs


def _semantic_errors(cfg: GatewayCfg) -> list[str]:
    return _reference_errors(cfg.model_dump()) + _policy_errors(cfg)


def validate_config(raw: Any) -> list[str]:
    """Every problem with ``raw``, each prefixed with its path; ``[]`` means valid."""
    cfg, errs = _parse(raw)
    if cfg is not None:
        return _semantic_errors(cfg)
    return errs + [e for e in _reference_errors(raw) if e not in errs]


def load_config_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError([f"{p.name}: cannot read: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ConfigError([f"{p.name}: top level must be a mapping"])
    return data


def default_config_path() -> Path | None:
    """``EXAMLOPS_GATEWAY_CONFIG``, else ``<config dir>/gateway.yaml`` when that file exists."""
    if raw := os.getenv("EXAMLOPS_GATEWAY_CONFIG"):
        return Path(raw)
    from examlops.lifecycle.datadir import config_dir

    candidate = config_dir() / "gateway.yaml"
    return candidate if candidate.is_file() else None


def generated_config(base_url: str, *, name: str | None = None) -> dict[str, Any]:
    """The zero-config default: the Ollama at ``base_url``, every chat model it reports (ADR 0155 d4)."""
    provider: dict[str, Any] = {
        "type": "ollama",
        "base_url": base_url,
        "locality": "local",
        "discover": True,
    }
    if os.getenv("AGENT_OLLAMA_KEEP_ALIVE"):
        provider["keep_alive"] = os.environ["AGENT_OLLAMA_KEEP_ALIVE"]
    if os.getenv("AGENT_OLLAMA_NUM_CTX"):
        provider["options"] = {"num_ctx": int(os.environ["AGENT_OLLAMA_NUM_CTX"])}
    raw: dict[str, Any] = {
        "version": 1,
        "providers": {name or os.getenv("EXAMLOPS_LLM_OLLAMA_NAME", "ollama"): provider},
        "models": {},
        "aliases": {},
    }
    if agent_model := os.getenv("AGENT_MODEL"):
        raw["aliases"]["default"] = agent_model
    return raw


def default_provider_factory(name: str, cfg: ProviderCfg) -> Provider:
    import httpx

    return OllamaProvider(
        name,
        cfg.base_url,
        locality=cfg.locality,
        keep_alive=cfg.keep_alive,
        options=cfg.options,
        think=cfg.think,
        timeout=httpx.Timeout(cfg.read_timeout_s, connect=cfg.connect_timeout_s),
    )


@dataclass
class Runtime:
    """A built, immutable routing table. Reload builds a whole new one and swaps the reference."""

    cfg: GatewayCfg
    catalog: Catalog
    core: GatewayCore
    providers: dict[str, Provider]
    source: str  # "file" | "generated"
    allowed_localities: tuple[str, ...]
    warnings: list[str] = field(default_factory=list)
    built_at: float = field(default_factory=time.time)


async def build_runtime(
    raw: Any,
    *,
    provider_factory: ProviderFactory = default_provider_factory,
    source: str = "file",
    retry_budget: RetryBudget | None = None,
) -> Runtime:
    """Validate ``raw`` and build a :class:`Runtime`, or raise :class:`ConfigError` and build nothing."""
    cfg, errs = _parse(raw)
    if cfg is None:
        raise ConfigError(errs)
    errs = _semantic_errors(cfg)
    if errs:
        raise ConfigError(errs)

    warnings: list[str] = []
    providers = {n: provider_factory(n, p) for n, p in cfg.providers.items()}
    discovered: dict[str, dict[str, Capabilities]] = {}  # provider → model → capabilities
    for pname, pcfg in cfg.providers.items():
        if not pcfg.discover:
            continue
        try:
            discovered[pname] = {
                m.name: m.capabilities
                for m in await providers[pname].list_models()
                if m.capabilities.chat
            }
        except Exception as exc:  # noqa: BLE001 - discovery is advisory; the gateway still starts
            warnings.append(f"provider {pname!r}: discovery failed ({exc})")

    def deployment(dcfg: DeploymentCfg) -> Deployment:
        prov = cfg.providers[dcfg.provider]
        return Deployment(
            providers[dcfg.provider],
            dcfg.model,
            weight=dcfg.weight,
            priority=dcfg.priority,
            capabilities=discovered.get(dcfg.provider, {}).get(dcfg.model),
            external_ok=prov.external_ok if dcfg.external_ok is None else dcfg.external_ok,
            max_concurrency=prov.max_concurrency,
            max_queue=prov.max_queue,
            queue_timeout_s=prov.queue_timeout_s,
            price_per_1k=dcfg.price_per_1k,
        )

    routes = [
        Route(
            name,
            [deployment(d) for d in m.deployments],
            strategy=m.strategy,
            fallbacks=list(m.fallbacks),
            total_timeout_s=m.total_timeout_s,
            required=m.required,
        )
        for name, m in cfg.models.items()
    ]
    for pname, models in discovered.items():  # operator-written routes win over discovered ones
        pcfg = cfg.providers[pname]
        for model_name, caps in models.items():
            if model_name in cfg.models:
                continue
            dep = Deployment(
                providers[pname],
                model_name,
                capabilities=caps,
                external_ok=pcfg.external_ok,
                max_concurrency=pcfg.max_concurrency,
                max_queue=pcfg.max_queue,
                queue_timeout_s=pcfg.queue_timeout_s,
            )
            routes.append(Route(model_name, [dep]))

    known = {r.name for r in routes}
    aliases: dict[str, str] = {}
    alias_errors: list[str] = []
    for alias, target in cfg.aliases.items():
        if target in known:
            aliases[alias] = target
        elif source == "generated":
            warnings.append(
                f"alias {alias!r} → {target!r} dropped: {target!r} is not on the server"
            )
        else:
            alias_errors.append(f"aliases.{alias}: unknown model {target!r}")
    if alias_errors:
        raise ConfigError(alias_errors)

    catalog = Catalog(routes, aliases)
    return Runtime(
        cfg=cfg,
        catalog=catalog,
        core=GatewayCore(catalog, retry_budget=retry_budget),
        providers=providers,
        source=source,
        allowed_localities=tuple(cfg.defaults.allowed_localities),
        warnings=warnings,
    )
