"""examlops.data.audit — Audit trail (D4).

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from typing import Any  # noqa: F401

from examlops.platform_db import (  # noqa: F401
    _audit_canonical,
    _audit_hash,
    _immediate_write,
    get_db,
    init_db,
    install_write_retry,
    write_retry,
)

__all__ = [
    "audit_chain_head",
    "export_audit_events",
    "list_audit_checkpoints",
    "list_training_checkpoints",
    "sign_audit_checkpoint",
    "verify_audit_chain",
    "write_audit_event",
    "write_training_checkpoint",
]


def audit_chain_head() -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, hash FROM audit_events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {"id": row["id"], "hash": row["hash"]} if row else None


def export_audit_events(*, before_ts: str | None = None) -> list[dict[str, Any]]:
    """Read-only archival export of the audit trail (R4). Never deletes — append-only."""
    init_db()
    clause = "WHERE ts < ?" if before_ts else ""
    params = (before_ts,) if before_ts else ()
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_events {clause} ORDER BY id ASC", params
        ).fetchall()
    return [dict(r) for r in rows]


def list_audit_checkpoints(last_n: int = 20) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_checkpoints ORDER BY id DESC LIMIT ?", (last_n,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_training_checkpoints(run_id: str) -> list[dict[str, Any]]:
    """Checkpoints for a run, newest (highest step) first."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM training_checkpoints WHERE run_id=? ORDER BY step DESC, id DESC",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def sign_audit_checkpoint(signature: str, *, key_id: str | None = None) -> dict[str, Any] | None:
    """Persist a detached signature over the current chain head (R5). Returns the checkpoint."""
    head = audit_chain_head()
    if head is None:
        return None
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_checkpoints (head_id, head_hash, signature, key_id) VALUES (?,?,?,?)",
            (head["id"], head["hash"], signature, key_id),
        )
    return {"head_id": head["id"], "head_hash": head["hash"], "key_id": key_id}


def verify_audit_chain() -> dict[str, Any]:
    """Recompute the hash chain and report the first broken link, if any (R2/R6)."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, source, actor, action, target, details, tenant, prev_hash, hash, ts "
            "FROM audit_events WHERE hash IS NOT NULL ORDER BY id ASC"
        ).fetchall()
    prev = "GENESIS"
    for r in rows:
        canonical = _audit_canonical(
            r["source"],
            r["actor"],
            r["action"],
            r["target"],
            r["details"],
            r["tenant"] or "default",
            r["ts"],
        )
        expected = _audit_hash(prev, canonical)
        if r["prev_hash"] != prev or r["hash"] != expected:
            return {
                "ok": False,
                "verified": True,
                "count": len(rows),
                "broken_at_id": r["id"],
                "reason": "prev_hash mismatch"
                if r["prev_hash"] != prev
                else "hash mismatch (event altered)",
            }
        prev = r["hash"]
    return {"ok": True, "verified": True, "count": len(rows), "head_hash": prev}


def write_audit_event(
    source: str,
    actor: str | None,
    action: str,
    target: str | None,
    details: dict[str, Any] | None = None,
    *,
    tenant: str = "default",
) -> None:
    """Append a tamper-evident, hash-chained audit event (D4, R1/R7).

    Each row stores ``prev_hash`` and ``hash = H(prev_hash ‖ canonical(event))`` so any
    edit/deletion/reordering breaks the chain (verify with :func:`verify_audit_chain`).
    The table is append-only at the DB level (triggers). Chaining degrades gracefully:
    if the hash columns are missing (older DB pre-migration) the event is still written.
    """
    details_json = json.dumps(details) if details else None

    def _append() -> None:
        # IMMEDIATE lock makes head-read + append atomic across writer processes, so the
        # hash chain cannot fork under concurrency; write_retry re-runs the whole txn if the
        # lock is lost after busy_timeout.
        with _immediate_write() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
            if not {"prev_hash", "hash"} <= cols:  # pre-migration DB — plain append
                conn.execute(
                    "INSERT INTO audit_events (source, actor, action, target, details) "
                    "VALUES (?,?,?,?,?)",
                    (source, actor, action, target, details_json),
                )
                return
            # Chain over the current head. CURRENT_TIMESTAMP is resolved here so the stored
            # ts matches what we hash.
            ts = conn.execute("SELECT CURRENT_TIMESTAMP AS t").fetchone()["t"]
            head = conn.execute(
                "SELECT hash FROM audit_events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = head["hash"] if head and head["hash"] else "GENESIS"
            canonical = _audit_canonical(source, actor, action, target, details_json, tenant, ts)
            h = _audit_hash(prev_hash, canonical)
            conn.execute(
                "INSERT INTO audit_events (source, actor, action, target, details, tenant, "
                "prev_hash, hash, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (source, actor, action, target, details_json, tenant, prev_hash, h, ts),
            )

    write_retry(_append)


def write_training_checkpoint(
    run_id: str,
    step: int,
    epoch: int,
    state_json: str,
    integrity_hash: str,
    *,
    shard_count: int = 1,
    uri: str | None = None,
    mlflow_run_id: str | None = None,
) -> int:
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO training_checkpoints
                   (run_id, step, epoch, shard_count, uri, state_json, integrity_hash,
                    mlflow_run_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, step, epoch, shard_count, uri, state_json, integrity_hash, mlflow_run_id),
        )
        return int(cur.lastrowid)


install_write_retry(__name__)
