"""examlops.data.genai_apps - storage for GenAI applications and their aliases (ADR 0159).

Policy (manifest schema, validation, the promotion gate) lives in :mod:`examlops.genai_apps`; this
module only touches ``genai_applications``, ``genai_app_aliases`` and ``genai_app_alias_history``.

The same two invariants :mod:`examlops.data.agent_versions` enforces, enforced here for the same
reasons: a version row is **insert-only** (there is no update helper for it; the id is a hash of
the content), and an alias move writes the pointer and its history row in one transaction, so
history can never disagree with the pointer.
"""

from __future__ import annotations

import json
import time
from typing import Any

from examlops.platform_db import (
    _immediate_write,
    get_db,
    init_db,
    install_write_retry,
    write_retry,
)

__all__ = [
    "alias_history",
    "get_alias",
    "get_version",
    "init_db",
    "insert_version",
    "list_aliases",
    "list_versions",
    "move_alias",
]


def _ver(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["manifest"] = json.loads(d["manifest_json"])
    return d


def insert_version(
    version_id: str,
    name: str,
    manifest_json: str,
    *,
    actor: str | None,
) -> tuple[bool, dict[str, Any]]:
    """``(created, row)``. An existing id is returned untouched (register is idempotent)."""

    def _do() -> tuple[bool, dict[str, Any]]:
        init_db()
        with _immediate_write("genai_applications") as conn:
            r = conn.execute(
                "SELECT * FROM genai_applications WHERE version_id=?", (version_id,)
            ).fetchone()
            if r is not None:
                return False, _ver(r)
            conn.execute(
                "INSERT INTO genai_applications (version_id, name, manifest_json, actor, "
                "created_at) VALUES (?,?,?,?,?)",
                (version_id, name, manifest_json, actor, time.time()),
            )
            r = conn.execute(
                "SELECT * FROM genai_applications WHERE version_id=?", (version_id,)
            ).fetchone()
            return True, _ver(r)

    return write_retry(_do)


def get_version(version_id: str) -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        init_db()
        with get_db() as conn:
            r = conn.execute(
                "SELECT * FROM genai_applications WHERE version_id=?", (version_id,)
            ).fetchone()
        return _ver(r) if r else None

    return write_retry(_do)


def list_versions(name: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        sql = "SELECT * FROM genai_applications"
        args: list[Any] = []
        if name:
            sql += " WHERE name=?"
            args.append(name)
        sql += " ORDER BY created_at DESC, version_id LIMIT ?"
        args.append(int(limit))
        with get_db() as conn:
            return [_ver(r) for r in conn.execute(sql, args).fetchall()]

    return write_retry(_do)


def get_alias(name: str, alias: str) -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        init_db()
        with get_db() as conn:
            r = conn.execute(
                "SELECT * FROM genai_app_aliases WHERE name=? AND alias=?", (name, alias)
            ).fetchone()
        return dict(r) if r else None

    return write_retry(_do)


def list_aliases(name: str | None = None) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        sql = "SELECT * FROM genai_app_aliases"
        args: list[Any] = []
        if name:
            sql += " WHERE name=?"
            args.append(name)
        sql += " ORDER BY name, alias"
        with get_db() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    return write_retry(_do)


def move_alias(
    name: str,
    alias: str,
    version_id: str,
    *,
    action: str,
    actor: str | None,
    reason: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> str | None:
    """Point ``name@alias`` at ``version_id`` and record the move; returns the previous id."""

    def _do() -> str | None:
        init_db()
        now = time.time()
        with _immediate_write("genai_app_aliases") as conn:
            r = conn.execute(
                "SELECT version_id FROM genai_app_aliases WHERE name=? AND alias=?", (name, alias)
            ).fetchone()
            prev = r["version_id"] if r else None
            if r is None:
                conn.execute(
                    "INSERT INTO genai_app_aliases (name, alias, version_id, actor, updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (name, alias, version_id, actor, now),
                )
            else:
                conn.execute(
                    "UPDATE genai_app_aliases SET version_id=?, actor=?, updated_at=? "
                    "WHERE name=? AND alias=?",
                    (version_id, actor, now, name, alias),
                )
            conn.execute(
                "INSERT INTO genai_app_alias_history (name, alias, version_id, prev_version, "
                "action, reason, evidence_json, actor, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    name,
                    alias,
                    version_id,
                    prev,
                    action,
                    reason,
                    json.dumps(evidence, sort_keys=True, default=str) if evidence else None,
                    actor,
                    now,
                ),
            )
            return prev

    return write_retry(_do)


def alias_history(name: str, alias: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first."""

    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM genai_app_alias_history WHERE name=? AND alias=? "
                "ORDER BY id DESC LIMIT ?",
                (name, alias, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    return write_retry(_do)


install_write_retry(__name__)
