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
    "audit_for_version",
    "enqueue_reeval",
    "gate_reports_for",
    "get_alias",
    "get_version",
    "history_for_version",
    "init_db",
    "insert_version",
    "latest_moves",
    "list_aliases",
    "list_reevals",
    "list_rollouts",
    "list_versions",
    "move_alias",
    "open_blocking_reevals",
    "resolve_reeval",
    "set_rollout",
    "versions_mentioning",
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


def history_for_version(agent: str, version_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Every alias move that set, replaced or restored ``version_id`` (oldest first)."""

    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_alias_history WHERE agent=? AND "
                "(version_id=? OR prev_version=?) ORDER BY id LIMIT ?",
                (agent, version_id, version_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    return write_retry(_do)


def latest_moves(agent: str) -> list[dict[str, Any]]:
    """The newest history row of each alias of ``agent``."""

    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT h.* FROM agent_alias_history h JOIN (SELECT alias, MAX(id) AS id "
                "FROM agent_alias_history WHERE agent=? GROUP BY alias) m ON h.id = m.id",
                (agent,),
            ).fetchall()
        return [dict(r) for r in rows]

    return write_retry(_do)


def _like(text: str) -> str:
    """Escape LIKE wildcards: an agent named ``job_doc`` must not match ``jobXdoc``."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def audit_for_version(
    agent: str, version_id: str, *, tenant: str | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    """Audit events about ``agent`` that name ``version_id``; every filter is applied in SQL,
    before the ``LIMIT``, so a busy agent cannot push this version's record out of the page."""

    def _do() -> list[dict[str, Any]]:
        init_db()
        sql = (
            "SELECT * FROM audit_events WHERE (target LIKE ? ESCAPE '\\' OR target LIKE ? "
            "ESCAPE '\\') AND details LIKE ? ESCAPE '\\'"
        )
        a, v = _like(agent), _like(version_id)
        args: list[Any] = [f"{a}:%", f"{a}@%", f"%{v}%"]
        if tenant is not None:
            sql += " AND tenant=?"
            args.append(tenant)
        sql += " ORDER BY id LIMIT ?"
        args.append(int(limit))
        with get_db() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    return write_retry(_do)


def gate_reports_for(model: str, candidate: str, *, limit: int = 1000) -> list[dict[str, Any]]:
    """Gate reports whose candidate is exactly ``candidate`` (filtered in SQL, oldest first)."""

    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM gate_reports WHERE model=? AND candidate=? ORDER BY id LIMIT ?",
                (model, candidate, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    return write_retry(_do)


# -- session rollout (ADR 0146 d4) ------------------------------------------------------------


def set_rollout(agent: str, canary_percent: float, *, actor: str | None) -> float | None:
    """Set the canary share of new sessions; returns the previous share (None when unset)."""

    def _do() -> float | None:
        init_db()
        with _immediate_write("agent_rollouts") as conn:
            r = conn.execute(
                "SELECT canary_percent FROM agent_rollouts WHERE agent=?", (agent,)
            ).fetchone()
            conn.execute(
                "INSERT INTO agent_rollouts (agent, canary_percent, actor, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(agent) DO UPDATE SET "
                "canary_percent=excluded.canary_percent, actor=excluded.actor, "
                "updated_at=excluded.updated_at",
                (agent, float(canary_percent), actor, time.time()),
            )
            return float(r["canary_percent"]) if r else None

    return write_retry(_do)


def list_rollouts() -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM agent_rollouts ORDER BY agent").fetchall()
            ]

    return write_retry(_do)


# -- follow-binding re-evaluation (ADR 0146 d2) -----------------------------------------------


def versions_mentioning(needle: str, *, after: str = "", batch: int = 500) -> list[dict[str, Any]]:
    """A page of versions whose manifest text contains ``needle``, keyed after ``after``.

    A coarse SQL pre-filter; the caller checks the parsed manifest exactly. Paged by
    ``version_id`` so a registry of any size is walked in bounded batches.
    """

    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_versions WHERE manifest_json LIKE ? ESCAPE '\\' "
                "AND version_id > ? ORDER BY version_id LIMIT ?",
                (f"%{_like(needle)}%", after, int(batch)),
            ).fetchall()
        return [_ver(r) for r in rows]

    return write_retry(_do)


def enqueue_reeval(
    agent: str,
    version_id: str,
    servable: str,
    alias: str,
    model_version: str | None,
    previous_version: str | None,
    *,
    blocking: bool,
    actor: str | None,
) -> tuple[bool, dict[str, Any]]:
    """``(created, row)``; the same (version, servable, alias, model version) is enqueued once."""

    def _do() -> tuple[bool, dict[str, Any]]:
        init_db()
        with _immediate_write("agent_reeval_queue") as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO agent_reeval_queue (agent, version_id, servable, alias, "
                "model_version, previous_version, blocking, actor, enqueued_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    agent,
                    version_id,
                    servable,
                    alias,
                    model_version,
                    previous_version,
                    1 if blocking else 0,
                    actor,
                    time.time(),
                ),
            )
            r = conn.execute(
                "SELECT * FROM agent_reeval_queue WHERE version_id=? AND servable=? AND alias=? "
                "AND model_version IS ?",
                (version_id, servable, alias, model_version),
            ).fetchone()
            return cur.rowcount == 1, dict(r)

    return write_retry(_do)


def list_reevals(
    *, agent: str | None = None, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    def _do() -> list[dict[str, Any]]:
        init_db()
        sql = "SELECT * FROM agent_reeval_queue WHERE 1=1"
        args: list[Any] = []
        if agent:
            sql += " AND agent=?"
            args.append(agent)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with get_db() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    return write_retry(_do)


def open_blocking_reevals(*, after_id: int = 0, limit: int = 10_000) -> list[dict[str, Any]]:
    """A page of blocking entries not yet ``passed`` (pending or failed), oldest first, with
    ``id > after_id`` - callers page until a short page, never trusting one window."""

    def _do() -> list[dict[str, Any]]:
        init_db()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_reeval_queue WHERE blocking=1 AND "
                "status IN ('pending','failed') AND id > ? ORDER BY id LIMIT ?",
                (int(after_id), max(1, int(limit))),
            ).fetchall()
        return [dict(r) for r in rows]

    return write_retry(_do)


def resolve_reeval(
    reeval_id: int, status: str, *, reason: str | None, actor: str | None
) -> dict[str, Any] | None:
    """Close a pending entry as ``passed``/``failed``; None when unknown or already closed."""

    def _do() -> dict[str, Any] | None:
        init_db()
        with _immediate_write("agent_reeval_queue") as conn:
            cur = conn.execute(
                "UPDATE agent_reeval_queue SET status=?, outcome_reason=?, actor=?, "
                "resolved_at=? WHERE id=? AND status='pending'",
                (status, reason, actor, time.time(), int(reeval_id)),
            )
            if cur.rowcount != 1:
                return None
            r = conn.execute(
                "SELECT * FROM agent_reeval_queue WHERE id=?", (int(reeval_id),)
            ).fetchone()
            return dict(r)

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
