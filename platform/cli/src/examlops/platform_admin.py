"""``examlops.platform_admin`` — the governed façade for platform-management changes.

The **single sanctioned entry point** a Jupyter notebook (the "Platform Ops" workbench) or the
dashboard uses to mutate platform-wide configuration and deploy calculation code. It exists to close
one gap: a notebook that writes *directly* to the provider store, ``config.toml``/``finops.yaml``, or
``platform.db`` bypasses the RBAC + policy + audit that the ``exa`` CLI and dashboard emit. Every
method here runs the same three-step governance envelope so a notebook change is attributable,
policy-gated and auditable exactly like a CLI change:

    1. ``authz.check``       — RBAC on ``platform:core`` (editor for config, owner for source).
                               Enforced only under ``EXAMLOPS_MULTITENANCY`` (single-tenant dev = allow).
    2. ``policy.decide``     — declarative allow / deny / require_approval (ADR 0079). A denied action
                               raises; ``require_approval`` raises unless the caller passes ``approve=True``
                               (recorded), matching the CLI's confirm/HITL behaviour.
    3. ``write_audit_event`` — a hash-chained ``audit_events`` row, ``source=<workbench|dashboard>``,
                               attributed to ``EXAMLOPS_ACTOR`` — before/after captured where cheap.

Nothing here reimplements a writer: each method wraps the exact ``examlops.*`` function the CLI calls
(``finops.cost``, ``providers.save_provider``, ``connections``, ``_config.write_config``,
``data.*`` setters, the ``seanerbus`` UUID edit) — so the façade can never drift from the CLI.
"""

from __future__ import annotations

import contextvars
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from examlops.platform_db import _actor

__all__ = [
    "PlatformAdminApprovalRequired",
    "PlatformAdminDenied",
    "PlatformAdminError",
    "acting_as",
    "compute_cost_card",
    "deploy_provider",
    "list_authored_providers",
    "propose_source_change",
    "recent_changes",
    "set_bridge_uuid",
    "set_compute_cost",
    "set_config",
    "set_connection",
    "set_knob",
]

# ── Governance object + relations ────────────────────────────────────────────
# The platform-management surface is modelled as a single authz object. Config changes need
# `editor`; editing/staging real source (Tier B) needs `owner`. Under single-tenant dev
# (`EXAMLOPS_MULTITENANCY` unset) authz.check always allows, so this is a no-op until a site
# turns multi-tenancy on — then it enforces exactly like every other project resource (ADR 0086).
PLATFORM_OBJECT = "platform:core"


class PlatformAdminError(Exception):
    """Base error for the platform-management façade."""


class PlatformAdminDenied(PlatformAdminError):
    """RBAC or policy denied the action."""


class PlatformAdminApprovalRequired(PlatformAdminError):
    """Policy requires a human approval; re-call with ``approve=True`` to proceed."""


# Per-call attribution override (concurrency-safe): the dashboard serves many users from one process,
# so it can't set EXAMLOPS_ACTOR/EXAMLOPS_ADMIN_SOURCE in the environment. `acting_as` binds them to
# the current task instead, and the resolvers below prefer the contextvar over the env.
_CTX: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "platform_admin_ctx", default={}
)


@contextmanager
def acting_as(actor: str, *, source: str = "dashboard") -> Iterator[None]:
    """Attribute façade writes inside this block to ``actor`` with audit ``source`` (task-local).

    Used by the dashboard router so a write is attributed to the logged-in principal — not the
    container user — without touching process env (which would race across concurrent requests).
    """
    token = _CTX.set({"actor": actor, "source": source})
    try:
        yield
    finally:
        _CTX.reset(token)


def _resolved_actor() -> str:
    """Actor for the current write: the ``acting_as`` binding wins, else ``EXAMLOPS_ACTOR``/``USER``."""
    return _CTX.get().get("actor") or _actor()


def _source() -> str:
    """Audit ``source`` for façade writes: ``acting_as`` binding → ``EXAMLOPS_ADMIN_SOURCE`` → workbench."""
    return _CTX.get().get("source") or os.getenv("EXAMLOPS_ADMIN_SOURCE") or "workbench"


def _run(
    action: str,
    target: str | None,
    apply: Callable[[], Any],
    *,
    details: dict[str, Any] | None = None,
    relation: str = "editor",
    approve: bool = False,
    tenant: str = "default",
) -> dict[str, Any]:
    """Run ``apply`` inside the RBAC → policy → audit envelope and return a governed result.

    Raises :class:`PlatformAdminDenied` (RBAC or ``deny`` policy) or
    :class:`PlatformAdminApprovalRequired` (``require_approval`` policy without ``approve=True``)
    *before* ``apply`` runs, so a blocked change never touches a store.
    """
    actor = _resolved_actor()

    # 1. RBAC (default-allow under single-tenant dev).
    from examlops import authz

    if not authz.check(actor, relation, PLATFORM_OBJECT, actor=actor):
        _audit_decision(action, target, actor, "deny", "rbac", tenant)
        raise PlatformAdminDenied(
            f"actor {actor!r} lacks {relation!r} on {PLATFORM_OBJECT} for action {action!r}"
        )

    # 2. Policy-as-code (ADR 0079). policy.decide writes its own audit row.
    # `decide_safe`, not `decide`: an engine bug used to raise here uncaught, crashing the
    # workbench/dashboard call before RBAC's own audit row (above) was the only trace left —
    # nothing said policy was ever consulted. Now it denies (a notebook has no human confirming
    # a require_approval prompt, same reasoning as autopilot) and is itself durably audited (BL-080).
    from examlops.policy import DENY, decide_safe

    ctx = {"actor": actor, "target": target or "", "action_kind": action, **(details or {})}
    decision = decide_safe(f"platform_admin:{action}", ctx, default_effect=DENY)
    if decision.denied:
        raise PlatformAdminDenied(f"policy denied {action!r}: {decision.reason}")
    if decision.requires_approval and not approve:
        _audit_decision(action, target, actor, "require_approval", decision.rule, tenant)
        raise PlatformAdminApprovalRequired(
            f"policy requires approval for {action!r} ({decision.reason}); "
            "re-call with approve=True (a human confirms) to proceed"
        )

    # 3. Perform, then audit the mutation with attribution + before/after.
    result = apply()
    audited = dict(details or {})
    if approve and decision.requires_approval:
        audited["approved_by"] = actor
    if isinstance(result, Mapping):
        audited.setdefault("result", {k: result[k] for k in list(result)[:8]})
    _write_audit(action, target, actor, audited, tenant)
    return {"ok": True, "action": action, "target": target, "actor": actor, "result": result}


def _write_audit(
    action: str, target: str | None, actor: str, details: dict[str, Any], tenant: str
) -> None:
    from examlops.data.audit import write_audit_event

    write_audit_event(_source(), actor, f"platform_admin:{action}", target, details, tenant=tenant)


def _audit_decision(
    action: str, target: str | None, actor: str, effect: str, rule: str | None, tenant: str
) -> None:
    """Audit a blocked/gated action so denials + approval-required are on the tamper-evident trail."""
    try:
        _write_audit(action, target, actor, {"effect": effect, "rule": rule}, tenant)
    except Exception:  # pragma: no cover - audit must never mask the real error
        pass


# ── Config: compute-node cost rate card ──────────────────────────────────────


def set_compute_cost(
    gpu_per_hour: float | None = None,
    cpu_per_hour: float | None = None,
    *,
    provider: str | None = None,
    approve: bool = False,
) -> dict[str, Any]:
    """Set the compute-node cost rate card in ``~/.config/examlops/finops.yaml`` (``[finops.cost]``).

    Writes ``coefficients.gpu_rate`` / ``coefficients.cpu_rate`` (and optionally the active
    ``provider``) — the same block ``finops.cost.estimate_cost_via_provider`` reads, so
    ``exa models cost`` and the dashboard pick the new rate up immediately. Governed + audited.
    """
    if gpu_per_hour is None and cpu_per_hour is None and provider is None:
        raise PlatformAdminError("set_compute_cost: nothing to change")
    before = _read_finops_cost()

    def _apply() -> dict[str, Any]:
        block = _read_finops_cost()
        coeffs = dict(block.get("coefficients") or {})
        if gpu_per_hour is not None:
            coeffs["gpu_rate"] = float(gpu_per_hour)
        if cpu_per_hour is not None:
            coeffs["cpu_rate"] = float(cpu_per_hour)
        block["coefficients"] = coeffs
        if provider is not None:
            block["provider"] = provider
        _write_finops_cost(block)
        return block

    return _run(
        "set_compute_cost",
        "finops.cost",
        _apply,
        details={"before": before, "gpu_rate": gpu_per_hour, "cpu_rate": cpu_per_hour},
        approve=approve,
    )


def _finops_yaml_path():
    from examlops.providers.loader import FINOPS_YAML

    return FINOPS_YAML


def _read_finops_cost() -> dict[str, Any]:
    """The current ``[finops.cost]`` block (empty if unset)."""
    from examlops.providers.loader import load_domain_config

    return dict(load_domain_config("cost"))


def _write_finops_cost(block: Mapping[str, Any]) -> None:
    """Merge ``block`` into ``finops.yaml`` under ``finops.cost`` (preserving other sections)."""
    import yaml

    path = _finops_yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            data = {}
    finops = dict(data.get("finops") or {})
    finops["cost"] = dict(block)
    data["finops"] = finops
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def compute_cost_card() -> dict[str, Any]:
    """Read-only: the effective compute-node rate card + provider methodology (for notebook/dashboard)."""
    from examlops.finops.cost import default_cpu_rate, default_gpu_rate, estimate_cost_via_provider

    block = _read_finops_cost()
    probe = estimate_cost_via_provider(1.0, 0.0)
    coeffs = dict(block.get("coefficients") or {})
    return {
        "gpu_rate": coeffs.get("gpu_rate", default_gpu_rate()),
        "cpu_rate": coeffs.get("cpu_rate", default_cpu_rate()),
        "provider": probe.get("provider"),
        "methodology": probe.get("methodology"),
        "cost_per_gpu_hour": probe.get("cost_usd"),
    }


# ── Deploy governed calc code (the providers system) ─────────────────────────


def deploy_provider(
    domain: str,
    name: str,
    code: str,
    *,
    project: str = "platform-ops",
    activate: bool = True,
    approve: bool = False,
) -> dict[str, Any]:
    """AST-gate, persist and (optionally) activate a calculation provider authored in Python.

    Wraps :func:`examlops.providers.save_provider` (which validates the source *before* it reaches
    disk) so "write Python and deploy it to ExaMLOps" goes through governance + audit instead of a
    raw ``.providers`` write. ``domain`` ∈ ``cost``/``carbon``/``drift``/``promotion``/``llm_*`` …
    """

    def _apply() -> dict[str, Any]:
        from examlops.providers import save_provider, set_active_provider

        info = save_provider(domain, name, code, project=project, actor=_resolved_actor())
        if activate:
            set_active_provider(project, domain, name)
            info["activated"] = True
        return info

    return _run(
        f"deploy_provider:{domain}",
        f"{project}/{domain}/{name}",
        _apply,
        details={"domain": domain, "name": name, "project": project, "activate": activate},
        approve=approve,
    )


def list_authored_providers(project: str = "platform-ops") -> list[dict[str, Any]]:
    """Read-only: a project's authored providers (name·domain·active·gate-status)."""
    from examlops.providers import list_project_providers

    return list_project_providers(project)


# ── Connections (ExaMLOps ↔ data-plane / S3 / URI) ───────────────────────────


def set_connection(
    name: str,
    kind: str,
    *,
    project: str | None = None,
    config: dict[str, Any] | None = None,
    secret_value: str | None = None,
    approve: bool = False,
) -> dict[str, Any]:
    """Create a named connection (``s3``/``uri``/``dataplane``) via ``examlops.connections``.

    The secret (if any) is written to the D7 secrets store and only a ``secret_ref`` is kept — the
    value is never audited or returned. Governed + audited (secret-safe details only).
    """

    def _apply() -> dict[str, Any]:
        from examlops.connections import create_connection

        return create_connection(
            name,
            kind,
            project=project,
            config=config,
            secret_value=secret_value,
            created_by=_resolved_actor(),
        )

    return _run(
        "set_connection",
        f"{project or 'global'}/{name}",
        _apply,
        details={
            "name": name,
            "kind": kind,
            "project": project,
            "has_secret": secret_value is not None,
        },
        approve=approve,
    )


# ── Bridge wiring (ExaMLOps ↔ SeanerBUS, per-model UUID) ──────────────────────


def set_bridge_uuid(
    model: str, *, regenerate: bool = False, approve: bool = False
) -> dict[str, Any]:
    """Assign or regenerate a model's ``seanerbus_uuid`` in its use-case YAML (bridge ↔ model map).

    Reuses the same YAML edit the ``exa seanerbus`` command performs. ``regenerate=False`` only
    assigns one when missing (idempotent); ``regenerate=True`` rotates an existing UUID — note that
    HPC teams must then update their config. Governed + audited.
    """
    import uuid as _uuid

    import yaml

    from examlops.cli.commands.seanerbus_cmd import _insert_uuid, _iter_yamls, _replace_uuid

    def _apply() -> dict[str, Any]:
        for name, path, text in _iter_yamls():
            if name.upper() != model.upper():
                continue
            raw = yaml.safe_load(text) or {}
            current = raw.get("seanerbus_uuid")
            if current is not None and not regenerate:
                return {"model": name, "uuid": current, "changed": False}
            new_uid = str(_uuid.uuid4())
            path.write_text(
                _replace_uuid(text, new_uid) if current is not None else _insert_uuid(text, new_uid)
            )
            return {"model": name, "uuid": new_uid, "changed": True, "previous": current}
        raise PlatformAdminError(f"model {model!r} not found in the active use-case pack")

    return _run(
        "set_bridge_uuid",
        model,
        _apply,
        details={"model": model, "regenerate": regenerate},
        approve=approve,
    )


# ── platform.db knobs (traffic / autoscale …) ────────────────────────────────

# domain → callable(model, params) reusing the CLI's data setters. Extend as more knobs are wired.
_KNOB_SETTERS: dict[str, Callable[[str, dict[str, Any]], None]] = {}


def _register_knobs() -> None:
    if _KNOB_SETTERS:
        return
    from examlops.data.serving import set_autoscale_config, set_traffic_rules

    _KNOB_SETTERS["traffic"] = lambda model, p: set_traffic_rules(
        model, dict(p), updated_by=_resolved_actor()
    )
    _KNOB_SETTERS["autoscale"] = lambda model, p: set_autoscale_config(model, **p)


def set_knob(
    domain: str, model: str, params: dict[str, Any], *, approve: bool = False
) -> dict[str, Any]:
    """Set a ``platform.db`` knob (``traffic``/``autoscale`` …) via the CLI's data setter. Governed + audited."""
    _register_knobs()
    setter = _KNOB_SETTERS.get(domain)
    if setter is None:
        raise PlatformAdminError(
            f"unsupported knob domain {domain!r} (known: {sorted(_KNOB_SETTERS)})"
        )

    def _apply() -> dict[str, Any]:
        setter(model, params)
        return {"domain": domain, "model": model, "params": params}

    return _run(
        f"set_knob:{domain}",
        model,
        _apply,
        details={"domain": domain, "model": model, "params": params},
        approve=approve,
    )


# ── Service URLs / tokens (config.toml) ──────────────────────────────────────


def set_config(*, approve: bool = False, **updates: Any) -> dict[str, Any]:
    """Set CLI/service config (URLs, tokens, contexts) via ``_config.write_config``. Governed + audited.

    Token values are redacted in the audit trail.
    """
    if not updates:
        raise PlatformAdminError("set_config: no updates given")

    def _apply() -> dict[str, Any]:
        from examlops.cli._config import write_config

        write_config(updates)
        return {"keys": sorted(updates)}

    redacted = {k: ("***" if "token" in k or "secret" in k else v) for k, v in updates.items()}
    return _run(
        "set_config",
        ",".join(sorted(updates)),
        _apply,
        details={"updates": redacted},
        approve=approve,
    )


# ── Tier B: stage a platform source change for review ────────────────────────


def propose_source_change(
    paths: list[str], message: str, *, approve: bool = False
) -> dict[str, Any]:
    """Record intent to change platform **source** (Tier B) — the review/redeploy path, not a hot patch.

    Editing real integration code (e.g. the ExaMLOps↔bridge connection) is deliberately *not*
    hot-applied: the edited files live on the deploy node's git tree and ship through
    ``dualgit ship`` + a service redeploy. This records a governed, audited ``platform_source_change`` intent
    (needs ``owner`` on ``platform:core``) and returns the exact commands to ship + redeploy. It does
    **not** itself commit, push or restart anything.
    """
    if not paths:
        raise PlatformAdminError("propose_source_change: no paths given")

    def _apply() -> dict[str, Any]:
        return {
            "paths": paths,
            "message": message,
            "next_steps": [
                "review the diff on the deploy node: git -C <repo> diff -- " + " ".join(paths),
                'ship via dualgit: dualgit doctor → dualgit ship "<message>"',
                "redeploy the affected service (e.g. make seanerbus-up / dashboard-up / control-plane-up)",
            ],
        }

    return _run(
        "platform_source_change",
        ",".join(paths),
        _apply,
        details={"paths": paths, "message": message},
        relation="owner",
        approve=approve,
    )


# ── Read side: the change feed (for the dashboard/notebook "see the results") ─


def recent_changes(
    limit: int = 50, *, sources: tuple[str, ...] = ("workbench", "dashboard")
) -> list[dict[str, Any]]:
    """Recent platform-management audit rows (newest first) — the change feed for the console."""
    from examlops.data import get_db, init_db

    init_db()
    placeholders = ",".join("?" for _ in sources)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT ts, source, actor, action, target, details FROM audit_events "
            f"WHERE source IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            (*sources, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]
