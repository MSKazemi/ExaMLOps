"""examlops.data.audit — Audit trail (D4).

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from typing import Any  # noqa: F401

from examlops.data._rowid import last_insert_id
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
        # Rows with no hash are outside the chain and cannot be verified. They must be *counted*,
        # not silently skipped: a verifier that ignores what it cannot check reports `ok: True`
        # over a log it has only partly read, and "we did not look at these" then reads as "these
        # are fine". Found 2026-09-02, when every dashboard-written event turned out to be
        # unchained and `verify` still answered ok with a count that excluded all of them.
        unchained = int(
            conn.execute("SELECT COUNT(*) FROM audit_events WHERE hash IS NULL").fetchone()[0]
        )
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
                "unchained": unchained,
                "broken_at_id": r["id"],
                "reason": "prev_hash mismatch"
                if r["prev_hash"] != prev
                else "hash mismatch (event altered)",
            }
        prev = r["hash"]
    out: dict[str, Any] = {
        "ok": True,
        "verified": True,
        "count": len(rows),
        "unchained": unchained,
        "head_hash": prev,
    }
    if unchained and rows:
        # The oldest chained event dates the migration. An unchained row *after* it is the one
        # worth investigating; everything before is pre-chain history that cannot be retro-fitted
        # without rewriting the log, which would defeat the point of having one.
        out["chain_begins_at"] = rows[0]["ts"]
    if unchained:
        # `ok` stays True — the chain that exists is intact, and saying otherwise would cry wolf.
        # But the claim is narrowed out loud, because the guarantee D4 advertises ("any edit,
        # deletion or reordering breaks the chain") simply does not hold for these rows.
        out["warning"] = (
            f"{unchained} event(s) carry no hash and were not verified — they are outside the "
            "chain, so their order and presence are not tamper-evident (the append-only triggers "
            "still protect them from SQL edits). Two causes, and they need telling apart: events "
            "written before the chain columns were added are expected and age out, whereas a "
            "recent one means a writer is bypassing `write_audit_event`. Compare their timestamps "
            "against the oldest chained event."
        )
    return out


def write_audit_event(
    source: str,
    actor: str | None,
    action: str,
    target: str | None,
    details: dict[str, Any] | None = None,
    *,
    tenant: str = "default",
    conn: Any = None,
) -> None:
    """Append a tamper-evident, hash-chained audit event (D4, R1/R7).

    Pass ``conn`` to append inside a transaction the caller already holds — see
    :func:`append_audit_event` for when that is required.

    Each row stores ``prev_hash`` and ``hash = H(prev_hash ‖ canonical(event))`` so any
    edit/deletion/reordering breaks the chain (verify with :func:`verify_audit_chain`).
    The table is append-only at the DB level (triggers). Chaining degrades gracefully:
    if the hash columns are missing (older DB pre-migration) the event is still written.
    """
    details_json = json.dumps(details) if details else None

    # Every read path in this module bootstraps the schema; this write path did not, so a
    # command whose *first* database touch was its own audit event died on
    # `no such table: audit_events` instead of working. Near-free after the first call per
    # process (`_INITIALIZED_PATHS`). A caller supplying `conn` is already inside a
    # transaction on an initialised database, and re-entering init there would deadlock.
    if conn is None:
        init_db()

    if conn is not None:
        append_audit_event(conn, source, actor, action, target, details=details, tenant=tenant)
        return

    def _append() -> None:
        # IMMEDIATE lock makes head-read + append atomic across writer processes, so the
        # hash chain cannot fork under concurrency; write_retry re-runs the whole txn if the
        # lock is lost after busy_timeout.
        with _immediate_write() as conn_:
            _append_on(conn_, source, actor, action, target, details_json, tenant)

    write_retry(_append)


def append_audit_event(
    conn: Any,
    source: str,
    actor: str | None,
    action: str,
    target: str | None,
    details: dict[str, Any] | None = None,
    *,
    tenant: str = "default",
) -> None:
    """Append a chained event **on an existing connection**, inside the caller's transaction.

    For a caller that is already writing — a dashboard router that has just changed the thing it
    is about to audit. Opening a second connection there deadlocks: SQLite admits one writer, the
    caller holds the write lock, and the audit blocks until `busy_timeout` and fails. Worse, a
    caller that swallows the failure loses the event entirely, which is how a fix for *unchained*
    audit rows turns into *missing* ones.

    Writing on the caller's connection also makes the audit **atomic with the mutation**: they
    commit together or not at all, so an action can no longer succeed while its record is lost.

    **Must be called inside an open write transaction.** The chain's integrity rests on
    head-read and append being indivisible; the standalone path buys that with an IMMEDIATE
    lock, and here it comes from the caller already holding a RESERVED lock through its own
    write. Called on an idle connection, a concurrent writer could interleave between the two
    statements and fork the chain.
    """
    _append_on(
        conn, source, actor, action, target, json.dumps(details) if details else None, tenant
    )


def _append_on(
    conn: Any,
    source: str,
    actor: str | None,
    action: str,
    target: str | None,
    details_json: str | None,
    tenant: str,
) -> None:
    """The chaining itself, on whichever connection/transaction it is handed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
    if not {"prev_hash", "hash"} <= cols:  # pre-migration DB — plain append
        conn.execute(
            "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
            (source, actor, action, target, details_json),
        )
        return
    # Chain over the current head. CURRENT_TIMESTAMP is resolved here so the stored ts matches
    # what we hash.
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
        return last_insert_id(cur)


install_write_retry(__name__)
