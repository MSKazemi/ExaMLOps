"""examlops.data.audit_retention - audit-log retention that keeps the hash chain verifiable (ADR 0028).

``audit_events`` is append-only (triggers refuse UPDATE and DELETE) and hash-chained: each row
stores ``hash = H(prev_hash || canonical(event))``. Deleting an old prefix therefore breaks
verification *unless* the cut is recorded somewhere the verifier trusts. This module records it:

* a **prune record** (``audit_prunes``) holds the last deleted row's id and hash - the *cut* - plus
  an HMAC signature (the D7-managed signing key, the same one checkpoints use) over the cut, the
  count and the archive digest;
* :func:`examlops.data.audit.verify_audit_chain` starts the chain at the newest prune record's cut
  hash instead of ``GENESIS`` after checking that signature, so the retained range still verifies
  link by link and a tampered retained row is still found;
* a forged or deleted prune record breaks verification (signature mismatch / the first retained
  row no longer chains onto ``GENESIS``).

Pruning is refused unless every one of these holds: a retention period is configured
(``EXAMLOPS_AUDIT_RETENTION_DAYS``; unset = keep forever), the chain verifies, a fresh signed
checkpoint over the head is anchored to the WORM store, the cut itself is anchored, and the pruned
rows were written to an archive file whose SHA-256 is recorded. The prune is then recorded in the
chain as an ``audit_pruned`` event. The row deletion drops and recreates the append-only DELETE
trigger *inside one transaction*, so a failure leaves the trigger in place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from examlops.platform_db import _immediate_write, get_db, init_db, write_retry

logger = logging.getLogger(__name__)

_TS = "%Y-%m-%d %H:%M:%S"
PRUNE_KEY_ID = "d3-hmac-prune"


class RetentionRefused(RuntimeError):
    """A prune was refused; the message says which precondition failed."""


def retention_days() -> int | None:
    """``EXAMLOPS_AUDIT_RETENTION_DAYS``: the minimum period rows are kept. Unset = forever."""
    raw = os.getenv("EXAMLOPS_AUDIT_RETENTION_DAYS", "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError as exc:
        raise RetentionRefused(
            f"EXAMLOPS_AUDIT_RETENTION_DAYS must be a whole number of days, got {raw!r}"
        ) from exc
    if days < 1:
        raise RetentionRefused("EXAMLOPS_AUDIT_RETENTION_DAYS must be >= 1")
    return days


def parse_cutoff(value: str) -> str:
    """Normalise ``2026-01-31`` / ISO-8601 to the stored ``YYYY-MM-DD HH:MM:SS`` (UTC)."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetentionRefused(
            f"cannot parse --before {value!r}: use YYYY-MM-DD or ISO-8601"
        ) from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.strftime(_TS)


def _payload(cut_id: int, cut_hash: str, count: int, before_ts: str, archive_sha: str) -> str:
    return f"audit-prune/v1|{cut_id}|{cut_hash}|{count}|{before_ts}|{archive_sha}"


def effective_cutoff(before: str | None, *, now: datetime | None = None) -> str:
    """The cutoff actually used: never newer than ``now - retention``. Refuses if none is set."""
    days = retention_days()
    if days is None:
        raise RetentionRefused(
            "no audit retention is configured (EXAMLOPS_AUDIT_RETENTION_DAYS unset): the audit "
            "trail is kept forever, which is the default. Set a minimum retention period to "
            "allow pruning older rows."
        )
    floor = ((now or datetime.now(UTC)).replace(tzinfo=None) - timedelta(days=days)).strftime(_TS)
    if before is None:
        return floor
    asked = parse_cutoff(before)
    return min(asked, floor)  # a --before newer than the retention floor is clamped, never honoured


def plan_prune(cutoff_ts: str) -> dict[str, Any]:
    """Read-only: what a prune to ``cutoff_ts`` would remove. Always keeps the newest row."""
    init_db()
    with get_db() as conn:
        max_id = conn.execute("SELECT MAX(id) AS m FROM audit_events").fetchone()["m"]
        if max_id is None:
            return {"eligible": 0, "cut_id": None, "cut_hash": None, "cutoff": cutoff_ts}
        first_keep = conn.execute(
            "SELECT MIN(id) AS m FROM audit_events WHERE ts >= ?", (cutoff_ts,)
        ).fetchone()["m"]
        first_keep = max_id if first_keep is None else min(first_keep, max_id)
        cut = conn.execute(
            "SELECT id, hash, ts FROM audit_events WHERE id < ? AND hash IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (first_keep,),
        ).fetchone()
        if cut is None:
            return {"eligible": 0, "cut_id": None, "cut_hash": None, "cutoff": cutoff_ts}
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE id <= ?", (cut["id"],)
        ).fetchone()["n"]
        span = conn.execute(
            "SELECT MIN(ts) AS a, MAX(ts) AS b FROM audit_events WHERE id <= ?", (cut["id"],)
        ).fetchone()
    return {
        "eligible": int(count),
        "cut_id": int(cut["id"]),
        "cut_hash": cut["hash"],
        "cutoff": cutoff_ts,
        "oldest_ts": span["a"],
        "newest_pruned_ts": span["b"],
    }


def list_prunes(limit: int = 20) -> list[dict[str, Any]]:
    """Recorded prunes, newest first. ``[]`` when none was ever made (or the table is absent)."""
    init_db()
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_prunes ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    except Exception:  # noqa: BLE001 - no table means no prune was ever recorded
        return []
    return [dict(r) for r in rows]


def prune_anchor() -> dict[str, Any] | None:
    """The newest prune record, for the verifier. ``None`` if the log was never pruned."""
    rows = list_prunes(1)
    return rows[0] if rows else None


def check_prune_signature(rec: dict[str, Any]) -> str:
    """``"ok"``, ``"bad"`` (forged/altered record) or ``"unverifiable"`` (no signing key here)."""
    import hmac

    from examlops.supplychain import SigningKeyMissing, _hmac_sign

    try:
        expected = _hmac_sign(
            _payload(
                rec["cut_id"],
                rec["cut_hash"],
                rec["pruned_count"],
                rec["before_ts"],
                rec.get("archive_sha256") or "",
            )
        )
    except SigningKeyMissing:
        return "unverifiable"
    return "ok" if hmac.compare_digest(expected, rec.get("signature") or "") else "bad"


def _ensure_table(conn: Any) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_prunes (
               id             INTEGER PRIMARY KEY AUTOINCREMENT,
               ts             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
               cut_id         INTEGER NOT NULL,   -- last deleted audit_events.id
               cut_hash       TEXT NOT NULL,      -- its hash: the retained chain starts here
               pruned_count   INTEGER NOT NULL,
               before_ts      TEXT NOT NULL,      -- the cutoff that was applied
               archive_sha256 TEXT,               -- digest of the archive of the deleted rows
               signature      TEXT NOT NULL,      -- HMAC over the above (D7 signing key)
               key_id         TEXT,
               worm_hash      TEXT,               -- where the cut was anchored
               actor          TEXT
           )"""
    )


def _write_archive(path: str, rows: list[dict[str, Any]]) -> str:
    """Write the pruned rows durably (tmp + fsync + rename) and return the file's SHA-256."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=dest.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, default=str, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def execute_prune(
    before: str | None,
    *,
    archive_path: str,
    actor: str,
    allow_unanchored: bool = False,
) -> dict[str, Any]:
    """Prune audit rows older than the retention cutoff. See the module docstring for the gates.

    Raises :class:`RetentionRefused` (and deletes nothing) if any precondition fails.
    """
    from examlops import audit_worm
    from examlops.data.audit import verify_audit_chain, write_audit_event
    from examlops.supplychain import SigningKeyMissing, _hmac_sign

    cutoff = effective_cutoff(before)
    verified = verify_audit_chain()
    if not verified["ok"]:
        raise RetentionRefused(
            f"the audit chain does not verify (broken at id {verified.get('broken_at_id')}: "
            f"{verified.get('reason')}) - refusing to prune a log that is already in doubt"
        )
    plan = plan_prune(cutoff)
    if not plan["eligible"]:
        return {"pruned": 0, **plan, "status": "nothing-to-prune"}

    try:
        _hmac_sign("probe")
    except SigningKeyMissing as exc:
        raise RetentionRefused(f"cannot sign the prune record: {exc}") from exc

    # 1. Fresh signed checkpoint over the head, anchored off-platform.
    dest = audit_worm._worm_path()
    if dest is None and not allow_unanchored:
        raise RetentionRefused(
            "no WORM anchor is configured (EXAMLOPS_AUDIT_WORM_PATH): pruning without an "
            "external anchor would let a full-DB rewrite hide the cut. Configure one, or pass "
            "--allow-unanchored to accept that."
        )
    if dest is not None:
        worm = audit_worm.verify_worm()
        if not worm["ok"]:
            raise RetentionRefused(f"the WORM anchor does not verify: {worm['reason']}")
    cp = audit_worm.checkpoint_and_anchor()
    if dest is not None and not cp.get("anchored"):
        raise RetentionRefused(
            "the fresh checkpoint could not be durably anchored "
            f"({cp.get('anchor_error') or 'degraded to the local fallback'}) - nothing was deleted"
        )

    # 2. Archive the rows about to go, and anchor the cut itself.
    with get_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM audit_events WHERE id <= ? ORDER BY id ASC", (plan["cut_id"],)
            ).fetchall()
        ]
    archive_sha = _write_archive(archive_path, rows)
    signature = _hmac_sign(
        _payload(plan["cut_id"], plan["cut_hash"], len(rows), cutoff, archive_sha)
    )
    worm_hash = None
    if dest is not None:
        res = audit_worm.anchor_checkpoint_ex(
            {"head_id": plan["cut_id"], "head_hash": plan["cut_hash"], "key_id": PRUNE_KEY_ID},
            ts=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        if res is None or res["degraded"]:
            raise RetentionRefused(
                "the cut could not be anchored to the WORM store - nothing deleted"
            )
        worm_hash = res["worm_hash"]

    # 3. Delete inside one transaction; the append-only trigger is dropped and restored atomically.
    def _delete() -> int:
        with _immediate_write("audit") as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT hash FROM audit_events WHERE id = ?", (plan["cut_id"],)
            ).fetchone()
            if row is None or row["hash"] != plan["cut_hash"]:
                raise RetentionRefused("the cut row changed while pruning - nothing was deleted")
            conn.execute("DROP TRIGGER IF EXISTS audit_events_no_delete")
            cur = conn.execute("DELETE FROM audit_events WHERE id <= ?", (plan["cut_id"],))
            deleted = int(cur.rowcount)
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events "
                "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only (D4)'); END"
            )
            conn.execute(
                "INSERT INTO audit_prunes (cut_id, cut_hash, pruned_count, before_ts, "
                "archive_sha256, signature, key_id, worm_hash, actor) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    plan["cut_id"],
                    plan["cut_hash"],
                    deleted,
                    cutoff,
                    archive_sha,
                    signature,
                    PRUNE_KEY_ID,
                    worm_hash,
                    actor,
                ),
            )
            return deleted

    deleted = write_retry(_delete)
    if deleted != len(rows):
        # Rows landed at or below the cut between the archive and the delete - impossible for an
        # append-only log, so say so loudly rather than sign a lie.
        logger.error("audit prune archived %d rows but deleted %d", len(rows), deleted)

    details = {
        "cut_id": plan["cut_id"],
        "cut_hash": plan["cut_hash"],
        "pruned": deleted,
        "before": cutoff,
        "archive": str(archive_path),
        "archive_sha256": archive_sha,
        "worm_hash": worm_hash,
    }
    write_audit_event("audit", actor, "audit_pruned", str(plan["cut_id"]), details)
    return {"status": "pruned", **details}
