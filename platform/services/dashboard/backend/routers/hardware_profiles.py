"""Hardware Profiles (ADR 0157 Phase 4) — dashboard surface over ``examlops.hardware_profiles``.

A hardware profile is a named, versioned resource+runtime bundle a workbench, a training run or a
serving deployment references instead of restating raw ``--gpus``/``--cpu`` flags. This router is
the dashboard half of ``exa hardware profile``:

* **Reads** (viewer): the catalog (``active`` version per name), one profile's versions + the
  version a label points at, a live ``resolve`` against a cluster, the resolution ledger
  (``history``) and ``in-use`` — every profile a running workbench or a recent training/serving
  resolution is bound to, with the honest status it got (``unchecked``/``verified``/``degraded``/
  ``unresolvable``, or ``missing`` when the bound version has since been deleted).
* **Writes** (admin, ``platform.manage``; audited as ``dashboard`` events): create a new immutable
  version (``POST``) and delete a version or a whole name (``DELETE``).

Like the connections/projects routers (Phase 42 edit parity), every call goes through the **same**
``examlops`` code paths the CLI uses — never a raw-SQL mirror — so validation, versioning and the
dangling-label rule cannot drift between the two. The ``examlops`` import is lazy and guarded: a
deployment without the package still boots, and these routes answer 503.
"""

from __future__ import annotations

from typing import Any

import audit_write
from auth import require_role
from capabilities import PLATFORM_MANAGE, can, deny_reason, require_capability
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

router = APIRouter(prefix="/v1/hardware-profiles", tags=["hardware-profiles"])
_viewer = require_role("viewer")
_admin = require_role("admin")

#: Upper bound on one ledger page — the same ceiling the CLI's ``history --limit`` enforces.
_MAX_LIMIT = 1000


def _hp():
    """Lazy, guarded import of the shared logic (503 if the examlops package is unavailable)."""
    try:
        from examlops import hardware_profiles as hp  # type: ignore

        return hp
    except ImportError as exc:  # pragma: no cover - only when examlops is absent
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "hardware profiles require the examlops package (not available in this deployment)",
        ) from exc


def _require_manage(principal: dict) -> None:
    role = principal.get("role", "")
    if not can(role, PLATFORM_MANAGE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, PLATFORM_MANAGE))


def _profile_view(p: Any) -> dict[str, Any]:
    return {
        "name": p.name,
        "version": p.version,
        "acceleratorFamily": p.accelerator_family,
        "acceleratorModelHint": p.accelerator_model_hint,
        "gpuCount": p.gpu_count,
        "gpuFraction": p.gpu_fraction,
        "migProfile": p.mig_profile,
        "cpu": p.cpu,
        "memoryGb": p.memory_gb,
        "nodes": p.nodes,
        "driverTag": p.driver_tag,
        "runtimeTag": p.runtime_tag,
        "applicability": list(p.applicability),
        "description": p.description,
        "createdAt": p.created_at,
        "createdBy": p.created_by,
    }


def _resolution_view(r: Any) -> dict[str, Any]:
    return {
        "name": r.name,
        "version": r.version,
        "status": r.status,
        "reason": r.reason,
        "resources": {
            "gpus": r.resources.gpus,
            "cpus": r.resources.cpus,
            "memoryGb": r.resources.memory_gb,
            "nodes": r.resources.nodes,
        },
        "unconfirmed": list(r.unconfirmed),
    }


@router.get("")
async def list_profiles(
    applicability: str | None = Query(default=None),
    _=Depends(_viewer),
) -> list[dict[str, Any]]:
    """Every profile by its ``active`` version, optionally filtered by applicability. Viewer.

    ``applicability=workbench`` also returns ``any`` profiles — the same rule the consumers apply,
    so the workbench create form offers exactly the profiles a create would accept.
    """
    hp = _hp()
    if applicability is not None and applicability not in hp.APPLICABILITIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"applicability must be one of {list(hp.APPLICABILITIES)}",
        )
    out = []
    for name in hp.list_names():
        p = hp.get_profile(name)
        if p is None:  # the 'active' label is dangling (its version was deleted)
            continue
        if applicability and not ({applicability, "any"} & set(p.applicability)):
            continue
        out.append(_profile_view(p))
    return out


def _read_scope(principal: dict, project: str | None) -> set[str | None] | None:
    """The projects this session may read ledger/in-use rows of (ADR 0014 tenancy), or ``None``.

    The ledger is project-scoped: its rows name a project's workbenches, the models it trains and
    who ran them. So these reads obey the same relationship check as the Projects router — an
    explicit ``project`` needs ``viewer`` on it (403 otherwise), and with
    ``EXAMLOPS_MULTITENANCY`` on an unscoped read is narrowed **in SQL** to the projects the
    session may read, rows with no project counting as project ``default`` (the rule
    ``capabilities.tenant_visible`` applies to a resource with no tenant). ``None`` = no narrowing
    (tenancy off, or a single explicitly authorised project).
    """
    from routers.projects import _project_allowed, _project_guard

    if project is not None:
        _project_guard(principal, "viewer", project)
        return None
    try:
        from examlops import authz  # type: ignore
        from examlops.data.projects import list_projects  # type: ignore
    except ImportError:  # pragma: no cover - examlops absent: tenancy cannot be on
        return None
    if not authz.multitenancy_enabled():
        return None
    scope: set[str | None] = {
        p["name"] for p in list_projects() if _project_allowed(principal, "viewer", p["name"])
    }
    if _project_allowed(principal, "viewer", "default"):
        scope.add(None)
    return scope


@router.get("/in-use")
async def in_use(
    days: float = Query(default=7.0, gt=0, le=365),
    project: str | None = Query(default=None),
    principal: dict = Depends(_viewer),
) -> dict[str, Any]:
    """Profiles bound to running workbenches / recent training+serving, with status. Viewer.

    Tenant-scoped (see :func:`_read_scope`): a session sees only the projects it may read.
    """
    hp = _hp()
    return hp.in_use_report(days=days, project=project, projects=_read_scope(principal, project))


@router.get("/history")
async def history(
    name: str | None = Query(default=None),
    consumer: str | None = Query(default=None),
    project: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=_MAX_LIMIT),
    principal: dict = Depends(_viewer),
) -> list[dict[str, Any]]:
    """The append-only resolution ledger, newest first; filters apply before the limit. Viewer.

    Tenant-scoped (see :func:`_read_scope`) in the ``WHERE`` clause, so the limit counts only
    rows the session may see.
    """
    hp = _hp()
    if consumer is not None and consumer not in hp.CONSUMERS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"consumer must be one of {list(hp.CONSUMERS)}"
        )
    from examlops.data.hardware_profiles import list_resolutions  # type: ignore

    return list_resolutions(
        name,
        consumer=consumer,
        project=project,
        projects=_read_scope(principal, project),
        limit=limit,
    )


@router.get("/{name}")
async def show_profile(
    name: str,
    version: int | None = Query(default=None, ge=1),
    label: str = Query(default="active"),
    _=Depends(_viewer),
) -> dict[str, Any]:
    """One profile version (default: the ``active`` label's) plus every version's summary. Viewer."""
    hp = _hp()
    p = hp.get_profile(name, label=label, version=version)
    if p is None:
        ref = f"version {version}" if version is not None else f"label '{label}'"
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"hardware profile '{name}' ({ref}) not found"
        )
    return {
        **_profile_view(p),
        "versions": [
            {"version": v.version, "createdAt": v.created_at, "createdBy": v.created_by}
            for v in hp.list_versions(name)
        ],
    }


@router.get("/{name}/resolve")
async def resolve(
    name: str,
    cluster: str = Query(..., min_length=1),
    version: int | None = Query(default=None, ge=1),
    label: str = Query(default="active"),
    _=Depends(_viewer),
) -> dict[str, Any]:
    """Resolve against a cluster's live capacity — never fabricates a capability. Viewer.

    Read-only: nothing is recorded (the ledger holds what consumers actually resolved, not what
    someone looked at). ``unresolvable`` is a 200 with that status, not an error — it is an answer.
    """
    hp = _hp()
    try:
        r = hp.resolve_profile(name, label=label, version=version, target_cluster=cluster)
    except hp.HardwareProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _resolution_view(r)


def _applicability(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ("any",)
    if isinstance(raw, str):
        return tuple(a.strip() for a in raw.split(",") if a.strip())
    if isinstance(raw, list) and all(isinstance(a, str) for a in raw):
        return tuple(a.strip() for a in raw if a.strip())
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "applicability must be a list of strings")


def _num(payload: dict, key: str, default: float, kind: type) -> Any:
    value = payload.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} must be a number")
    if kind is int and float(value) != int(value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} must be a whole number")
    return kind(value)


def _opt_str(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} must be a string")
    return value.strip() or None


@router.post("", status_code=status.HTTP_201_CREATED)
async def set_profile(
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    _gate: dict = Depends(require_capability(PLATFORM_MANAGE)),
) -> dict[str, Any]:
    """Create a new immutable version and move ``label`` (default ``active``) to it. Admin; audited.

    Body mirrors ``exa hardware profile set``: ``{name, acceleratorFamily, gpuCount?, gpuFraction?,
    migProfile?, cpu?, memoryGb?, nodes?, acceleratorModelHint?, driverTag?, runtimeTag?,
    applicability?, description?, label?}``. Validation is the shared module's, so an invalid shape
    is refused with the same message the CLI prints.
    """
    _require_manage(principal)
    hp = _hp()
    name = _opt_str(payload, "name")
    family = _opt_str(payload, "acceleratorFamily")
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")
    if not family:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "acceleratorFamily is required")
    label = _opt_str(payload, "label") or "active"
    actor = principal.get("sub", "?")
    try:
        p = hp.create_profile_version(
            name,
            accelerator_family=family,
            gpu_count=_num(payload, "gpuCount", 0, int),
            gpu_fraction=_num(payload, "gpuFraction", 1.0, float),
            mig_profile=_opt_str(payload, "migProfile"),
            cpu=_num(payload, "cpu", 0.0, float),
            memory_gb=_num(payload, "memoryGb", 0.0, float),
            nodes=_num(payload, "nodes", 1, int),
            accelerator_model_hint=_opt_str(payload, "acceleratorModelHint"),
            driver_tag=_opt_str(payload, "driverTag"),
            runtime_tag=_opt_str(payload, "runtimeTag"),
            applicability=_applicability(payload.get("applicability")),
            description=_opt_str(payload, "description") or "",
            label=label,
            created_by=actor,
        )
    except hp.HardwareProfileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit_write.audit(
        actor,
        "hardware_profile_set",
        name,
        {
            "version": p.version,
            "label": label,
            "accelerator_family": p.accelerator_family,
            "gpu_count": p.gpu_count,
            "applicability": list(p.applicability),
        },
    )
    return _profile_view(p)


@router.delete("/{name}")
async def delete_profile(
    name: str,
    version: int | None = Query(default=None, ge=1),
    principal: dict = Depends(_admin),
    _gate: dict = Depends(require_capability(PLATFORM_MANAGE)),
) -> dict[str, Any]:
    """Delete one version, or the whole name (every version + label). Admin; audited.

    Same rules as ``exa hardware profile delete``: a label pointing at a deleted version is left
    **dangling** (reported as ``danglingActive``), never silently re-pointed; and every consumer
    still bound to what was removed is named in ``inUse`` — they now report ``missing``.
    """
    _require_manage(principal)
    hp = _hp()
    from examlops.data import hardware_profiles as data  # type: ignore

    existing = hp.list_versions(name)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"hardware profile '{name}' not found")
    if version is not None and not any(v.version == version for v in existing):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"hardware profile '{name}' has no version {version}"
        )
    dangling_active = False
    if version is not None:
        active = data.resolve_label(name, "active")
        dangling_active = active is not None and int(active["version"]) == version
    in_use = [
        {"consumer": e["consumer"], "consumerRef": e["consumer_ref"], "version": e["version"]}
        for e in hp.in_use_report()["entries"]
        if e["name"] == name and (version is None or e["version"] == version)
    ]
    removed = data.delete_profile(name, version)
    audit_write.audit(
        principal.get("sub", "?"),
        "hardware_profile_deleted",
        name,
        {"version": version, "rows_removed": removed, "in_use": len(in_use)},
    )
    return {
        "name": name,
        "version": version,
        "deleted": removed,
        "danglingActive": dangling_active,
        "inUse": in_use,
    }
