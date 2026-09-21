"""examlops.data.agent_versions - storage for agent versions and their aliases (ADR 0146).

Policy (manifest schema, validation, the promotion gate) lives in :mod:`examlops.agent_versions`;
this module only touches ``agent_versions``, ``agent_aliases`` and ``agent_alias_history``.

Two invariants are enforced here, not in the callers: a version row is **insert-only** (there is no
update helper for it; the id is a hash of the content), and an alias move writes the pointer and
its history row in one transaction, so history can never disagree with the pointer.
"""

from __future__ import annotations

import json
import time
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

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
    agent: str,
    manifest_json: str,
    *,
    actor: str | None,
    signature: str | None = None,
    sign_algo: str | None = None,
    sign_key_id: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """``(created, row)``. An existing id is returned untouched (register is idempotent)."""

    def _do() -> tuple[bool, dict[str, Any]]:
        init_db()
        with _immediate_write("agent_versions") as conn:
            r = conn.execute(
                "SELECT * FROM agent_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if r is not None:
                return False, _ver(r)
            conn.execute(
                "INSERT INTO agent_versions (version_id, agent, manifest_json, signature, "
                "sign_algo, sign_key_id, actor, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    agent,
                    manifest_json,
                    signature,
                    sign_algo,
                    sign_key_id,
                    actor,
                    time.time(),
                ),
            )
            r = conn.execute(
                "SELECT * FROM agent_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            return True, _ver(r)

    return write_retry(_do)


def get_version(version_id: str) -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        init_db()
        with get_db() as conn:
            r = conn.execute(
                "SELECT * FROM agent_versions WHERE version_id=?", (version_id,)
            ).fetchone()
        return _ver(r) if r else None

    return write_retry(_do)


def list_versions(agent: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        sql = "SELECT * FROM agent_versions"
        args: list[Any] = []
        if agent:
            sql += " WHERE agent=?"
            args.append(agent)
        sql += " ORDER BY created_at DESC, version_id LIMIT ?"
        args.append(int(limit))
        with get_db() as conn:
            return [_ver(r) for r in conn.execute(sql, args).fetchall()]

    return write_retry(_do)


def get_alias(agent: str, alias: str) -> dict[str, Any] | None:
    def _do() -> dict[str, Any] | None:
        init_db()
        with get_db() as conn:
            r = conn.execute(
                "SELECT * FROM agent_aliases WHERE agent=? AND alias=?", (agent, alias)
            ).fetchone()
        return dict(r) if r else None

    return write_retry(_do)


def list_aliases(agent: str | None = None) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        sql = "SELECT * FROM agent_aliases"
        args: list[Any] = []
        if agent:
            sql += " WHERE agent=?"
            args.append(agent)
        sql += " ORDER BY agent, alias"
        with get_db() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    return write_retry(_do)


def move_alias(
    agent: str,
    alias: str,
    version_id: str,
    *,
    action: str,
    actor: str | None,
    reason: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> str | None:
    """Point ``agent@alias`` at ``version_id`` and record the move; returns the previous id."""

    def _do() -> str | None:
        init_db()
        now = time.time()
        with _immediate_write("agent_aliases") as conn:
            r = conn.execute(
                "SELECT version_id FROM agent_aliases WHERE agent=? AND alias=?", (agent, alias)
            ).fetchone()
            prev = r["version_id"] if r else None
            if r is None:
                conn.execute(
                    "INSERT INTO agent_aliases (agent, alias, version_id, actor, updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (agent, alias, version_id, actor, now),
                )
            else:
                conn.execute(
                    "UPDATE agent_aliases SET version_id=?, actor=?, updated_at=? "
                    "WHERE agent=? AND alias=?",
                    (version_id, actor, now, agent, alias),
                )
            conn.execute(
                "INSERT INTO agent_alias_history (agent, alias, version_id, prev_version, action, "
                "reason, evidence_json, actor, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    agent,
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


def alias_history(agent: str, alias: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first."""

    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_alias_history WHERE agent=? AND alias=? "
                "ORDER BY id DESC LIMIT ?",
                (agent, alias, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    return write_retry(_do)
