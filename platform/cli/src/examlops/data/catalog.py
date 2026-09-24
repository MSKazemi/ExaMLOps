"""examlops.data.catalog — storage for the Model Catalog (ADR 0158, spec §1.2).

Policy (manifest schema, validation, the publish trust gate, the pull) lives in
:mod:`examlops.catalog`; this module only touches ``catalog_entries`` and ``catalog_pulls``. Same
per-domain split every other ``data/`` module follows (mirrors ``examlops/data/prompts.py`` and
``examlops/data/hardware_profiles.py``); ``install_write_retry(__name__)`` re-applies the item-0.4
auto-wrapping and ``platform_db`` re-exports these names for backward compatibility.

Two invariants are enforced here, not in the callers:

* a ``catalog_entries`` row is **insert-only** — there is no update helper, because a correction
  is a new ``catalog_version`` (ADR 0158 decision 1), exactly as ``agent_versions`` rows are;
* publishing the same content twice under one name is **idempotent** — ``(name, entry_hash)`` is
  looked up first and the existing row returned untouched, so a re-run of a seeding script does
  not grow the catalog by a version that changed nothing.
"""

from __future__ import annotations

from typing import Any

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "count_pulls",
    "get_entry_row",
    "insert_entry",
    "latest_entry_row",
    "list_entry_names",
    "list_entry_rows",
    "list_pull_rows",
    "record_pull",
]


def insert_entry(
    name: str,
    entry_hash: str,
    manifest_json: str,
    *,
    kind: str,
    source_kind: str,
    source_ref: str,
    license_id: str,
    trust_tier: str,
    resource_hint: str | None = None,
    eval_suite: str | None = None,
    eval_model_version: str | None = None,
    supplychain_ref: str | None = None,
    model_yaml_template: str | None = None,
    description: str = "",
    published_by: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """``(created, row)``. Identical content under the same name returns the existing row.

    The new ``catalog_version`` is ``MAX(catalog_version)+1`` for ``name``, assigned here so the
    number is allocated under the same write as the insert.
    """
    init_db()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM catalog_entries WHERE name=? AND entry_hash=?", (name, entry_hash)
        ).fetchone()
        if existing is not None:
            return False, dict(existing)
        row = conn.execute(
            "SELECT COALESCE(MAX(catalog_version), 0) AS v FROM catalog_entries WHERE name=?",
            (name,),
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            """INSERT INTO catalog_entries
                   (name, catalog_version, entry_hash, kind, source_kind, source_ref, license,
                    resource_hint, eval_suite, eval_model_version, supplychain_ref, trust_tier,
                    model_yaml_template, description, published_by, manifest_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name,
                version,
                entry_hash,
                kind,
                source_kind,
                source_ref,
                license_id,
                resource_hint,
                eval_suite,
                eval_model_version,
                supplychain_ref,
                trust_tier,
                model_yaml_template,
                description,
                published_by,
                manifest_json,
            ),
        )
        created = conn.execute(
            "SELECT * FROM catalog_entries WHERE name=? AND catalog_version=?", (name, version)
        ).fetchone()
    return True, dict(created)


def get_entry_row(name: str, catalog_version: int) -> dict[str, Any] | None:
    """One immutable entry version, or ``None``."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM catalog_entries WHERE name=? AND catalog_version=?",
            (name, int(catalog_version)),
        ).fetchone()
    return dict(row) if row else None


def latest_entry_row(name: str) -> dict[str, Any] | None:
    """The highest ``catalog_version`` of ``name``, or ``None``."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM catalog_entries WHERE name=? ORDER BY catalog_version DESC LIMIT 1",
            (name,),
        ).fetchone()
    return dict(row) if row else None


def list_entry_rows(*, latest_only: bool = True) -> list[dict[str, Any]]:
    """Every entry, by name. ``latest_only`` keeps one row per name (its newest version)."""
    init_db()
    with get_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM catalog_entries ORDER BY name, catalog_version DESC"
            ).fetchall()
        ]
    if not latest_only:
        return rows
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["name"] in seen:
            continue
        seen.add(row["name"])
        out.append(row)
    return out


def list_entry_names() -> list[str]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT name FROM catalog_entries ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def record_pull(
    entry: str,
    catalog_version: int,
    entry_hash: str,
    project: str,
    model_name: str,
    *,
    actor: str | None = None,
) -> None:
    """Append the pull record (ADR 0158 decision 2). Append-only: a pull is a historical fact."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO catalog_pulls
                   (entry, catalog_version, entry_hash, project, model_name, actor)
               VALUES (?,?,?,?,?,?)""",
            (entry, int(catalog_version), entry_hash, project, model_name, actor),
        )


def list_pull_rows(
    *, project: str | None = None, entry: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Pull records, newest first."""
    init_db()
    sql = "SELECT * FROM catalog_pulls"
    where: list[str] = []
    args: list[Any] = []
    if project:
        where.append("project=?")
        args.append(project)
    if entry:
        where.append("entry=?")
        args.append(entry)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def count_pulls() -> int:
    """How many pulls have been recorded — the ``--dry-run`` writes-nothing check reads this."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM catalog_pulls").fetchone()
    return int(row["n"])


install_write_retry(__name__)
