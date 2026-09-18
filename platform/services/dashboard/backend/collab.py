"""Collaboration & workflow store (F22 / ADR 0073).

Comments/annotations attached to platform entities, an entity activity trail, and shareable time-frozen
snapshots behind scoped, expiring, read-only tokens. Everything is **tenant-scoped** (F15), **sanitized**
(F16), and **audited** (D4). The dashboard owns two additive tables in `platform.db`
(`entity_comments`, `share_snapshots`) created on first use — it never alters the CLI's schema.

Pure helpers (mention extraction, sanitization) are unit-tested in isolation.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

import audit_write
from dbconn import connect

_MENTION = re.compile(r"@([A-Za-z0-9._-]+)")
_TAG = re.compile(r"<[^>]*>")
_JS_URI = re.compile(r"javascript:", re.I)


def extract_mentions(text: str) -> list[str]:
    """Unique @-mentions in a comment, order-preserving (F22 R1/GWT-1)."""
    seen: list[str] = []
    for m in _MENTION.findall(text or ""):
        if m not in seen:
            seen.append(m)
    return seen


def sanitize_comment(text: str) -> str:
    """Strip HTML tags and `javascript:` URIs so a comment renders as inert text (F16/R6/GWT-6)."""
    return _JS_URI.sub("", _TAG.sub("", text or "")).strip()


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entity_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            tenant TEXT NOT NULL DEFAULT 'default',
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            mentions TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS share_snapshots (
            token TEXT PRIMARY KEY,
            tenant TEXT NOT NULL DEFAULT 'default',
            view TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL
        );
        """
    )


def _audit(conn: sqlite3.Connection, actor: str, action: str, target: str, details: str) -> None:
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'"
    ).fetchone():
        audit_write.audit(
            actor,
            action,
            target,
            {"detail": details} if details else None,
            source="dashboard-collab",
            conn=conn,
        )


def add_comment(
    db_path: str, entity_type: str, entity_id: str, tenant: str, author: str, body: str
) -> dict:
    """Sanitize + persist a comment on an entity, audited (F22 R1/R5). Returns the stored row."""
    clean = sanitize_comment(body)
    mentions = extract_mentions(clean)
    conn = connect(db_path)
    try:
        _ensure_tables(conn)
        cur = conn.execute(
            "INSERT INTO entity_comments (entity_type, entity_id, tenant, author, body, mentions) "
            "VALUES (?,?,?,?,?,?)",
            (entity_type, entity_id, tenant, author, clean, json.dumps(mentions)),
        )
        _audit(
            conn,
            author,
            "comment_added",
            f"{entity_type}/{entity_id}",
            json.dumps({"mentions": mentions}),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, author, body, mentions, created_at FROM entity_comments WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "id": row[0],
        "author": row[1],
        "body": row[2],
        "mentions": json.loads(row[3]),
        "created_at": row[4],
    }


def list_comments(db_path: str, entity_type: str, entity_id: str, tenant: str) -> list[dict]:
    """Comments on an entity, scoped to the caller's tenant (F15/R1). Newest last."""
    conn = connect(db_path)
    try:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT id, author, body, mentions, created_at FROM entity_comments "
            "WHERE entity_type=? AND entity_id=? AND tenant=? ORDER BY id ASC",
            (entity_type, entity_id, tenant),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "author": r[1], "body": r[2], "mentions": json.loads(r[3]), "created_at": r[4]}
        for r in rows
    ]


def entity_activity(db_path: str, entity_type: str, entity_id: str, tenant: str) -> list[dict]:
    """Merged activity trail: comments + audit events referencing this entity (F22 R5/GWT-5)."""
    target = f"{entity_type}/{entity_id}"
    items: list[dict] = []
    conn = connect(db_path)
    try:
        _ensure_tables(conn)
        for r in conn.execute(
            "SELECT author, created_at FROM entity_comments WHERE entity_type=? AND entity_id=? AND tenant=?",
            (entity_type, entity_id, tenant),
        ).fetchall():
            items.append({"kind": "comment", "actor": r[0], "ts": r[1], "detail": "commented"})
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'"
        ).fetchone():
            for r in conn.execute(
                # By `id`: the merge below re-sorts everything on `ts`, and Python's sort is
                # stable, so events sharing a second keep the order they arrive in. Ordering by
                # `ts` here would hand that tie to the query plan; `id` is the chain order.
                "SELECT actor, action, ts FROM audit_events WHERE target IN (?, ?) ORDER BY id",
                (target, entity_id),
            ).fetchall():
                items.append({"kind": "audit", "actor": r[0], "ts": r[2], "detail": r[1]})
    finally:
        conn.close()
    items.sort(key=lambda x: str(x.get("ts") or ""))
    return items


def create_snapshot(
    db_path: str, tenant: str, view: dict, actor: str, ttl_hours: int = 168
) -> dict:
    """Create a scoped, expiring, read-only shareable snapshot of a view (F22 R2). Returns the token."""
    token = secrets.token_urlsafe(16)
    expires = (datetime.now(UTC) + timedelta(hours=ttl_hours)).isoformat()
    conn = connect(db_path)
    try:
        _ensure_tables(conn)
        conn.execute(
            "INSERT INTO share_snapshots (token, tenant, view, expires_at) VALUES (?,?,?,?)",
            (token, tenant, json.dumps(view), expires),
        )
        _audit(conn, actor, "snapshot_created", token, json.dumps({"keys": sorted(view.keys())}))
        conn.commit()
    finally:
        conn.close()
    return {"token": token, "expires_at": expires}


def get_snapshot(db_path: str, token: str) -> dict | None:
    """Resolve a snapshot if it exists and has not expired — read-only (F22 R2/GWT-3)."""
    conn = connect(db_path)
    try:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT tenant, view, expires_at FROM share_snapshots WHERE token=?", (token,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if row[2] and row[2] < datetime.now(UTC).isoformat():
        return None  # expired
    return {"tenant": row[0], "view": json.loads(row[1]), "expires_at": row[2], "read_only": True}
