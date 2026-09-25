"""P5 — Project Workbenches (ADR 0090).

On-demand, project-bound dev environments (the RHOAI *workbench* analogue). ExaMLOps records the
workbench intent + wiring in ``platform.db`` and injects the project's Named Connections (P2) as
environment variables; the actual spawn is delegated to the runtime (JupyterHub/Docker) behind the
``spawn``/``stop`` seam, consistent with ADR 0084's advisory boundary. Self-contained (own idempotent
table). A workbench is registered as a project resource (``kind='storage'``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from examlops.data import get_db, init_db
from examlops.data.projects import get_project

_DEFAULT_IMAGE = "jupyter/scipy-notebook:latest"

logger = logging.getLogger(__name__)


class WorkbenchError(Exception):
    """Raised for unknown workbench / unknown project / an unusable hardware profile."""


def _migrate_this_table(conn: Any) -> None:
    """Apply ``platform_db._COLUMN_MIGRATIONS['workbenches']`` to the table just ensured.

    The schema bootstrap runs those migrations too, but ``workbenches`` is *lazily* created — by
    this module, and by the dashboard router's own ``CREATE TABLE IF NOT EXISTS`` copy — so the
    bootstrap can legitimately run before the table exists and find nothing to migrate. A table
    created after that point (or by an older build) then keeps its old shape for the life of the
    process, and the first INSERT fails with ``no such column: hardware_profile``. Same mechanism,
    same single declaration, applied at the only other moment the table can appear.
    """
    from examlops.platform_db import _COLUMN_MIGRATIONS

    existing = {r[1] for r in conn.execute("PRAGMA table_info(workbenches)").fetchall()}
    for column, decl in _COLUMN_MIGRATIONS.get("workbenches", {}).items():
        if column not in existing:
            conn.execute(f"ALTER TABLE workbenches ADD COLUMN {column} {decl}")


def _ensure_table() -> None:
    # init_db() first: the two hardware-profile columns below are additive (ADR 0157 Phase 2), and
    # a database whose `workbenches` table predates them gets them from
    # platform_db._COLUMN_MIGRATIONS, which runs inside the schema bootstrap. CREATE TABLE IF NOT
    # EXISTS alone would leave such a table at its old shape forever. Cached per process/DB path,
    # so this costs nothing after the first call.
    init_db()
    with get_db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS workbenches (
                   name           TEXT NOT NULL,
                   project        TEXT NOT NULL,
                   image          TEXT NOT NULL DEFAULT 'jupyter/scipy-notebook:latest',
                   cpu            REAL,
                   memory_gb      REAL,
                   storage_volume TEXT,
                   status         TEXT NOT NULL DEFAULT 'STOPPED',  -- STOPPED | RUNNING
                   created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   created_by     TEXT,
                   hardware_profile         TEXT,     -- ADR 0157: profile this was created from
                   hardware_profile_version INTEGER,  -- the immutable version resolved at create
                   PRIMARY KEY (project, name)
               )"""
        )
        _migrate_this_table(conn)


def _env_key(*parts: str) -> str:
    raw = "_".join(parts)
    return re.sub(r"[^A-Z0-9_]", "_", raw.upper())


def connection_env(
    project: str, *, tenant: str = "default", actor: str | None = None
) -> dict[str, str]:
    """Resolve the project's Named Connections into environment variables (RHOAI injection).

    For each connection ``c``: every non-secret config key becomes ``EXA_CONN_<C>_<KEY>`` and its
    secret (if any) becomes ``EXA_CONN_<C>_SECRET``. Never raises — a connection that fails to
    resolve its secret is skipped for that value.
    """
    from examlops import connections as _conn

    env: dict[str, str] = {}
    for c in _conn.list_connections(project=project):
        name = c["name"]
        for k, v in c["config"].items():
            env[_env_key("EXA_CONN", name, k)] = str(v)
        if c.get("secret_ref"):
            try:
                resolved = _conn.resolve_connection(
                    name, project=project, tenant=tenant, actor=actor
                )
                if "secret" in resolved:
                    env[_env_key("EXA_CONN", name, "SECRET")] = str(resolved["secret"])
            except Exception:
                pass
    return env


def _apply_hardware_profile(
    profile_name: str, *, cpu: float | None, memory_gb: float | None
) -> tuple[float | None, float | None, str, int, tuple[str, ...]]:
    """Resolve a hardware profile into workbench defaults (ADR 0157 Phase 2, spec §4).

    Returns ``(cpu, memory_gb, name, version, overridden)``. A profile is a **default, not an
    override**: a field the caller passed explicitly is kept and named in ``overridden`` so the
    partial override is reported rather than applied silently.

    Refuses a profile whose ``applicability`` covers neither ``workbench`` nor ``any`` — naming
    what it actually declares, because silently ignoring the flag would hand the caller a
    workbench sized by nothing they asked for (the ADR 0041 portability-gate tone, not a shrug).
    """
    from examlops.hardware_profiles import HardwareProfileError, get_profile, resolve_profile

    try:
        # A workbench runs on the Docker/JupyterHub substrate, not an HPC cluster, so there is no
        # target cluster to check against: this resolution is `unchecked` by construction and the
        # call is what pins the exact version (the `active` label may move afterwards).
        resolution = resolve_profile(profile_name, target_cluster=None)
    except HardwareProfileError as exc:
        raise WorkbenchError(str(exc)) from exc
    # Re-read that exact version for the raw floats: ProfileResolution.resources.cpus is an int
    # (the admission seam's shape), which would silently truncate a fractional-core profile.
    profile = get_profile(profile_name, version=resolution.version)
    if profile is None:  # pragma: no cover — deleted between the two reads
        raise WorkbenchError(
            f"hardware profile {profile_name!r} version {resolution.version} no longer exists"
        )
    if not {"workbench", "any"} & set(profile.applicability):
        raise WorkbenchError(
            f"hardware profile {profile_name!r} (version {resolution.version}) declares "
            f"applicability '{','.join(profile.applicability)}' — a workbench needs 'workbench' "
            "or 'any'. Create a profile with --applicability workbench, or pass --cpu/--memory-gb "
            "directly."
        )

    overridden: list[str] = []
    if cpu is None:
        cpu = profile.cpu
    else:
        overridden.append("cpu")
    if memory_gb is None:
        memory_gb = profile.memory_gb
    else:
        overridden.append("memory_gb")
    if overridden:
        logger.info(
            "hardware profile %s v%s applied partially: %s kept from the explicit argument(s); "
            "the remaining field(s) came from the profile",
            profile.name,
            resolution.version,
            ", ".join(overridden),
        )
    return cpu, memory_gb, profile.name, resolution.version, tuple(overridden)


def create_workbench(
    name: str,
    project: str,
    *,
    image: str | None = None,
    cpu: float | None = None,
    memory_gb: float | None = None,
    hardware_profile: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Define a workbench in a project (status STOPPED). Raises if the project is unknown.

    ``hardware_profile`` (ADR 0157 Phase 2) names a profile whose ``cpu``/``memory_gb`` become the
    workbench's defaults and whose name + resolved version are recorded on the row. Explicit
    ``cpu``/``memory_gb`` win over the profile (see :func:`_apply_hardware_profile`). Omitted, the
    path is exactly what it was before profiles existed.
    """
    if not get_project(project):
        raise WorkbenchError(f"project {project!r} not found")
    profile_name: str | None = None
    profile_version: int | None = None
    if hardware_profile is not None:
        cpu, memory_gb, profile_name, profile_version, _ = _apply_hardware_profile(
            hardware_profile, cpu=cpu, memory_gb=memory_gb
        )
    _ensure_table()
    volume = f"{project}-{name}-data"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO workbenches (name, project, image, cpu, memory_gb, storage_volume,
                                        created_by, hardware_profile, hardware_profile_version)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                name,
                project,
                image or _DEFAULT_IMAGE,
                cpu,
                memory_gb,
                volume,
                created_by,
                profile_name,
                profile_version,
            ),
        )
    from examlops.data.projects import assign_resource_to_project

    assign_resource_to_project(project, "storage", f"workbench:{name}", added_by=created_by)
    if profile_name is not None and profile_version is not None:
        _record_profile_use(project, name, profile_name, profile_version, actor=created_by)
    return get_workbench(name, project)  # type: ignore[return-value]


def _record_profile_use(
    project: str, name: str, profile_name: str, profile_version: int, *, actor: str | None
) -> None:
    """Ledger the resolution this workbench was created from (ADR 0157 Phase 4).

    Written only after the row exists, so a refused or conflicting create leaves no trace. The
    resolution is re-taken at the pinned version — ``unchecked``, because a workbench has no HPC
    target — and the write is fail-open (see ``hardware_profiles.record_resolution``).
    """
    from examlops.hardware_profiles import (  # noqa: PLC0415
        HardwareProfileError,
        record_resolution,
        resolve_profile,
    )

    try:
        resolution = resolve_profile(profile_name, version=profile_version)
    except HardwareProfileError as exc:  # deleted between create and here — say so, don't fail
        logger.warning("workbench %s/%s: profile resolution not recorded (%s)", project, name, exc)
        return
    except Exception as exc:  # noqa: BLE001 - the row exists: a failed re-read must not undo it
        # The workbench is already created; raising here would report a failed create for a
        # workbench that exists (and a retry would then conflict). Same fail-open contract as
        # ``record_resolution`` — the in-use report shows it as ``unchecked`` ("no recorded
        # resolution"), never as fine.
        logger.warning("workbench %s/%s: profile resolution not recorded (%s)", project, name, exc)
        return
    record_resolution(
        resolution,
        consumer="workbench",
        consumer_ref=f"{project}/{name}",
        project=project,
        actor=actor,
    )


def get_workbench(name: str, project: str) -> dict[str, Any] | None:
    _ensure_table()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM workbenches WHERE project=? AND name=?", (project, name)
        ).fetchone()
    return dict(row) if row else None


def list_workbenches(project: str | None = None) -> list[dict[str, Any]]:
    _ensure_table()
    with get_db() as conn:
        if project is None:
            rows = conn.execute("SELECT * FROM workbenches ORDER BY project, name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workbenches WHERE project=? ORDER BY name", (project,)
            ).fetchall()
    return [dict(r) for r in rows]


def _set_status(name: str, project: str, status: str) -> bool:
    _ensure_table()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE workbenches SET status=? WHERE project=? AND name=?", (status, project, name)
        )
        return cur.rowcount > 0


def start_workbench(name: str, project: str, *, actor: str | None = None) -> dict[str, Any]:
    """Mark a workbench RUNNING and return its launch spec (image, mounted volume, injected env).

    The returned spec is what a runtime backend (JupyterHub/Docker) consumes to spawn the pod;
    ExaMLOps records intent + wiring and leaves enforcement to that runtime (ADR 0084 boundary).
    """
    wb = get_workbench(name, project)
    if not wb:
        raise WorkbenchError(f"workbench {name!r} not found in project {project!r}")
    if not _set_status(name, project, "RUNNING"):
        raise WorkbenchError("failed to update status")
    return {
        "name": name,
        "project": project,
        "image": wb["image"],
        "volume": wb["storage_volume"],
        "env": connection_env(project, actor=actor),
        "status": "RUNNING",
    }


def stop_workbench(name: str, project: str) -> bool:
    """Mark a workbench STOPPED. Returns True if it existed."""
    return _set_status(name, project, "STOPPED")


def delete_workbench(name: str, project: str) -> bool:
    """Delete a workbench definition. Returns True if removed."""
    _ensure_table()
    with get_db() as conn:
        cur = conn.execute("DELETE FROM workbenches WHERE project=? AND name=?", (project, name))
        removed = cur.rowcount > 0
    if removed:
        from examlops.data.projects import remove_project_resource

        remove_project_resource(project, "storage", f"workbench:{name}")
    return removed
