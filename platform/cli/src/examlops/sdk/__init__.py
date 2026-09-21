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

# Pipeline-as-code DSL (ADR 0080). ``examlops.pipeline_dsl`` is stdlib-only and imports nothing
# from the CLI, so importing it here keeps the SDK cheap and cannot create an import cycle.
from examlops.pipeline_dsl import (  # noqa: E402
    IRError,
    Resources,
    custom_python,
    dataset,
    evaluate,
    hpo,
    pipeline,
    promote,
    step,
    train,
)

__all__ = [
    "pipeline",
    "step",
    "dataset",
    "train",
    "evaluate",
    "promote",
    "hpo",
    "custom_python",
    "Resources",
    "IRError",
    "ServiceHealth",
    "PlatformStatus",
    "Result",
    "ok",
    "err",
    "status",
    "place",
    "list_providers",
    "resolve_provider",
    "list_zoo_models",
    "onboard_model",
    "onboard_all_models",
]


# ── canonical result envelope (item 4.6) ────────────────────────────────────────────────────────
# One ``ok``/error shape for every agent-callable / programmatic surface (CLI JSON, MCP tools, agent
# tools, dashboard BFF), so a caller never has to guess a surface's bespoke dict. ``to_dict()`` emits
# the exact historical wire format (``{"ok": bool, "error"?: str, **data}``) so surfaces can adopt it
# with zero wire change; new code builds envelopes with :func:`ok` / :func:`err`.
@dataclass(frozen=True)
class Result:
    """A typed success/failure envelope. Prefer :func:`ok` / :func:`err` to construct."""

    ok: bool
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, **self.data}
        return {"ok": False, "error": self.error, **self.data}


def ok(**data: Any) -> Result:
    """A success envelope carrying ``data`` fields."""
    return Result(ok=True, error=None, data=data)


def err(message: str, **data: Any) -> Result:
    """A failure envelope with a human ``message`` and optional structured ``data``."""
    return Result(ok=False, error=message, data=data)


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

    ``reachable`` is ``False`` when the control plane could not be contacted; ``pending_approvals``
    and ``production_models`` are then ``None`` — *unknown*, which is not the same claim as "none"
    and must not be rendered as one. ``raw`` carries the underlying payload for renderers that need
    extra detail — new code should prefer the typed fields.
    """

    reachable: bool
    services: dict[str, ServiceHealth]
    pending_approvals: int | None
    #: ``None`` means *not determined* — the control plane or the model registry could not be
    #: read. An empty list is the measured claim that no model carries a lifecycle alias.
    production_models: list[Any] | None
    raw: dict[str, Any] = field(default_factory=dict)


# ── operations ──────────────────────────────────────────────────────────────────────────────────
def control_plane_status(base: str, token: str) -> Any:
    """``GET /v1/status``, or ``/status`` on a control plane that predates the ``/v1`` API.

    During a rolling upgrade the CLI can be newer than the control plane. Only a missing route
    (404) falls back; any other answer is the control plane's answer. Kept here, not in the
    generated ``control_plane_api``, which is rewritten from the contract.
    """
    from examlops import control_plane_api
    from examlops.cli import _client

    try:
        return control_plane_api.status(base=base, token=token)
    except _client.ClientError as exc:
        if exc.status != 404:
            raise
        return _client.get(f"{base.rstrip('/')}/status", token=token)


def status() -> PlatformStatus:
    """Return a typed platform snapshot (service health, pending approvals, production models)."""
    from examlops.cli._config import load_config

    cfg = load_config()
    try:
        data = control_plane_status(cfg.control_plane_url, cfg.control_plane_token or "")
    except Exception:
        # Any transport failure (ClientError, socket reset, timeout) → degrade to "unreachable"
        # rather than raising into callers (graceful-degradation invariant, ADR 0076).
        # Not `pending_approvals=0`: zero is the answer "the queue is empty", and a control plane
        # nobody could reach has not told us that. The same fabrication was removed from the
        # endpoint itself, and it was still being reintroduced here on the failure path.
        return PlatformStatus(
            reachable=False, services={}, pending_approvals=None, production_models=None, raw={}
        )
    services = {
        key: ServiceHealth(name=key, ok=bool(val.get("ok")), detail=dict(val))
        for key, val in (data.get("services") or {}).items()
    }
    return PlatformStatus(
        reachable=True,
        services=services,
        # `or 0` would turn the control plane's "unknown" back into "none pending" — the
        # exact fabrication the endpoint stopped making. None stays None.
        pending_approvals=(
            None if data.get("pending_approvals") is None else int(data["pending_approvals"])
        ),
        production_models=_production_models(cfg, data, services),
        raw=dict(data),
    )


def _production_models(cfg, data: dict[str, Any], services: dict[str, ServiceHealth]):
    """Models carrying a lifecycle alias — read from the registry that actually owns them.

    Both this facade and ``exa status`` used to answer this by reading a ``production_models`` (or
    ``models``) key out of the control plane's ``/status``. That endpoint has never returned
    either: measured against the live app, ``/status`` carries exactly ``services`` and
    ``pending_approvals``. So the promise in three docstrings, in ``exa status --help`` and in the
    README resolved to an empty list on every call, and because the renderer prints nothing for an
    empty list, the answer arrived as *silence* — indistinguishable from a platform with no models
    in production.

    MLflow's registry is where aliases live, so that is where the answer comes from. ``None`` means
    the registry could not be read, which is a different claim from "no model is in production" and
    is rendered differently.
    """
    from examlops.cli import _client

    supplied = data.get("production_models") or data.get("models")
    if supplied:
        # A future control plane that does carry the field wins; nothing today does.
        return list(supplied)
    mlflow = services.get("mlflow")
    if mlflow is not None and not mlflow.ok:
        # The status ping already established it is down. Asking again buys a second timeout and
        # the same answer.
        return None
    try:
        # Every page: this list is filtered to the models carrying a lifecycle alias, so reading
        # one page would report a model that IS in production as not being in production.
        # `PagingError` lands in the same `except` as a transport failure, and `None` here means
        # "unknown" — the honest answer when the registry could not be read completely.
        from examlops.mlflow_paging import all_items

        models = all_items(
            _client.get,
            f"{cfg.mlflow_url}/api/2.0/mlflow/registered-models/search",
            "registered_models",
        )
    except Exception:
        return None
    out: list[dict[str, Any]] = []
    for m in models:
        aliases = {a.get("alias"): a.get("version") for a in (m.get("aliases") or [])}
        if "Production" not in aliases and "Staging" not in aliases:
            continue
        out.append(
            {
                "name": m.get("name"),
                "production_version": aliases.get("Production"),
                "staging_version": aliases.get("Staging"),
            }
        )
    return out


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


# ── model-zoo onboarding (one project per model — CLI/Dashboard/Jupyter share this path) ─────────
def list_zoo_models() -> list[str]:
    """Every model the active use-case pack declares — the candidates for :func:`onboard_model`."""
    from examlops.modelzoo_adopt import zoo_models

    return zoo_models()


def onboard_model(model: str, *, dry_run: bool = False, **opts: Any) -> dict[str, Any]:
    """Provision (or complete) a project for one Zoo model — project · storage · **MinIO connection** ·
    budget · model · workbench · pipeline surfaces. Idempotent; safe to re-run from a notebook.

    Keyword options pass through to :func:`examlops.modelzoo_adopt.adopt_model` (``connection_name``,
    ``provision_connection``, ``s3_endpoint``/``s3_access_key``/``s3_secret``/``s3_bucket``,
    ``cpu_limit``, ``memory_gb``, ``storage_gb``, ``gpu_hours_budget``, ``cost_budget``, ``actor``).
    Returns ``{model, project, dry_run, changed, steps}``.
    """
    from examlops.modelzoo_adopt import adopt_model

    return adopt_model(model, dry_run=dry_run, **opts)


def onboard_all_models(*, dry_run: bool = False, **opts: Any) -> list[dict[str, Any]]:
    """Onboard every Zoo/pack model (one project each). Idempotent — already-provisioned models
    report ``changed=False``. Returns one result dict per model."""
    from examlops.modelzoo_adopt import adopt_all

    return adopt_all(dry_run=dry_run, **opts)
