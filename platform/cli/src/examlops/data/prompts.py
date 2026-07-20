"""examlops.data.prompts — Prompt registry (B1).

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "create_prompt_version",
    "get_prompt_by_label",
    "get_prompt_version",
    "list_prompt_labels",
    "list_prompt_names",
    "list_prompt_versions",
    "set_prompt_label",
]


def create_prompt_version(
    name: str,
    template: str,
    *,
    variables: list[str] | None = None,
    tags: dict[str, Any] | None = None,
    actor: str | None = None,
) -> int:
    """Create a new immutable prompt version (spec R1). Returns the new version number."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM prompt_versions WHERE name=?", (name,)
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            """INSERT INTO prompt_versions (name, version, template, variables, tags, actor)
               VALUES (?,?,?,?,?,?)""",
            (
                name,
                version,
                template,
                json.dumps(variables or []),
                json.dumps(tags or {}),
                actor,
            ),
        )
    return version


def get_prompt_by_label(name: str, label: str) -> dict[str, Any] | None:
    """Resolve ``name@label`` to its pinned prompt version (spec R3)."""
    init_db()
    with get_db() as conn:
        lab = conn.execute(
            "SELECT version FROM prompt_labels WHERE name=? AND label=?", (name, label)
        ).fetchone()
    if lab is None:
        return None
    return get_prompt_version(name, int(lab["version"]))


def get_prompt_version(name: str, version: int) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM prompt_versions WHERE name=? AND version=?", (name, version)
        ).fetchone()
    return dict(row) if row else None


def list_prompt_labels(name: str) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_labels WHERE name=? ORDER BY label", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_prompt_names() -> list[str]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT name FROM prompt_versions ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def list_prompt_versions(name: str) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_versions WHERE name=? ORDER BY version DESC", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_prompt_label(name: str, label: str, version: int) -> None:
    """Point a label at a version (spec R8). Caller writes the audit event (R9)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO prompt_labels (name, label, version, updated_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(name, label) DO UPDATE SET
                   version=excluded.version, updated_at=CURRENT_TIMESTAMP""",
            (name, label, version),
        )


install_write_retry(__name__)
