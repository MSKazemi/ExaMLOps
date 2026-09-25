"""examlops.data.hardware_profiles — Hardware Profiles registry (ADR 0157, spec §2.1).

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), the same
convention every other domain in this repo follows for its ``data/`` module (mirrors
``examlops/data/prompts.py``, ``examlops/data/hpc.py``). Shared primitives are imported from
``platform_db``. ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.

Persistence only — a named, versioned ``<name, version>`` row plus a mutable ``<name, label>``
pointer, the exact shape already proven for the prompt registry. The pure logic (resolution,
adapters into the existing resource-ask shapes) lives in the sibling top-level module
``examlops.hardware_profiles``, which is the only intended caller of these helpers outside tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "create_profile_version",
    "delete_profile",
    "get_profile_version",
    "list_profile_names",
    "list_latest_resolutions",
    "list_profile_versions",
    "list_resolutions",
    "record_resolution",
    "resolve_label",
    "set_profile_label",
]


def create_profile_version(
    name: str,
    *,
    accelerator_family: str,
    gpu_count: int = 0,
    gpu_fraction: float = 1.0,
    mig_profile: str | None = None,
    cpu: float = 0.0,
    memory_gb: float = 0.0,
    nodes: int = 1,
    accelerator_model_hint: str | None = None,
    driver_tag: str | None = None,
    runtime_tag: str | None = None,
    applicability: tuple[str, ...] = ("any",),
    description: str = "",
    created_by: str | None = None,
) -> int:
    """Create a new **immutable** hardware-profile version (spec §2). Returns the new version.

    Never edits an existing row — a ``set`` always inserts ``MAX(version)+1`` for ``name``, the
    same immutable-version shape ``data/prompts.py`` uses for prompts. Field-level validation
    (accelerator family, non-empty applicability) is the caller's job — the sibling
    ``examlops.hardware_profiles.create_profile_version`` — so this stays a thin, honest writer.
    """
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM hardware_profile_versions WHERE name=?",
            (name,),
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            """INSERT INTO hardware_profile_versions
                   (name, version, accelerator_family, accelerator_model_hint, gpu_count,
                    gpu_fraction, mig_profile, cpu, memory_gb, nodes, driver_tag, runtime_tag,
                    applicability, description, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name,
                version,
                accelerator_family,
                accelerator_model_hint,
                gpu_count,
                gpu_fraction,
                mig_profile,
                cpu,
                memory_gb,
                nodes,
                driver_tag,
                runtime_tag,
                ",".join(applicability),
                description,
                created_by,
            ),
        )
    return version


def get_profile_version(name: str, version: int) -> dict[str, Any] | None:
    """One immutable version row, or ``None``."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM hardware_profile_versions WHERE name=? AND version=?", (name, version)
        ).fetchone()
    return dict(row) if row else None


def resolve_label(name: str, label: str = "active") -> dict[str, Any] | None:
    """Resolve ``name@label`` (default ``active``) to its pinned version row, or ``None``."""
    init_db()
    with get_db() as conn:
        lab = conn.execute(
            "SELECT version FROM hardware_profile_labels WHERE name=? AND label=?", (name, label)
        ).fetchone()
    if lab is None:
        return None
    return get_profile_version(name, int(lab["version"]))


def list_profile_versions(name: str) -> list[dict[str, Any]]:
    """Every version of ``name``, newest first."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM hardware_profile_versions WHERE name=? ORDER BY version DESC", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_profile_names() -> list[str]:
    """Every distinct profile name that has at least one version."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT name FROM hardware_profile_versions ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]


def set_profile_label(name: str, label: str, version: int) -> None:
    """Point ``label`` at ``version`` (creating or moving it). Caller writes the audit event."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO hardware_profile_labels (name, label, version, updated_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(name, label) DO UPDATE SET
                   version=excluded.version, updated_at=CURRENT_TIMESTAMP""",
            (name, label, version),
        )


def delete_profile(name: str, version: int | None = None) -> int:
    """Delete one version (``version`` given) or the whole name (every version + every label).

    Returns the number of ``hardware_profile_versions`` rows removed. Deleting a single version
    never re-points a label that was pointing at it — a label whose target version no longer
    exists is left **dangling** (ADR 0157 GWT-4); detecting and warning about that is the
    caller's job (it needs the *before* state, which this function does not return), typically
    ``exa hardware profile delete``.
    """
    init_db()
    with get_db() as conn:
        if version is None:
            cur = conn.execute("DELETE FROM hardware_profile_versions WHERE name=?", (name,))
            conn.execute("DELETE FROM hardware_profile_labels WHERE name=?", (name,))
        else:
            cur = conn.execute(
                "DELETE FROM hardware_profile_versions WHERE name=? AND version=?",
                (name, version),
            )
    return cur.rowcount


#: Hard ceiling on one ledger read — a caller asking for more gets this many, never an unbounded
#: scan of an append-only table.
MAX_RESOLUTION_ROWS = 1000


def record_resolution(
    name: str,
    version: int,
    *,
    consumer: str,
    consumer_ref: str,
    status: str,
    reason: str = "",
    unconfirmed: tuple[str, ...] = (),
    target_cluster: str | None = None,
    project: str | None = None,
    actor: str | None = None,
) -> int:
    """Append one resolution to the ``hardware_profile_resolutions`` ledger (ADR 0157 Phase 4).

    Append-only: a row is never updated, so the ledger answers "which exact version did that
    consumer resolve, and what status did it get" for every point in time. Returns the row id.
    """
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO hardware_profile_resolutions
                   (name, version, consumer, consumer_ref, project, target_cluster, status,
                    reason, unconfirmed, actor)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                name,
                int(version),
                consumer,
                consumer_ref,
                project,
                target_cluster,
                status,
                reason,
                ",".join(unconfirmed),
                actor,
            ),
        )
        return int(cur.lastrowid or 0)


def _where(
    *,
    name: str | None = None,
    consumer: str | None = None,
    consumer_ref: str | None = None,
    project: str | None = None,
    projects: Iterable[str | None] | None = None,
    since_days: float | None = None,
) -> tuple[list[str], list[Any]]:
    """The shared ``WHERE`` of every ledger read — so no reader can filter after its ``LIMIT``.

    ``projects`` is the tenant scope: the set of projects the caller may read. ``None`` in it
    admits rows recorded with no project (a serving model, a training run given no
    ``--project``). An empty ``projects`` matches nothing — default-deny, never "no filter".
    """
    where: list[str] = []
    params: list[Any] = []
    for col, val in (
        ("name", name),
        ("consumer", consumer),
        ("consumer_ref", consumer_ref),
        ("project", project),
    ):
        if val is not None:
            where.append(f"{col}=?")
            params.append(val)
    if projects is not None:
        scope = set(projects)
        named = sorted(p for p in scope if p is not None)
        clauses = []
        if named:
            clauses.append(f"project IN ({','.join('?' * len(named))})")
            params.extend(named)
        if None in scope:
            clauses.append("project IS NULL")
        where.append("(" + " OR ".join(clauses) + ")" if clauses else "1=0")
    if since_days is not None:
        where.append("ts >= datetime('now', ?)")
        params.append(f"-{float(since_days)} days")
    return where, params


def _rows_out(rows: Iterable[Any]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = dict(r)
        d["unconfirmed"] = [f for f in (d.get("unconfirmed") or "").split(",") if f]
        out.append(d)
    return out


def list_resolutions(
    name: str | None = None,
    *,
    consumer: str | None = None,
    consumer_ref: str | None = None,
    project: str | None = None,
    projects: Iterable[str | None] | None = None,
    since_days: float | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Ledger rows, newest first. Every filter is applied in SQL **before** the ``LIMIT``.

    ``limit`` is clamped to ``[1, MAX_RESOLUTION_ROWS]``. ``project`` scopes the read to one
    project's rows and ``projects`` to the set a caller may read (the tenant filter) — both are
    part of the ``WHERE`` clause, never a Python filter over an already-limited page. Ties on
    ``ts`` (one-second resolution) break on ``id``.
    """
    init_db()
    where, params = _where(
        name=name,
        consumer=consumer,
        consumer_ref=consumer_ref,
        project=project,
        projects=projects,
        since_days=since_days,
    )
    bounded = max(1, min(int(limit), MAX_RESOLUTION_ROWS))
    sql = "SELECT * FROM hardware_profile_resolutions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(bounded)
    with get_db() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return _rows_out(rows)


def list_latest_resolutions(
    consumer: str,
    *,
    project: str | None = None,
    projects: Iterable[str | None] | None = None,
    since_days: float | None = None,
    limit: int = MAX_RESOLUTION_ROWS,
) -> tuple[list[dict[str, Any]], bool]:
    """The **latest** ledger row per ``consumer_ref`` of one consumer kind, newest first.

    The "latest per consumer" reduction is done in SQL (``MAX(id) … GROUP BY consumer_ref``), so
    the ``LIMIT`` bounds *consumers*, not raw rows: a consumer that re-resolves often can never
    push a quiet one out of the page. Returns ``(rows, truncated)``; ``truncated`` is ``True``
    when more consumers matched than ``limit`` (clamped to ``[1, MAX_RESOLUTION_ROWS]``), so a
    caller can say its report is partial instead of presenting it as complete.
    """
    init_db()
    where, params = _where(
        consumer=consumer, project=project, projects=projects, since_days=since_days
    )
    bounded = max(1, min(int(limit), MAX_RESOLUTION_ROWS))
    sql = (
        "SELECT r.* FROM hardware_profile_resolutions r JOIN ("
        "SELECT MAX(id) AS mid FROM hardware_profile_resolutions WHERE "
        + " AND ".join(where)
        + " GROUP BY consumer_ref) m ON r.id = m.mid ORDER BY r.id DESC LIMIT ?"
    )
    params.append(bounded + 1)
    with get_db() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return _rows_out(rows[:bounded]), len(rows) > bounded


install_write_retry(__name__)
