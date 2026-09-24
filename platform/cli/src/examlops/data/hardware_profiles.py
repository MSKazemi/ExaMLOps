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

from typing import Any

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "create_profile_version",
    "delete_profile",
    "get_profile_version",
    "list_profile_names",
    "list_profile_versions",
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


install_write_retry(__name__)
