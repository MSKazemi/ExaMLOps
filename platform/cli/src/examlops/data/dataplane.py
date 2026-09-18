"""examlops.data.dataplane — Dataplane source catalog + pull history (ADR 0130 §6) + stream
catalog (ADR 0130/0131, Plan 2 task A6).

Owns these helpers (bodies live here, not in ``platform_db``) — per-domain split (item 4.5), a
brand-new domain module (not a monolith relocation). DDL for ``dataplane_sources`` /
``dataplane_pulls`` / ``dataplane_streams`` lives once in ``platform_db.init_db``; this module only
reads/writes rows. ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping to the
mutating helpers.

The stream-catalog helpers (``upsert_stream``/``get_stream``/``list_streams``/
``set_stream_state``/``delete_stream``) are deliberately dumb storage: they know nothing about
``StreamBinding``/``StreamLimits`` (``examlops.dataplane.streams.types``) or tenancy — they take
and return plain dicts, exactly like the source-catalog helpers above. The dataclass round-trip and
project/model tenancy rules live one layer up, in ``examlops.dataplane.streams.bindings``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from secrets import token_hex
from typing import Any

from examlops.platform_db import get_db, init_db, install_write_retry

__all__ = [
    "upsert_source",
    "get_source",
    "list_sources",
    "delete_source",
    "new_pull_id",
    "insert_pull",
    "update_pull",
    "restore_pull",
    "get_pull",
    "list_pulls",
    "list_snapshots",
    "list_active_pulls",
    "last_pull",
    "ACTIVE_PULL_STATUSES",
    "COMMITTED_PULL_STATUSES",
    "upsert_stream",
    "get_stream",
    "list_streams",
    "set_stream_state",
    "delete_stream",
    "STREAM_STATES",
    "upsert_dead_letter",
    "get_dead_letter_row",
    "list_dead_letter_rows",
    "claim_dead_letter_replay",
    "finish_dead_letter_replay",
    "purge_dead_letters",
    "prune_dead_letters",
]

#: A pull that left data behind. `unchanged` counts: the source had nothing new, so the previous
#: revision is still the current one, and the pull committed that fact.
COMMITTED_PULL_STATUSES: tuple[str, ...] = ("succeeded", "unchanged")
_COMMITTED = COMMITTED_PULL_STATUSES


def _parse(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for col, key in (
        ("spec_json", "spec"),
        ("limits_json", "limits"),
        ("watermark_json", "watermark"),
        ("options_json", "options"),
    ):
        if col in d:
            raw = d.pop(col)
            d[key] = json.loads(raw) if raw else ({} if key != "watermark" else None)
    return d


def upsert_source(
    project: str,
    name: str,
    *,
    connector: str,
    connection: str | None,
    spec: dict[str, Any],
    schedule: str | None,
    limits: dict[str, Any],
    contract: str | None,
    enabled: bool,
    actor: str | None,
) -> None:
    """Create or update a dataplane source row (upsert on ``(project, name)``)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO dataplane_sources
                   (project, name, connector, connection, spec_json, schedule, limits_json,
                    contract, enabled, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(project, name) DO UPDATE SET
                   connector=excluded.connector, connection=excluded.connection,
                   spec_json=excluded.spec_json, schedule=excluded.schedule,
                   limits_json=excluded.limits_json, contract=excluded.contract,
                   enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP""",
            (
                project,
                name,
                connector,
                connection,
                json.dumps(spec, sort_keys=True),
                schedule,
                json.dumps(limits, sort_keys=True),
                contract,
                1 if enabled else 0,
                actor,
            ),
        )


def get_source(name: str, project: str = "") -> dict[str, Any] | None:
    """Fetch one source row by ``(project, name)``, or ``None`` if it does not exist."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM dataplane_sources WHERE project=? AND name=?", (project, name)
        ).fetchone()
    return _parse(row) if row else None


def list_sources(project: str | None = None) -> list[dict[str, Any]]:
    """List sources, optionally scoped to one project. ``None`` lists across all projects."""
    init_db()
    with get_db() as conn:
        if project is None:
            rows = conn.execute("SELECT * FROM dataplane_sources ORDER BY project, name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dataplane_sources WHERE project=? ORDER BY name", (project,)
            ).fetchall()
    return [_parse(r) for r in rows]


def delete_source(name: str, project: str = "") -> bool:
    """Delete a source row. Returns ``True`` if a row was deleted."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM dataplane_sources WHERE project=? AND name=?", (project, name)
        )
        return cur.rowcount > 0


_pull_id_lock = threading.Lock()
_last_pull_id_ns = 0


def new_pull_id() -> str:
    """Time-ordered id: 16 hex chars of epoch-ns (strictly increasing per process) + 6 random hex chars.

    A plain ``time.time_ns()`` can repeat (or even go backwards, on some clocks) across two calls
    made in quick succession, which would break the ordering callers rely on (``first < second``,
    and a later task parses ``int(pid[:16], 16)`` as nanoseconds for orphan age). Guarding a
    monotonically-advancing counter with a lock makes each id in this process strictly greater
    than the last, regardless of wall-clock resolution or repeated ``time.time_ns()`` reads.
    """
    global _last_pull_id_ns
    with _pull_id_lock:
        now = max(time.time_ns(), _last_pull_id_ns + 1)
        _last_pull_id_ns = now
    return f"{now:016x}{token_hex(3)}"


def insert_pull(
    pull_id: str,
    project: str,
    source: str,
    *,
    trigger_kind: str,
    actor: str | None,
    parent_revision: str | None,
) -> None:
    """Record a new pull as ``running``."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO dataplane_pulls
                   (id, project, source, status, trigger_kind, actor, parent_revision)
               VALUES (?,?,?,?,?,?,?)""",
            (pull_id, project, source, "running", trigger_kind, actor, parent_revision),
        )


def update_pull(
    pull_id: str,
    *,
    status: str,
    finished: bool = False,
    revision: str | None = None,
    row_count: int | None = None,
    byte_count: int | None = None,
    watermark: dict[str, Any] | None = None,
    error: str | None = None,
    only_if_status: tuple[str, ...] | None = None,
) -> bool:
    """Update a pull's status and, optionally, its outcome fields. ``True`` if a row changed.

    ``only_if_status`` makes the update conditional — one ``UPDATE … WHERE id=? AND status IN
    (…)``, atomic in the database — so a writer that decided on a stale read (the interrupted-pull
    reaper, a failure handler) can never overwrite an outcome another writer has recorded since.
    """
    init_db()
    sets = ["status=?"]
    args: list[Any] = [status]
    for col, val in (
        ("revision", revision),
        ("row_count", row_count),
        ("byte_count", byte_count),
        ("error", error),
    ):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if watermark is not None:
        sets.append("watermark_json=?")
        args.append(json.dumps(watermark, sort_keys=True, default=str))
    if finished:
        sets.append("finished_at=CURRENT_TIMESTAMP")
    args.append(pull_id)
    sql = f"UPDATE dataplane_pulls SET {', '.join(sets)} WHERE id=?"  # noqa: S608 - fixed column names
    if only_if_status is not None:
        if not only_if_status:
            return False
        sql += f" AND status IN ({', '.join('?' for _ in only_if_status)})"
        args.extend(only_if_status)
    with get_db() as conn:
        cur = conn.execute(sql, args)
        return bool(cur.rowcount and cur.rowcount > 0)


def restore_pull(
    pull_id: str,
    project: str,
    source: str,
    *,
    revision: str,
    parent_revision: str | None,
    row_count: int,
    byte_count: int,
    watermark: dict[str, Any] | None,
    started_at: str | None,
    finished_at: str | None,
    actor: str | None,
) -> bool:
    """Re-create a committed pull's row from its snapshot manifest (``catalog-rebuild``).

    Idempotent: a row that already exists is left exactly as it is (``ON CONFLICT DO NOTHING``).
    Returns ``True`` when a row was inserted. Timestamps are ``YYYY-MM-DD HH:MM:SS`` UTC, the form
    ``CURRENT_TIMESTAMP`` writes, so the scheduler's freshness math reads them unchanged.
    """
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO dataplane_pulls
                   (id, project, source, status, trigger_kind, actor, started_at, finished_at,
                    revision, parent_revision, row_count, byte_count, watermark_json)
               VALUES (?,?,?,?,?,?,COALESCE(?, CURRENT_TIMESTAMP),?,?,?,?,?,?)
               ON CONFLICT(id) DO NOTHING""",
            (
                pull_id,
                project,
                source,
                "succeeded",
                "rebuild",
                actor,
                started_at,
                finished_at,
                revision,
                parent_revision,
                row_count,
                byte_count,
                json.dumps(watermark or {}, sort_keys=True, default=str),
            ),
        )
        return bool(cur.rowcount and cur.rowcount > 0)


def get_pull(pull_id: str) -> dict[str, Any] | None:
    """Fetch one pull row by id, or ``None`` if it does not exist."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM dataplane_pulls WHERE id=?", (pull_id,)).fetchone()
    return _parse(row) if row else None


def list_pulls(
    *, project: str | None = None, source: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List pulls, newest first, optionally filtered by project and/or source."""
    init_db()
    where: list[str] = []
    args: list[Any] = []
    if project is not None:
        where.append("project=?")
        args.append(project)
    if source is not None:
        where.append("source=?")
        args.append(source)
    sql = "SELECT * FROM dataplane_pulls"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()  # noqa: S608 - fixed column names
    return [_parse(r) for r in rows]


# Every status a pull row holds while its process may still be working on it. `committing` is the
# window between the upload and the final status write: a process that dies there leaves a row no
# one else would ever finish.
ACTIVE_PULL_STATUSES: tuple[str, ...] = ("running", "queued", "committing")
_ACTIVE = ACTIVE_PULL_STATUSES


def list_snapshots(
    *, project: str | None = None, source: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Committed pulls that produced a revision, newest first — the pinnable snapshots.

    Deliberately its own query rather than a filter over :func:`list_pulls`. A snapshot is rarer
    than a pull, so bounding the pulls and discarding the uncommitted ones answers "snapshots among
    the newest N *pulls*": a source whose recent pulls have been failing shows fewer revisions than
    it has, and once N consecutive failures pile up, none at all — an empty list that reads as "this
    source never produced data" at exactly the moment someone is looking because it is broken.
    """
    init_db()
    placeholders = ",".join("?" * len(_COMMITTED))
    where = [f"status IN ({placeholders})", "revision IS NOT NULL"]
    args: list[Any] = list(_COMMITTED)
    if project is not None:
        where.append("project=?")
        args.append(project)
    if source is not None:
        where.append("source=?")
        args.append(source)
    sql = "SELECT * FROM dataplane_pulls WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()  # noqa: S608 - fixed columns, placeheld statuses
    return [_parse(r) for r in rows]


def list_active_pulls() -> list[dict[str, Any]]:
    """Every pull row currently ``running``, ``queued`` or ``committing``, across all sources.

    No limit: there are normally very few of these at once (task 22a — the interrupted-pull
    reaper's own query, called from the service lifespan and each scheduler tick).
    """
    init_db()
    marks = ", ".join("?" for _ in _ACTIVE)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM dataplane_pulls WHERE status IN ({marks}) ORDER BY id",  # noqa: S608
            _ACTIVE,
        ).fetchall()
    return [_parse(r) for r in rows]


def last_pull(project: str, source: str, *, committed_only: bool = True) -> dict[str, Any] | None:
    """The most recent pull for ``(project, source)``, or the most recent committed one."""
    init_db()
    sql = "SELECT * FROM dataplane_pulls WHERE project=? AND source=?"
    args: list[Any] = [project, source]
    if committed_only:
        sql += " AND status IN (?, ?)"
        args.extend(_COMMITTED)
    sql += " ORDER BY id DESC LIMIT 1"
    with get_db() as conn:
        row = conn.execute(sql, args).fetchone()
    return _parse(row) if row else None


# ── stream catalog (ADR 0130/0131, Plan 2 task A6) ──────────────────────────

#: Valid ``dataplane_streams.state`` values. Enforced in Python (no SQL CHECK — this codebase's
#: convention for enum-shaped state columns, e.g. ``dataplane_pulls.status``).
STREAM_STATES: tuple[str, ...] = ("enabled", "paused", "disabled")


def upsert_stream(
    project: str,
    name: str,
    *,
    connector: str,
    model: str,
    alias: str,
    address: str,
    connection: str | None,
    options: dict[str, Any],
    limits: dict[str, Any],
    state: str = "enabled",
    origin: str = "api",
    actor: str | None,
) -> bool:
    """Create or update a stream-binding row (upsert on ``(project, name)``).

    ``state`` is only used for the initial insert — an existing row's ``state`` is left exactly as
    it is on conflict (a re-upsert, e.g. an idempotent pack re-sync, must never silently flip a
    binding a human paused/disabled back to enabled). Use :func:`set_stream_state` to change it.

    **Origin ownership (fix round 1, finding 1):** a conflicting row is only updated when its
    existing ``origin`` matches the ``origin`` being written — ``ON CONFLICT ... DO UPDATE ...
    WHERE dataplane_streams.origin = excluded.origin``. A pack-origin row can only be redefined by
    another pack sync, and an api-origin row only by another API write; SQLite and Postgres both
    leave the row **completely untouched** (no insert, no update) when that WHERE is false, so a
    read-then-write race between two processes attempting opposite origins cannot flip ownership —
    the loser's write silently no-ops rather than clobbering the winner. Returns ``True`` if the
    row was created or updated, ``False`` if refused because the existing row's origin differs
    (callers — :func:`examlops.dataplane.streams.bindings.define_stream`/``sync_pack_streams`` —
    own turning that into a caller-facing error/report entry).
    """
    if state not in STREAM_STATES:
        raise ValueError(f"invalid stream state {state!r}; expected one of {STREAM_STATES}")
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO dataplane_streams
                   (project, name, connector, model, alias, address, connection, options_json,
                    limits_json, state, origin, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(project, name) DO UPDATE SET
                   connector=excluded.connector, model=excluded.model, alias=excluded.alias,
                   address=excluded.address, connection=excluded.connection,
                   options_json=excluded.options_json, limits_json=excluded.limits_json,
                   origin=excluded.origin, updated_at=CURRENT_TIMESTAMP
               WHERE dataplane_streams.origin = excluded.origin""",
            (
                project,
                name,
                connector,
                model,
                alias,
                address,
                connection,
                json.dumps(options, sort_keys=True),
                json.dumps(limits, sort_keys=True),
                state,
                origin,
                actor,
            ),
        )
        return bool(cur.rowcount and cur.rowcount > 0)


def get_stream(name: str, project: str = "") -> dict[str, Any] | None:
    """Fetch one stream-binding row by ``(project, name)``, or ``None`` if it does not exist."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM dataplane_streams WHERE project=? AND name=?", (project, name)
        ).fetchone()
    return _parse(row) if row else None


def list_streams(project: str | None = None) -> list[dict[str, Any]]:
    """List stream bindings, optionally scoped to one project. ``None`` lists across all projects."""
    init_db()
    with get_db() as conn:
        if project is None:
            rows = conn.execute("SELECT * FROM dataplane_streams ORDER BY project, name").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dataplane_streams WHERE project=? ORDER BY name", (project,)
            ).fetchall()
    return [_parse(r) for r in rows]


def set_stream_state(
    project: str,
    name: str,
    state: str,
    *,
    only_if_state: str | None = None,
    reason: str | None = None,
) -> bool:
    """Set a stream binding's admin state (and its ``state_reason``). Returns ``True`` if a row
    matched the ``WHERE`` clause — including a same-value update, so this is "a row was touched",
    not necessarily "a value changed".

    ``only_if_state`` makes the update conditional — one ``UPDATE … WHERE project=? AND name=? AND
    state=?``, atomic in the database — so a caller acting on a stale read can never clobber a
    state change made since (mirrors ``update_pull``'s ``only_if_status``). ``reason`` is written
    verbatim to ``state_reason`` (``None`` clears it) — the sweep in
    ``examlops.dataplane.streams.bindings.sync_pack_streams`` uses ``"removed_from_pack"`` to mark
    its own disables so a later re-add can tell them apart from a human's disable/pause.
    """
    if state not in STREAM_STATES:
        raise ValueError(f"invalid stream state {state!r}; expected one of {STREAM_STATES}")
    if only_if_state is not None and only_if_state not in STREAM_STATES:
        raise ValueError(
            f"invalid only_if_state {only_if_state!r}; expected one of {STREAM_STATES}"
        )
    init_db()
    sql = (
        "UPDATE dataplane_streams SET state=?, state_reason=?, updated_at=CURRENT_TIMESTAMP "
        "WHERE project=? AND name=?"
    )
    args: list[Any] = [state, reason, project, name]
    if only_if_state is not None:
        sql += " AND state=?"
        args.append(only_if_state)
    with get_db() as conn:
        cur = conn.execute(sql, args)
        return bool(cur.rowcount and cur.rowcount > 0)


def delete_stream(name: str, project: str = "") -> bool:
    """Delete a stream-binding row. Returns ``True`` if a row was deleted."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM dataplane_streams WHERE project=? AND name=?", (project, name)
        )
        return cur.rowcount > 0


# ── stream dead letters (ADR 0131 §5, Plan 2 task A7b) ──────────────────────
#
# Dumb storage, like the stream catalog above: payload preparation (opt-in, size cap, redaction),
# the metric, the audit trail and replay live one layer up in
# ``examlops.dataplane.streams.dlq``. Every reader filters on ``project`` — a row of another
# project reads exactly like a row that does not exist.

#: Columns a listing returns: everything but the payload itself (``has_payload`` says whether one
#: is stored) and the internal replay claim.
_DEAD_LETTER_LIST_COLUMNS = (
    "id, project, stream, reason, error, attempts, sha256, size, origin_json, origin_topic, "
    "origin_partition, origin_offset, payload_encoding, payload_truncated, created_at, "
    "updated_at, replayed_at, replayed_by, "
    "CASE WHEN payload IS NULL THEN 0 ELSE 1 END AS has_payload"
)


def _parse_dead_letter(row: Any) -> dict[str, Any]:
    d = dict(row)
    raw = d.pop("origin_json", None)
    try:
        origin = json.loads(raw) if raw else {}
    except ValueError:
        origin = {}
    d["origin"] = origin if isinstance(origin, dict) else {}
    d.pop("replay_claim", None)
    d.pop("replay_claimed_at", None)
    d["payload_truncated"] = bool(d.get("payload_truncated"))
    if "has_payload" in d:
        d["has_payload"] = bool(d["has_payload"])
    else:
        d["has_payload"] = d.get("payload") is not None
    return d


def upsert_dead_letter(
    project: str,
    stream: str,
    *,
    reason: str,
    error: str,
    attempts: int,
    sha256: str | None,
    size: int,
    origin: dict[str, Any],
    origin_topic: str | None,
    origin_partition: int | None,
    origin_offset: int | None,
    payload: str | None,
    payload_encoding: str | None,
    payload_truncated: bool,
) -> tuple[int, bool]:
    """Record one dead letter; ``(id, created)``.

    Idempotent on the origin (ruling R15): when ``origin_topic``/``origin_partition``/
    ``origin_offset`` are all set and a row for ``(project, stream, origin)`` already exists, that
    row is updated instead — ``attempts`` becomes the larger of the two, ``reason``/``error`` are
    replaced only while the row has not been replayed, and the payload columns are left as they
    are — and ``created`` is ``False``. With any origin column ``None`` nothing is deduplicated
    (NULLs never conflict in the UNIQUE constraint). A database failure raises: the caller (a
    stream connector) retries the write rather than losing the dead letter.
    """
    init_db()
    keyed = origin_topic is not None and origin_partition is not None and origin_offset is not None
    insert_args = (
        project,
        stream,
        reason,
        error,
        int(attempts),
        sha256,
        int(size),
        json.dumps(origin, sort_keys=True, default=str),
        origin_topic,
        origin_partition,
        origin_offset,
        payload,
        payload_encoding,
        1 if payload_truncated else 0,
    )
    with get_db() as conn:
        # Two rounds: a purge can delete the conflicting row between the INSERT and the UPDATE,
        # and the second INSERT then succeeds.
        for _ in range(2):
            cur = conn.execute(
                """INSERT INTO dataplane_stream_dead_letters
                       (project, stream, reason, error, attempts, sha256, size, origin_json,
                        origin_topic, origin_partition, origin_offset, payload, payload_encoding,
                        payload_truncated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project, stream, origin_topic, origin_partition, origin_offset)
                   DO NOTHING""",
                insert_args,
            )
            if cur.rowcount and cur.rowcount > 0:
                new_id = cur.lastrowid
                if new_id is None:  # pragma: no cover - both backends report the new id
                    raise RuntimeError("dead letter inserted but its id was not reported")
                return int(new_id), True
            if not keyed:  # pragma: no cover - an unkeyed row cannot conflict
                raise RuntimeError("dead letter was neither inserted nor deduplicated")
            key = (project, stream, origin_topic, origin_partition, origin_offset)
            cur = conn.execute(
                """UPDATE dataplane_stream_dead_letters SET
                       attempts = CASE WHEN attempts < ? THEN ? ELSE attempts END,
                       reason = CASE WHEN replayed_at IS NULL THEN ? ELSE reason END,
                       error = CASE WHEN replayed_at IS NULL THEN ? ELSE error END,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE project=? AND stream=? AND origin_topic=? AND origin_partition=?
                     AND origin_offset=?""",
                (int(attempts), int(attempts), reason, error, *key),
            )
            if cur.rowcount and cur.rowcount > 0:
                row = conn.execute(
                    """SELECT id FROM dataplane_stream_dead_letters
                       WHERE project=? AND stream=? AND origin_topic=? AND origin_partition=?
                         AND origin_offset=?""",
                    key,
                ).fetchone()
                if row is not None:
                    return int(row["id"]), False
    raise RuntimeError("dead letter was neither inserted nor updated")


def get_dead_letter_row(project: str, dead_letter_id: int) -> dict[str, Any] | None:
    """One dead letter of ``project``, payload included, or ``None`` — also for an id that
    belongs to another project."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM dataplane_stream_dead_letters WHERE id=? AND project=?",
            (int(dead_letter_id), project),
        ).fetchone()
    return _parse_dead_letter(row) if row else None


def list_dead_letter_rows(
    project: str,
    *,
    stream: str | None = None,
    limit: int = 50,
    reason: str | None = None,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """Dead letters of ``project`` (optionally one stream), newest first, without payloads.

    ``reason`` and ``before_id`` (the cursor: rows strictly older than that id) are **selection**,
    not presentation, so they belong here rather than in the caller: applying either to an
    already-limited read answers about the window the limit happened to cover, and a caller who
    pages or filters is then shown "nothing more" when the truth is "nothing more *in the first
    N rows*".
    """
    init_db()
    sql = f"SELECT {_DEAD_LETTER_LIST_COLUMNS} FROM dataplane_stream_dead_letters WHERE project=?"  # noqa: S608 - fixed column list
    args: list[Any] = [project]
    if stream is not None:
        sql += " AND stream=?"
        args.append(stream)
    if reason is not None:
        sql += " AND reason=?"
        args.append(reason)
    if before_id is not None:
        sql += " AND id<?"
        args.append(int(before_id))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_parse_dead_letter(r) for r in rows]


def claim_dead_letter_replay(
    project: str, dead_letter_id: int, claim: str, *, force: bool, stale_before: str
) -> bool:
    """Take the one-at-a-time replay claim on a dead letter that has a stored payload.

    Atomic (one conditional ``UPDATE``): succeeds only when no other replay holds the claim, or
    the one that does took it before ``stale_before`` (a crashed replayer), and — unless ``force``
    — the row has not been replayed yet. ``True`` when the claim is now ``claim``'s.
    """
    init_db()
    sql = (
        "UPDATE dataplane_stream_dead_letters SET replay_claim=?, "
        "replay_claimed_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND project=? AND payload IS NOT NULL "
        "AND (replay_claim IS NULL OR replay_claimed_at < ?)"
    )
    if not force:
        sql += " AND replayed_at IS NULL"
    with get_db() as conn:
        cur = conn.execute(sql, (claim, int(dead_letter_id), project, stale_before))
        return bool(cur.rowcount and cur.rowcount > 0)


def finish_dead_letter_replay(
    project: str, dead_letter_id: int, claim: str, *, replayed_by: str | None
) -> bool:
    """Release ``claim``; with ``replayed_by`` also mark the row replayed now, by that actor.
    ``False`` when ``claim`` no longer holds the row (it went stale and was taken over)."""
    init_db()
    sets = "replay_claim=NULL, replay_claimed_at=NULL"
    args: list[Any] = []
    if replayed_by is not None:
        sets += ", replayed_at=CURRENT_TIMESTAMP, replayed_by=?"
        args.append(replayed_by)
    args.extend([int(dead_letter_id), project, claim])
    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE dataplane_stream_dead_letters SET {sets} "  # noqa: S608 - fixed columns
            "WHERE id=? AND project=? AND replay_claim=?",
            args,
        )
        return bool(cur.rowcount and cur.rowcount > 0)


def purge_dead_letters(project: str, stream: str | None, *, before: str) -> int:
    """Delete ``project``'s dead letters (optionally one stream) created before ``before``
    (``YYYY-MM-DD HH:MM:SS`` UTC, the form ``CURRENT_TIMESTAMP`` writes). The count deleted."""
    init_db()
    sql = "DELETE FROM dataplane_stream_dead_letters WHERE project=? AND created_at < ?"
    args: list[Any] = [project, before]
    if stream is not None:
        sql += " AND stream=?"
        args.append(stream)
    with get_db() as conn:
        cur = conn.execute(sql, args)
        return max(0, int(cur.rowcount or 0))


def prune_dead_letters(*, before: str) -> int:
    """Retention: delete every project's dead letters created before ``before``. The count."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM dataplane_stream_dead_letters WHERE created_at < ?", (before,)
        )
        return max(0, int(cur.rowcount or 0))


install_write_retry(__name__)
