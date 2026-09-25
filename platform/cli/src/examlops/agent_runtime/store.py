"""The agent state store (ADR 0144 decision 3): durable agent state outside the compute.

A **serving-plane** datastore, deliberately *not* ``platform.db``: the runtime must keep answering
while the control plane, MLflow, Prefect and ``platform.db`` are down (decision 5), and an agent
cannot answer without its state. It holds threads, runs, step checkpoints, parked interrupts,
the tool-result journal keyed by the derived idempotency key, the one-turn-per-thread lease, a
runtime event log and the last-known-good agent snapshot.

SQLite (WAL, via :mod:`examlops.resilience.db`) is the single-node store. The Postgres store the
ADR names for production is not built yet - :func:`default_path` refuses a ``platform.db`` path,
so the two can never be the same file by accident.

Every multi-step decision (claiming a lease, creating a run behind a busy thread) happens inside
one ``BEGIN IMMEDIATE`` transaction, so two workers cannot both win.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from examlops.agent_runtime.types import RuntimeRefusal
from examlops.resilience.db import connect, write_retry

__all__ = ["HOLDING_STATUSES", "AgentStateStore", "default_path"]

#: Thread statuses that hold a tenant's concurrent-session slot (ADR 0144 d4/d7).
HOLDING_STATUSES = ("active", "idle")

T = TypeVar("T")

_DDL = """
CREATE TABLE IF NOT EXISTS rt_threads (
    thread_id       TEXT PRIMARY KEY,
    tenant          TEXT NOT NULL,
    agent           TEXT NOT NULL,
    version_id      TEXT NOT NULL,
    principal       TEXT,
    status          TEXT NOT NULL,
    canary          INTEGER NOT NULL DEFAULT 0,
    metadata_json   TEXT,
    created_at      REAL NOT NULL,
    last_active_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rt_threads_tenant ON rt_threads(tenant, status, thread_id);
CREATE INDEX IF NOT EXISTS idx_rt_threads_version ON rt_threads(agent, version_id);
CREATE TABLE IF NOT EXISTS rt_runs (
    run_id               TEXT PRIMARY KEY,
    thread_id            TEXT NOT NULL,
    tenant               TEXT NOT NULL,
    version_id           TEXT NOT NULL,
    seq                  INTEGER NOT NULL,
    status               TEXT NOT NULL,
    input_json           TEXT,
    output_json          TEXT,
    error                TEXT,
    stop_requested       TEXT,
    base_checkpoint_id   INTEGER,
    resolved_models_json TEXT,
    steps                INTEGER NOT NULL DEFAULT 0,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rt_runs_thread ON rt_runs(thread_id, seq);
CREATE INDEX IF NOT EXISTS idx_rt_runs_status ON rt_runs(status, thread_id);
CREATE TABLE IF NOT EXISTS rt_checkpoints (
    thread_id             TEXT NOT NULL,
    checkpoint_id         INTEGER NOT NULL,
    run_id                TEXT NOT NULL,
    next_node             TEXT,
    state_json            TEXT NOT NULL,
    agent_version_id      TEXT NOT NULL,
    state_schema_version  INTEGER,
    created_at            REAL NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS rt_interrupts (
    key          TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    thread_id    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT,
    status       TEXT NOT NULL DEFAULT 'open',
    value_json   TEXT,
    resolved_by  TEXT,
    created_at   REAL NOT NULL,
    resolved_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_rt_interrupts_run ON rt_interrupts(run_id, status);
CREATE TABLE IF NOT EXISTS rt_tool_results (
    idem_key     TEXT PRIMARY KEY,
    thread_id    TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    tool         TEXT NOT NULL,
    args_digest  TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rt_tool_results_thread ON rt_tool_results(thread_id, created_at);
CREATE TABLE IF NOT EXISTS rt_leases (
    thread_id   TEXT PRIMARY KEY,
    holder      TEXT NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rt_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    tenant      TEXT,
    thread_id   TEXT,
    run_id      TEXT,
    kind        TEXT NOT NULL,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_rt_events_thread ON rt_events(thread_id, id);
CREATE TABLE IF NOT EXISTS rt_snapshot (
    k            TEXT PRIMARY KEY,
    generation   INTEGER NOT NULL,
    digest       TEXT NOT NULL,
    doc_json     TEXT NOT NULL,
    received_at  REAL NOT NULL
);
"""


def default_path() -> str:
    """``EXAMLOPS_AGENT_STATE_DB``, else ``<data root>/agent/agent_state.db``, else CWD.

    Raises ``ValueError`` if the resolved path is the platform datastore: the agent state store
    is serving plane and must survive ``platform.db`` being unavailable (ADR 0144 d5).
    """
    explicit = os.getenv("EXAMLOPS_AGENT_STATE_DB", "").strip()
    if explicit:
        path = explicit
    else:
        from examlops.lifecycle.datadir import agent_db_default

        path = agent_db_default("agent_state.db")
    _refuse_platform_db(path)
    return path


def _refuse_platform_db(path: str) -> None:
    """``ValueError`` when ``path`` is the platform datastore - wherever it was resolved from.

    Compared with the path ``platform_db`` itself would open (``PLATFORM_DB``, else the data
    root, else its default), not only with the environment variable: with ``PLATFORM_DB`` unset
    the platform datastore still exists, and the explicit ``AgentStateStore(path)`` route must be
    held to the same rule as :func:`default_path`.
    """
    from examlops.platform_db import _db_path

    mine = Path(path).expanduser().resolve()
    if mine == Path(_db_path()).expanduser().resolve():
        raise ValueError("the agent state store must not be platform.db (ADR 0144 decision 3)")


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, sort_keys=True, default=str)


def _uj(v: Any) -> Any:
    return None if v is None else json.loads(v)


class AgentStateStore:
    """All durable runtime state. Thread-safe: one short-lived connection per operation."""

    def __init__(self, path: str | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path or default_path()
        _refuse_platform_db(self.path)
        self.clock = clock
        parent = Path(self.path).expanduser().parent
        parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_DDL)

    # -- plumbing --------------------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.path)
        conn.isolation_level = None  # explicit transactions only
        try:
            yield conn
        finally:
            conn.close()

    def _tx(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def _do() -> T:
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    out = fn(conn)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return out

        return write_retry(_do)

    def _read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def _do() -> T:
            with self._conn() as conn:
                return fn(conn)

        return write_retry(_do)

    # -- threads ---------------------------------------------------------------------------------

    @staticmethod
    def _thread(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        d["metadata"] = _uj(d.pop("metadata_json")) or {}
        d["canary"] = bool(d["canary"])
        return d

    def create_thread(
        self,
        *,
        tenant: str,
        agent: str,
        version_id: str,
        principal: str | None,
        canary: bool = False,
        metadata: dict[str, Any] | None = None,
        thread_id: str | None = None,
        max_active: int | None = None,
    ) -> dict[str, Any]:
        """Create an ``active`` thread. With ``max_active``, the tenant's session quota is
        checked in the same ``BEGIN IMMEDIATE`` transaction as the insert, so two concurrent
        opens cannot both take the last slot (``RuntimeRefusal`` 429)."""
        tid = thread_id or f"th-{uuid.uuid4().hex}"
        now = self.clock()

        def _do(conn: sqlite3.Connection) -> dict[str, Any]:
            if conn.execute("SELECT 1 FROM rt_threads WHERE thread_id=?", (tid,)).fetchone():
                raise ValueError(f"thread {tid} already exists")
            if max_active is not None:
                self._check_quota(conn, tenant, max_active)
            conn.execute(
                "INSERT INTO rt_threads (thread_id, tenant, agent, version_id, principal, status, "
                "canary, metadata_json, created_at, last_active_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    tenant,
                    agent,
                    version_id,
                    principal,
                    "active",
                    1 if canary else 0,
                    _j(metadata or {}),
                    now,
                    now,
                ),
            )
            r = conn.execute("SELECT * FROM rt_threads WHERE thread_id=?", (tid,)).fetchone()
            return self._thread(r)  # type: ignore[return-value]

        return self._tx(_do)

    def get_thread(self, thread_id: str, *, tenant: str | None = None) -> dict[str, Any] | None:
        def _do(conn: sqlite3.Connection) -> dict[str, Any] | None:
            sql, args = "SELECT * FROM rt_threads WHERE thread_id=?", [thread_id]
            if tenant is not None:
                sql += " AND tenant=?"
                args.append(tenant)
            return self._thread(conn.execute(sql, args).fetchone())

        return self._read(_do)

    @staticmethod
    def _check_quota(conn: sqlite3.Connection, tenant: str, max_active: int) -> None:
        marks = ",".join("?" for _ in HOLDING_STATUSES)
        held = conn.execute(
            f"SELECT COUNT(*) FROM rt_threads WHERE tenant=? AND status IN ({marks})",  # noqa: S608
            (tenant, *HOLDING_STATUSES),
        ).fetchone()[0]
        if int(held) >= int(max_active):
            raise RuntimeRefusal(
                "session_quota_exceeded",
                f"tenant {tenant!r} already holds {max_active} active sessions",
                status=429,
            )

    def transition_thread(
        self,
        thread_id: str,
        *,
        from_statuses: tuple[str, ...],
        to: str,
        max_active: int | None = None,
        last_active_at: float | None = None,
    ) -> bool:
        """Compare-and-set a thread's status; ``False`` when it is no longer in ``from_statuses``.

        The lifecycle is written by several actors at once (the sweep, a request, a close, a
        retirement). A read-then-write would let one overwrite another's newer state - a sweep
        turning a just-closed session back into ``suspended``, which the next input would then
        reopen. Moving a thread that holds no quota slot into one that does is admitted under
        ``max_active`` in the same transaction.
        """

        def _do(conn: sqlite3.Connection) -> bool:
            r = conn.execute(
                "SELECT tenant, status FROM rt_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if r is None or r["status"] not in from_statuses:
                return False
            if (
                max_active is not None
                and to in HOLDING_STATUSES
                and r["status"] not in HOLDING_STATUSES
            ):
                self._check_quota(conn, r["tenant"], max_active)
            if last_active_at is None:
                conn.execute("UPDATE rt_threads SET status=? WHERE thread_id=?", (to, thread_id))
            else:
                conn.execute(
                    "UPDATE rt_threads SET status=?, last_active_at=? WHERE thread_id=?",
                    (to, last_active_at, thread_id),
                )
            return True

        return self._tx(_do)

    def update_thread(self, thread_id: str, **fields: Any) -> None:
        allowed = {"status", "version_id", "last_active_at", "principal"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update thread fields {sorted(bad)}")
        cols = ", ".join(f"{k}=?" for k in fields)

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"UPDATE rt_threads SET {cols} WHERE thread_id=?",  # noqa: S608 - allow-listed
                (*fields.values(), thread_id),
            )

        self._tx(_do)

    def list_threads(
        self,
        *,
        tenant: str | None,
        status: str | None = None,
        agent: str | None = None,
        version_id: str | None = None,
        after: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Tenant and every other filter are applied in SQL, before the LIMIT."""

        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            sql = "SELECT * FROM rt_threads WHERE thread_id > ?"
            args: list[Any] = [after]
            for col, val in (
                ("tenant", tenant),
                ("status", status),
                ("agent", agent),
                ("version_id", version_id),
            ):
                if val is not None:
                    sql += f" AND {col}=?"
                    args.append(val)
            sql += " ORDER BY thread_id LIMIT ?"
            args.append(max(1, min(int(limit), 1000)))
            return [self._thread(r) for r in conn.execute(sql, args).fetchall()]  # type: ignore[misc]

        return self._read(_do)

    def count_threads(self, *, tenant: str, statuses: tuple[str, ...]) -> int:
        marks = ",".join("?" for _ in statuses)

        def _do(conn: sqlite3.Connection) -> int:
            r = conn.execute(
                f"SELECT COUNT(*) FROM rt_threads WHERE tenant=? AND status IN ({marks})",  # noqa: S608
                (tenant, *statuses),
            ).fetchone()
            return int(r[0])

        return self._read(_do)

    def delete_thread(self, thread_id: str) -> None:
        """Hard delete (used for scratch replay threads only)."""

        def _do(conn: sqlite3.Connection) -> None:
            for table in (
                "rt_checkpoints",
                "rt_interrupts",
                "rt_tool_results",
                "rt_leases",
                "rt_runs",
                "rt_events",
                "rt_threads",
            ):
                conn.execute(f"DELETE FROM {table} WHERE thread_id=?", (thread_id,))  # noqa: S608

        self._tx(_do)

    # -- runs ------------------------------------------------------------------------------------

    @staticmethod
    def _run(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        d["input"] = _uj(d.pop("input_json"))
        d["output"] = _uj(d.pop("output_json"))
        d["resolved_models"] = _uj(d.pop("resolved_models_json")) or {}
        return d

    def create_run(
        self,
        thread_id: str,
        *,
        tenant: str,
        version_id: str,
        input: Any,
        decide: Callable[[list[dict[str, Any]]], dict[str, str]] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Create a pending run behind the thread's active runs, atomically.

        ``decide(active_runs)`` returns ``{run_id: action}`` with action ``stop:<mode>`` (ask the
        running worker to stop at the next boundary) or ``status:<state>`` (settle a run that is
        not executing). It may raise to refuse. Returns ``(run, active_runs_before)``.
        """
        rid = f"run-{uuid.uuid4().hex}"
        now = self.clock()

        def _do(conn: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            active = [
                self._run(r)
                for r in conn.execute(
                    "SELECT * FROM rt_runs WHERE thread_id=? AND status IN "
                    "('pending','running','interrupted') ORDER BY seq",
                    (thread_id,),
                ).fetchall()
            ]
            actions = decide(active) if decide is not None else {}  # type: ignore[arg-type]
            for run_id, action in actions.items():
                kind, _, value = action.partition(":")
                if kind == "stop":
                    conn.execute(
                        "UPDATE rt_runs SET stop_requested=?, updated_at=? WHERE run_id=?",
                        (value, now, run_id),
                    )
                elif kind == "status":
                    conn.execute(
                        "UPDATE rt_runs SET status=?, updated_at=? WHERE run_id=?",
                        (value, now, run_id),
                    )
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM rt_runs WHERE thread_id=?", (thread_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO rt_runs (run_id, thread_id, tenant, version_id, seq, status, "
                "input_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, thread_id, tenant, version_id, seq, "pending", _j(input), now, now),
            )
            r = conn.execute("SELECT * FROM rt_runs WHERE run_id=?", (rid,)).fetchone()
            return self._run(r), active  # type: ignore[return-value]

        return self._tx(_do)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._read(
            lambda c: self._run(
                c.execute("SELECT * FROM rt_runs WHERE run_id=?", (run_id,)).fetchone()
            )
        )

    def update_run(self, run_id: str, **fields: Any) -> None:
        cols = {
            "status": "status",
            "output": "output_json",
            "error": "error",
            "stop_requested": "stop_requested",
            "base_checkpoint_id": "base_checkpoint_id",
            "resolved_models": "resolved_models_json",
            "steps": "steps",
            "version_id": "version_id",
        }
        bad = set(fields) - set(cols)
        if bad:
            raise ValueError(f"cannot update run fields {sorted(bad)}")
        sets, args = [], []
        for k, v in fields.items():
            sets.append(f"{cols[k]}=?")
            args.append(_j(v) if cols[k].endswith("_json") else v)
        sets.append("updated_at=?")
        args.append(self.clock())

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"UPDATE rt_runs SET {', '.join(sets)} WHERE run_id=?",  # noqa: S608 - allow-listed
                (*args, run_id),
            )

        self._tx(_do)

    def runs_for_thread(
        self, thread_id: str, *, statuses: tuple[str, ...] | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            sql = "SELECT * FROM rt_runs WHERE thread_id=?"
            args: list[Any] = [thread_id]
            if statuses:
                sql += f" AND status IN ({','.join('?' for _ in statuses)})"
                args.extend(statuses)
            sql += " ORDER BY seq LIMIT ?"
            args.append(int(limit))
            return [self._run(r) for r in conn.execute(sql, args).fetchall()]  # type: ignore[misc]

        return self._read(_do)

    def runs_with_status(self, status: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM rt_runs WHERE status=? ORDER BY updated_at LIMIT ?",
                (status, int(limit)),
            ).fetchall()
            return [d for r in rows if (d := self._run(r)) is not None]

        return self._read(_do)

    def orphaned_runs(self, *, now: float, limit: int = 20) -> list[dict[str, Any]]:
        """``running`` runs whose thread has no live lease (their worker died), oldest first.

        The lease test is in SQL, before the ``LIMIT``: a window of runs that are merely busy
        would otherwise hide an orphan behind them.
        """

        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT r.* FROM rt_runs r LEFT JOIN rt_leases l ON l.thread_id = r.thread_id "
                "WHERE r.status='running' AND (l.expires_at IS NULL OR l.expires_at <= ?) "
                "ORDER BY r.updated_at, r.run_id LIMIT ?",
                (now, max(1, int(limit))),
            ).fetchall()
            return [d for r in rows if (d := self._run(r)) is not None]

        return self._read(_do)

    def consume_interrupt(self, key: str) -> None:
        """Mark a resolved interrupt as handed to the run that resumes on it."""
        self._tx(
            lambda c: c.execute(
                "UPDATE rt_interrupts SET status='consumed' WHERE key=? AND status='resolved'",
                (key,),
            )
        )

    # -- checkpoints -----------------------------------------------------------------------------

    def put_checkpoint(
        self,
        thread_id: str,
        *,
        run_id: str,
        state: dict[str, Any],
        next_node: str | None,
        agent_version_id: str,
        state_schema_version: int | None,
    ) -> int:
        def _do(conn: sqlite3.Connection) -> int:
            cid = conn.execute(
                "SELECT COALESCE(MAX(checkpoint_id), 0) + 1 FROM rt_checkpoints WHERE thread_id=?",
                (thread_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO rt_checkpoints (thread_id, checkpoint_id, run_id, next_node, "
                "state_json, agent_version_id, state_schema_version, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    thread_id,
                    cid,
                    run_id,
                    next_node,
                    _j(state),
                    agent_version_id,
                    state_schema_version,
                    self.clock(),
                ),
            )
            return int(cid)

        return self._tx(_do)

    @staticmethod
    def _cp(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        d["state"] = _uj(d.pop("state_json"))
        return d

    def latest_checkpoint(self, thread_id: str) -> dict[str, Any] | None:
        return self._read(
            lambda c: self._cp(
                c.execute(
                    "SELECT * FROM rt_checkpoints WHERE thread_id=? "
                    "ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id,),
                ).fetchone()
            )
        )

    def get_checkpoint(self, thread_id: str, checkpoint_id: int) -> dict[str, Any] | None:
        return self._read(
            lambda c: self._cp(
                c.execute(
                    "SELECT * FROM rt_checkpoints WHERE thread_id=? AND checkpoint_id=?",
                    (thread_id, checkpoint_id),
                ).fetchone()
            )
        )

    def discard_after(self, thread_id: str, checkpoint_id: int | None, *, run_id: str) -> int:
        """Drop ``run_id``'s checkpoints newer than ``checkpoint_id`` (multitask ``rollback``)."""

        def _do(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "DELETE FROM rt_checkpoints WHERE thread_id=? AND run_id=? AND checkpoint_id > ?",
                (thread_id, run_id, checkpoint_id or 0),
            )
            return int(cur.rowcount)

        return self._tx(_do)

    # -- interrupts ------------------------------------------------------------------------------

    def put_interrupt(
        self, key: str, *, run_id: str, thread_id: str, kind: str, payload: dict[str, Any]
    ) -> None:
        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO rt_interrupts (key, run_id, thread_id, kind, payload_json, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (key, run_id, thread_id, kind, _j(payload), self.clock()),
            )

        self._tx(_do)

    @staticmethod
    def _intr(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        d["payload"] = _uj(d.pop("payload_json"))
        d["value"] = _uj(d.pop("value_json"))
        return d

    def get_interrupt(self, key: str) -> dict[str, Any] | None:
        return self._read(
            lambda c: self._intr(
                c.execute("SELECT * FROM rt_interrupts WHERE key=?", (key,)).fetchone()
            )
        )

    def open_interrupt(self, run_id: str) -> dict[str, Any] | None:
        return self._read(
            lambda c: self._intr(
                c.execute(
                    "SELECT * FROM rt_interrupts WHERE run_id=? AND status='open' "
                    "ORDER BY created_at LIMIT 1",
                    (run_id,),
                ).fetchone()
            )
        )

    def resolve_interrupt(self, key: str, value: Any, *, by: str | None) -> bool:
        """Resolve an open interrupt once; a second resolution returns False (first one stands)."""

        def _do(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE rt_interrupts SET status='resolved', value_json=?, resolved_by=?, "
                "resolved_at=? WHERE key=? AND status='open'",
                (_j(value), by, self.clock(), key),
            )
            return cur.rowcount == 1

        return self._tx(_do)

    def interrupts_for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        return self._read(
            lambda c: [
                self._intr(r)  # type: ignore[misc]
                for r in c.execute(
                    "SELECT * FROM rt_interrupts WHERE thread_id=? ORDER BY created_at",
                    (thread_id,),
                ).fetchall()
            ]
        )

    # -- tool-result journal (ADR 0144 d3) -------------------------------------------------------

    def get_tool_result(self, key: str) -> dict[str, Any] | None:
        def _do(conn: sqlite3.Connection) -> dict[str, Any] | None:
            r = conn.execute("SELECT * FROM rt_tool_results WHERE idem_key=?", (key,)).fetchone()
            if r is None:
                return None
            d = dict(r)
            d["result"] = _uj(d.pop("result_json"))
            return d

        return self._read(_do)

    def put_tool_result(
        self,
        key: str,
        *,
        thread_id: str,
        run_id: str,
        seq: int,
        tool: str,
        args_digest: str,
        result: dict[str, Any],
    ) -> bool:
        """Store once per key; returns False when the key already held a result."""

        def _do(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "INSERT OR IGNORE INTO rt_tool_results (idem_key, thread_id, run_id, seq, tool, "
                "args_digest, ok, result_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    thread_id,
                    run_id,
                    seq,
                    tool,
                    args_digest,
                    1 if result.get("ok") else 0,
                    _j(result),
                    self.clock(),
                ),
            )
            return cur.rowcount == 1

        return self._tx(_do)

    def tool_calls_for_run(self, run_id: str) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            out = []
            for r in conn.execute(
                "SELECT * FROM rt_tool_results WHERE run_id=? ORDER BY created_at, seq",
                (run_id,),
            ).fetchall():
                d = dict(r)
                d["result"] = _uj(d.pop("result_json"))
                out.append(d)
            return out

        return self._read(_do)

    # -- the one-turn-per-thread lease (ADR 0144 d5) ---------------------------------------------

    def acquire_lease(self, thread_id: str, holder: str, ttl: float) -> bool:
        now = self.clock()

        def _do(conn: sqlite3.Connection) -> bool:
            r = conn.execute(
                "SELECT holder, expires_at FROM rt_leases WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if r is not None and r["holder"] != holder and r["expires_at"] > now:
                return False
            conn.execute(
                "INSERT INTO rt_leases (thread_id, holder, expires_at) VALUES (?,?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET holder=excluded.holder, "
                "expires_at=excluded.expires_at",
                (thread_id, holder, now + ttl),
            )
            return True

        return self._tx(_do)

    def release_lease(self, thread_id: str, holder: str) -> None:
        self._tx(
            lambda c: c.execute(
                "DELETE FROM rt_leases WHERE thread_id=? AND holder=?", (thread_id, holder)
            )
        )

    def lease(self, thread_id: str) -> dict[str, Any] | None:
        return self._read(
            lambda c: (
                dict(r)
                if (
                    r := c.execute(
                        "SELECT * FROM rt_leases WHERE thread_id=?", (thread_id,)
                    ).fetchone()
                )
                else None
            )
        )

    # -- events ----------------------------------------------------------------------------------

    def log_event(
        self,
        kind: str,
        *,
        tenant: str | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._tx(
            lambda c: c.execute(
                "INSERT INTO rt_events (ts, tenant, thread_id, run_id, kind, detail_json) "
                "VALUES (?,?,?,?,?,?)",
                (self.clock(), tenant, thread_id, run_id, kind, _j(detail)),
            )
        )

    def events(
        self, *, thread_id: str | None = None, kind: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            sql = "SELECT * FROM rt_events WHERE 1=1"
            args: list[Any] = []
            if thread_id is not None:
                sql += " AND thread_id=?"
                args.append(thread_id)
            if kind is not None:
                sql += " AND kind=?"
                args.append(kind)
            sql += " ORDER BY id LIMIT ?"
            args.append(int(limit))
            out = []
            for r in conn.execute(sql, args).fetchall():
                d = dict(r)
                d["detail"] = _uj(d.pop("detail_json"))
                out.append(d)
            return out

        return self._read(_do)

    def has_event(self, kind: str, key: str) -> bool:
        """Whether an event of ``kind`` whose ``detail.key`` is ``key`` was logged.

        Matched in SQL: a marker looked up in a fixed-size page of events would fall out of that
        page once enough of its kind accumulate, and the thing it guards would happen again.
        """
        return self._read(
            lambda c: (
                c.execute(
                    "SELECT 1 FROM rt_events WHERE kind=? AND json_extract(detail_json, '$.key')=? "
                    "LIMIT 1",
                    (kind, key),
                ).fetchone()
                is not None
            )
        )

    # -- last-known-good snapshot (ADR 0144 d5) --------------------------------------------------

    def save_snapshot(self, doc: dict[str, Any]) -> None:
        self._tx(
            lambda c: c.execute(
                "INSERT INTO rt_snapshot (k, generation, digest, doc_json, received_at) "
                "VALUES ('agent',?,?,?,?) ON CONFLICT(k) DO UPDATE SET "
                "generation=excluded.generation, digest=excluded.digest, "
                "doc_json=excluded.doc_json, received_at=excluded.received_at",
                (int(doc.get("generation", 0)), str(doc.get("digest", "")), _j(doc), self.clock()),
            )
        )

    def load_snapshot(self) -> dict[str, Any] | None:
        def _do(conn: sqlite3.Connection) -> dict[str, Any] | None:
            r = conn.execute("SELECT doc_json FROM rt_snapshot WHERE k='agent'").fetchone()
            return _uj(r["doc_json"]) if r else None

        return self._read(_do)

    # -- retention -------------------------------------------------------------------------------

    def prune(self, *, older_than_s: float) -> dict[str, int]:
        """Drop recorded tool results and events older than the retention window, and every row
        of threads closed before it. Recorded tool results are tenant data (ADR 0146 d4)."""
        cutoff = self.clock() - older_than_s

        def _do(conn: sqlite3.Connection) -> dict[str, int]:
            # A result journaled for a run that can still execute (pending, running, parked on
            # a human) is the only thing standing between a re-executed node and a repeated
            # side effect: it is kept however old it is. `retire_applied` markers are what make
            # a rollback's in-flight policy apply once; dropping them would re-apply it.
            out = {
                "tool_results": conn.execute(
                    "DELETE FROM rt_tool_results WHERE created_at < ? AND run_id NOT IN "
                    "(SELECT run_id FROM rt_runs WHERE status IN "
                    "('pending','running','interrupted'))",
                    (cutoff,),
                ).rowcount,
                "events": conn.execute(
                    "DELETE FROM rt_events WHERE ts < ? AND kind != 'retire_applied'", (cutoff,)
                ).rowcount,
            }
            closed = [
                r[0]
                for r in conn.execute(
                    "SELECT thread_id FROM rt_threads WHERE status='closed' AND last_active_at < ?",
                    (cutoff,),
                ).fetchall()
            ]
            for tid in closed:
                for table in ("rt_checkpoints", "rt_interrupts", "rt_runs", "rt_threads"):
                    conn.execute(f"DELETE FROM {table} WHERE thread_id=?", (tid,))  # noqa: S608
            out["threads"] = len(closed)
            return out

        return self._tx(_do)
