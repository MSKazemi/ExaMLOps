"""examlops.data.audit — Audit trail (D4).

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any  # noqa: F401

from examlops.data._rowid import last_insert_id
from examlops.platform_db import (  # noqa: F401
    _audit_canonical,
    _audit_hash,
    _immediate_write,
    begin_immediate,
    get_db,
    init_db,
    install_write_retry,
    write_retry,
)

__all__ = [
    "audit_chain_head",
    "audit_stream_enabled",
    "verify_audit_stream",
    "autonomous_actions",
    "correlation_chain",
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
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
        corr_select = ", " + ", ".join(_CORRELATION_COLS) if set(_CORRELATION_COLS) <= cols else ""
        rows = conn.execute(
            "SELECT id, source, actor, action, target, details, tenant, prev_hash, hash, ts"
            f"{corr_select} FROM audit_events WHERE hash IS NOT NULL ORDER BY id ASC"
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
        # The correlation fields are inside the hash, so verification has to feed them back in.
        # An event written outside any context has them all NULL and canonicalises exactly as it
        # did before ADR 0110, which is what keeps every historical row verifying.
        correlation = {c: r[c] for c in _CORRELATION_COLS} if corr_select else {}
        canonical = _audit_canonical(
            r["source"],
            r["actor"],
            r["action"],
            r["target"],
            r["details"],
            r["tenant"] or "default",
            r["ts"],
            correlation,
        )
        expected = _audit_hash(prev, canonical)
        if r["prev_hash"] != prev or r["hash"] != expected:
            return {
                "ok": False,
                "verified": True,
                # A broken chain was not fully verified either — see the clean return below for
                # why the two flags are separate.
                "fully_verified": False,
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
        # Two questions, and a compliance script is asking the second:
        #   `ok`             — the chain that exists is intact. It stays True over unchained rows
        #                      on purpose; crying wolf about pre-chain history that cannot be
        #                      retro-fitted would make this command useless.
        #   `fully_verified` — every row was checked. False the moment there is a row this could
        #                      not recompute, which is what "is my audit trail sound" means to a
        #                      script reading one field.
        "fully_verified": unchained == 0,
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


# ── best-effort auditing, made visible ────────────────────────────────────────

logger = logging.getLogger(__name__)

#: Audit events this process attempted and lost, keyed by action. Process-local by design: it is
#: the same shape as `examlops.gateway.accounting_failures()`, and for the same reason — the loss
#: cannot be recorded in the store that is unreachable.
_DROPPED_AUDIT_EVENTS: dict[str, int] = {}


def audit_best_effort(
    source: str,
    actor: str | None,
    action: str,
    target: str | None,
    details: dict[str, Any] | None = None,
    *,
    tenant: str = "default",
    conn: Any = None,
) -> bool:
    """Append an audit event, surviving — but never hiding — a failure. Returns whether it landed.

    **Failing open is deliberate and is not what this changes.** A promotion must not be refused
    because the audit datastore blinked, and a secret rotation must not be blocked by it; the
    operation is the thing the operator asked for, and losing the record does not un-do it. What
    must not happen is losing it *silently*, because the platform's two ways of trusting this log
    are both blind to an event that never arrived:

    * the hash chain proves **integrity, not completeness** — a missing event leaves a valid chain,
      since the chain is computed over the rows that exist;
    * ``compliance.check_art12_logging`` asks only whether *at least one* event of each
      EU-AI-Act-required type exists, so a dropped one is invisible while any sibling survives.

    So a loss is logged at ``WARNING`` with the action, the target and the cause, and counted in
    :func:`dropped_audit_events`. **A non-zero count is a record-keeping incident, not a warning:**
    those actions happened and are missing from the log, so any coverage or completeness statement
    about that window is unsound.
    """
    try:
        write_audit_event(source, actor, action, target, details, tenant=tenant, conn=conn)
    except Exception as exc:  # noqa: BLE001 - the caller's operation survives; the loss does not hide
        _DROPPED_AUDIT_EVENTS[action] = _DROPPED_AUDIT_EVENTS.get(action, 0) + 1
        logger.warning(
            "audit event LOST: action=%s target=%s source=%s actor=%s tenant=%s cause=%s: %s "
            "(lost %d event(s) of this action in this process; the log is incomplete for this "
            "window and the hash chain cannot show it)",
            action,
            target,
            source,
            actor,
            tenant,
            type(exc).__name__,
            exc,
            _DROPPED_AUDIT_EVENTS[action],
            exc_info=True,
        )
        return False
    return True


def dropped_audit_events() -> dict[str, int]:
    """Audit events this process attempted and lost, by action. Empty is the healthy state."""
    return dict(_DROPPED_AUDIT_EVENTS)


def reset_dropped_audit_events() -> None:
    """Clear the counters (tests; the map is process-global and would leak between them)."""
    _DROPPED_AUDIT_EVENTS.clear()


# Deliberately not in `__all__`: that list mirrors what `platform_db` re-exports, and the monolith
# must not grow new helpers (`tests/unit/test_data_facades.py` holds the two surfaces identical,
# and the coupling ratchet's message is "new code must go through examlops.sdk, not platform_db").
# `verify_tail` is new here, so it is imported by name rather than advertised as part of the
# monolith's surface.
def verify_tail(last_n: int = 20) -> tuple[bool, str]:
    """Recompute only the newest ``last_n`` links. Returns ``(ok, scope)``.

    :func:`verify_audit_chain` is the real verification and is O(the whole log) by nature: a chain
    is only proven from genesis. That is right for `exa audit verify` and wrong for anything that
    runs on a page render, where reading a table that grows forever makes the page slower every day.

    This checks the part a dashboard can honestly check in constant time — that the newest links
    recompute and point at each other — and returns a ``scope`` string saying exactly that, so a
    caller cannot present it as more than it is. A break older than ``last_n`` is invisible here;
    only the full verification finds it.
    """
    init_db()
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
        corr_select = ", " + ", ".join(_CORRELATION_COLS) if set(_CORRELATION_COLS) <= cols else ""
        rows = conn.execute(
            "SELECT id, source, actor, action, target, details, tenant, prev_hash, hash, ts"
            f"{corr_select} FROM audit_events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT ?",
            (max(1, last_n),),
        ).fetchall()
    rows = list(reversed(rows))
    if not rows:
        return True, "no chained events yet"
    for r in rows:
        correlation = {c: r[c] for c in _CORRELATION_COLS} if corr_select else {}
        canonical = _audit_canonical(
            r["source"],
            r["actor"],
            r["action"],
            r["target"],
            r["details"],
            r["tenant"] or "default",
            r["ts"],
            correlation,
        )
        # Each row carries the digest it was chained onto, so a tail can be checked without
        # walking back to genesis: recompute this row from its own stored `prev_hash`.
        if r["hash"] != _audit_hash(r["prev_hash"], canonical):
            return False, f"newest {len(rows)} events"
    return True, f"newest {len(rows)} events"


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
        # Every chain append — this path and `_lock_chain_head` — takes the one `audit` scope.
        with _immediate_write("audit") as conn_:
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

    The chain's integrity rests on head-read and append being indivisible. When the caller is
    already mid-write it holds the lock and the audit joins that transaction; when the connection
    is idle, :func:`_lock_chain_head` takes an IMMEDIATE lock on it first — the caller still owns
    the commit. (It used to be a documented *requirement* that the caller hold the lock; 24
    dashboard routes did not, and concurrent requests forked the chain.)
    """
    _append_on(
        conn, source, actor, action, target, json.dumps(details) if details else None, tenant
    )


_CORRELATION_COLS = (
    "correlation_id",
    "parent_correlation_id",
    "mode",
    "on_behalf_of",
    "rollback_ref",
)


def _lock_chain_head(conn: Any) -> None:
    """Serialize head-read + append across writers when the engine doesn't already.

    On SQLite the caller's own write holds the RESERVED lock, so the chain cannot fork. On
    Postgres (MVCC) an open transaction serializes nothing: two concurrent ``conn=`` callers
    (e.g. two dashboard mutations) would read the same chain head and both append with the same
    ``prev_hash`` — a permanent fork that ``verify_audit_chain`` reports as tampering forever.
    ``BEGIN IMMEDIATE`` translates on the Pg connection to the platform's transaction-scoped
    advisory lock (released at commit), which is exactly the serialization the standalone
    ``_immediate_write`` path already gets. Re-acquiring it there is safe: advisory xact locks
    stack within a session and all release at transaction end.

    On SQLite the "caller already holds RESERVED" premise is a contract the caller can break: a
    router that committed its change through a helper and then opened a *fresh* connection just to
    audit it hands over an idle connection, and head-read + append run unlocked. Twenty-four
    dashboard routes did exactly that and forked the chain under concurrency. So an idle SQLite
    connection takes the lock here; one already mid-transaction keeps it (and keeps the audit
    atomic with its mutation).
    """
    if type(conn).__name__ == "PgConnection":
        conn.execute(begin_immediate("audit"))
    elif isinstance(conn, sqlite3.Connection) and not conn.in_transaction:
        conn.execute(begin_immediate("audit"))


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
    _lock_chain_head(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
    if not {"prev_hash", "hash"} <= cols:  # pre-migration DB — plain append
        conn.execute(
            "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
            (source, actor, action, target, details_json),
        )
        return

    # ADR 0110: the causal edges come from the ambient context, so the ~200 existing call sites
    # gain correlation by being *inside* a unit of work rather than by each remembering to pass
    # one — a threading change whose failure mode is a silent gap in a causal chain.
    from examlops.evidence import current as _current_correlation

    correlation = _current_correlation().as_dict()
    has_corr_cols = set(_CORRELATION_COLS) <= cols
    if not has_corr_cols:
        # A DB predating the migration: the fields cannot be stored, so they must not be hashed
        # either — hashing what is not written would make every such row unverifiable.
        correlation = {}

    # Chain over the current head. CURRENT_TIMESTAMP is resolved here so the stored ts matches
    # what we hash.
    ts = conn.execute("SELECT CURRENT_TIMESTAMP AS t").fetchone()["t"]
    head = conn.execute(
        "SELECT hash FROM audit_events WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = head["hash"] if head and head["hash"] else "GENESIS"
    canonical = _audit_canonical(
        source, actor, action, target, details_json, tenant, ts, correlation
    )
    h = _audit_hash(prev_hash, canonical)
    if has_corr_cols:
        cur = conn.execute(
            "INSERT INTO audit_events (source, actor, action, target, details, tenant, "
            "prev_hash, hash, ts, correlation_id, parent_correlation_id, mode, on_behalf_of, "
            "rollback_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source,
                actor,
                action,
                target,
                details_json,
                tenant,
                prev_hash,
                h,
                ts,
                *(correlation.get(c) for c in _CORRELATION_COLS),
            ),
        )
    else:
        cur = conn.execute(
            "INSERT INTO audit_events (source, actor, action, target, details, tenant, "
            "prev_hash, hash, ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (source, actor, action, target, details_json, tenant, prev_hash, h, ts),
        )
    if audit_stream_enabled():
        # Same transaction: the event exists exactly when the audit row does (plan P2.4b).
        from examlops.data.events import enqueue_event  # noqa: PLC0415 - avoids an import cycle

        # actor/tenant travel in the payload, not as enqueue_event kwargs: the CloudEvents
        # envelope extensions (plan P2.x) aren't landed yet, and this slice must not depend on
        # them. Restore the kwargs once that slice ships.
        enqueue_event(
            "audit.recorded",
            {
                "id": last_insert_id(cur),
                "ts": str(ts),
                "source": source,
                "actor": actor,
                "action": action,
                "target": target,
                "details": details_json,
                "tenant": tenant,
                "correlation": correlation,
                "prev_hash": prev_hash,
                "hash": h,
            },
            conn=conn,
        )


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def audit_stream_enabled() -> bool:
    """``EXAMLOPS_AUDIT_STREAM``: also publish every audit event as ``audit.recorded`` (off)."""
    import os  # noqa: PLC0415

    return os.getenv("EXAMLOPS_AUDIT_STREAM", "").strip().lower() in _TRUTHY


def verify_audit_stream(events: list[dict[str, Any]]) -> list[str]:
    """Check a run of ``audit.recorded`` payloads, oldest first, the way a SIEM receiving them can.

    Each event's ``hash`` must be the hash of its own fields over ``prev_hash``, and each
    ``prev_hash`` must be the ``hash`` of the event before it: an edited event fails the first,
    a deleted or reordered one the second. Returns the problems found; an empty list means the run
    is intact. The first event's ``prev_hash`` is taken on trust, so verify from a known hash.
    """
    problems: list[str] = []
    previous: str | None = None
    for event in events:
        canonical = _audit_canonical(
            event["source"],
            event["actor"],
            event["action"],
            event["target"],
            event["details"],
            event["tenant"],
            event["ts"],
            event.get("correlation") or {},
        )
        if _audit_hash(event["prev_hash"], canonical) != event["hash"]:
            problems.append(f"event {event.get('id')}: its hash does not match its contents")
        if previous is not None and event["prev_hash"] != previous:
            problems.append(f"event {event.get('id')}: does not follow the event before it")
        previous = event["hash"]
    return problems


def correlation_chain(correlation_id: str, *, max_depth: int = 50) -> list[dict[str, Any]]:
    """Every event in the causal tree rooted at ``correlation_id``, oldest first (ADR 0110).

    Walks *down* through ``parent_correlation_id`` so an orchestrator's id returns the tool calls
    it caused and whatever those caused in turn — the reconstruction the W2 gate asks for. The
    depth bound is a cycle guard: ``parent_correlation_id`` is written by the process that acted
    and nothing at the database level stops a loop, so an unbounded walk would hang rather than
    report.
    """
    init_db()
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
        if "correlation_id" not in cols:
            return []
        seen: set[str] = set()
        frontier = {correlation_id}
        for _ in range(max_depth):
            frontier -= seen
            if not frontier:
                break
            seen |= frontier
            placeholders = ",".join("?" * len(frontier))
            children = conn.execute(
                f"SELECT DISTINCT correlation_id FROM audit_events "
                f"WHERE parent_correlation_id IN ({placeholders}) AND correlation_id IS NOT NULL",
                tuple(frontier),
            ).fetchall()
            frontier = {r["correlation_id"] for r in children}
        if not seen:
            return []
        placeholders = ",".join("?" * len(seen))
        rows = conn.execute(
            f"SELECT * FROM audit_events WHERE correlation_id IN ({placeholders}) ORDER BY id ASC",
            tuple(seen),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("details"):
            try:
                d["details"] = json.loads(d["details"])
            except (ValueError, TypeError):
                pass
        out.append(d)
    return out


def autonomous_actions(*, since_days: int = 30, limit: int = 500) -> list[dict[str, Any]]:
    """Autonomous actions in the recent window, with what the W2 gate asks of each.

    Each row reports who acted, on whose behalf, under which mode, and whether it declared an
    inverse. A row whose ``rollback_ref`` is NULL is exactly what ADR 0110 decision 4 calls a
    policy violation, so it is returned rather than filtered out — the point is to see them.
    """
    init_db()
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
        if "mode" not in cols:
            return []
        rows = conn.execute(
            "SELECT id, ts, source, actor, action, target, tenant, correlation_id, "
            "parent_correlation_id, mode, on_behalf_of, rollback_ref FROM audit_events "
            "WHERE mode = 'autonomous' AND ts >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
            (f"-{int(since_days)} days", limit),
        ).fetchall()
    return [{**dict(r), "undoable": bool(r["rollback_ref"])} for r in rows]


def count_autonomous_actions(*, since_days: int = 30) -> int:
    """How many autonomous actions the window holds — the whole window, not one page of it.

    See :func:`count_autonomous_without_rollback` for why this is not ``len(autonomous_actions())``.
    """
    init_db()
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
        if "mode" not in cols:
            return 0
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_events WHERE mode = 'autonomous' "
            "AND ts >= datetime('now', ?)",
            (f"-{int(since_days)} days",),
        ).fetchone()
    return int(row["c"])


def count_autonomous_without_rollback(*, since_days: int = 30) -> int:
    """How many autonomous actions in the window declared no inverse (ADR 0110 decision 4).

    Separate from :func:`autonomous_actions` on purpose. That one is a **listing** — it is read by
    a person through ``exa audit autonomy``, and a bound on it is right. This is a **count**, and a
    count read off a bounded listing is not the number: it silently becomes "violations among the
    newest N", so on a platform that has run more autonomous actions than N, an older violation
    stops being counted and the compliance section that keys on this number reports *verified*.

    A listing may be bounded. A count may not be.
    """
    init_db()
    with get_db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
        if "mode" not in cols:
            return 0
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_events WHERE mode = 'autonomous' "
            "AND ts >= datetime('now', ?) AND rollback_ref IS NULL",
            (f"-{int(since_days)} days",),
        ).fetchone()
    return int(row["c"])


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
