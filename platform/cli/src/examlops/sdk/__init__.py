"""``examlops`` — the stable, typed public SDK facade (ADR 0078, INC-2).

This is *the* programmatic contract into ExaMLOps. It wraps existing internals (``cli._client``,
``platform_db``, ``hpc_*``, ``providers``) behind a small, typed, semver'd surface so that the CLI,
the MCP tools, and third-party code all drive **one** code path instead of reaching into private
modules. Everything not exported here (or from ``examlops.__init__``) is ``_private`` and may change
without notice (Hyrum's-Law hygiene: keep the surface deliberately small — add on demand).

All heavy imports are **lazy** (inside functions), so importing the SDK is cheap and cannot create an
import cycle with the CLI that imports ``examlops`` at startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ServiceHealth",
    "PlatformStatus",
    "status",
    "place",
    "list_providers",
    "resolve_provider",
]


# ── typed return objects (a stable boundary — not bare dicts) ───────────────────────────────────
@dataclass(frozen=True)
class ServiceHealth:
    """Health of one platform service."""

    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlatformStatus:
    """A snapshot of platform health, pending approvals, and production models.

    ``reachable`` is ``False`` when the control plane could not be contacted; the other fields are
    then empty. ``raw`` carries the underlying payload for renderers that need extra detail — new
    code should prefer the typed fields.
    """

    reachable: bool
    services: dict[str, ServiceHealth]
    pending_approvals: int
    production_models: list[Any]
    raw: dict[str, Any] = field(default_factory=dict)


# ── operations ──────────────────────────────────────────────────────────────────────────────────
def status() -> PlatformStatus:
    """Return a typed platform snapshot (service health, pending approvals, production models)."""
    from examlops.cli import _client
    from examlops.cli._config import load_config

    cfg = load_config()
    try:
        data = _client.get(f"{cfg.control_plane_url}/status", token=cfg.control_plane_token)
    except Exception:
        # Any transport failure (ClientError, socket reset, timeout) → degrade to "unreachable"
        # rather than raising into callers (graceful-degradation invariant, ADR 0076).
        return PlatformStatus(
            reachable=False, services={}, pending_approvals=0, production_models=[], raw={}
        )
    services = {
        key: ServiceHealth(name=key, ok=bool(val.get("ok")), detail=dict(val))
        for key, val in (data.get("services") or {}).items()
    }
    return PlatformStatus(
        reachable=True,
        services=services,
        pending_approvals=int(data.get("pending_approvals", 0) or 0),
        production_models=list(data.get("production_models") or data.get("models") or []),
        raw=dict(data),
    )


def place(gpus: int = 0, cpus: int = 0, nodes: int = 1, provider: str | None = None):
    """Recommend which ACTIVE cluster should run a job for the given resource ask.

    Uses the pluggable placement provider (ADR 0077) — ``provider`` overrides the configured/default
    scorer. Returns a ``hpc_placement.PlacementResult`` (already a typed dataclass).
    """
    from examlops.hpc_placement import ResourceAsk, choose_cluster
    from examlops.hpc_placement_providers import resolve_placement_score_fn
    from examlops.hpc_registry import active_clusters_with_inventory

    ask = ResourceAsk(gpus=gpus, cpus=cpus, nodes=nodes)
    return choose_cluster(
        ask, active_clusters_with_inventory(), resolve_placement_score_fn(provider)
    )


def list_providers(domain: str) -> list[Any]:
    """List calculation providers for ``domain`` (re-export of the registry discovery record)."""
    from examlops.providers import list_providers as _list

    return _list(domain)


def resolve_provider(domain: str, *, override: str | None = None, group: str | None = None):
    """Resolve the active provider for ``domain`` (re-export of the loader's resolution)."""
    from examlops.providers.loader import resolve_provider as _resolve

    return _resolve(domain, override=override, group=group or "finops")
