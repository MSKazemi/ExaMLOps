"""Read-only view of the agent runtime's LangGraph SQLite checkpoint store.

The agent (``skipper``) writes checkpoints at super-step boundaries into ``AGENT_DB`` (table
``checkpoints``: ``thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint,
metadata``). This module *reads* that file through :func:`examlops.resilience.db.connect_snapshot`
(``mode=ro``) - the platform never writes the agent's store, and importing LangGraph here would
couple core to the agent package (ADR 0094 boundary). The Postgres checkpointer is not covered:
selecting it needs a driver and a live server, so a Postgres deployment gets no checkpoint-only
backend until a store for it exists.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from examlops.resilience.db import connect_snapshot

from .types import SuspendError


@dataclass(frozen=True)
class CheckpointRef:
    thread_id: str
    checkpoint_ns: str
    checkpoint_id: str
    size_bytes: int


class CheckpointStore(Protocol):
    def latest(self, thread_id: str) -> CheckpointRef | None: ...

    def get(
        self, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> CheckpointRef | None: ...

    def read_blob(self, ref: CheckpointRef) -> bytes: ...


class LangGraphSqliteStore:
    """``CheckpointStore`` over a LangGraph ``SqliteSaver`` file, opened read-only."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)

    def _conn(self) -> sqlite3.Connection:
        if not os.path.exists(self.path):
            raise SuspendError(f"agent checkpoint store not found: {self.path}")
        return connect_snapshot(self.path)

    def _ref(self, row: sqlite3.Row | None) -> CheckpointRef | None:
        if row is None:
            return None
        return CheckpointRef(
            row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"], int(row["n"] or 0)
        )

    def latest(self, thread_id: str) -> CheckpointRef | None:
        # checkpoint_id is a time-ordered uuid6 in LangGraph, so the max id is the newest.
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id, length(checkpoint) AS n "
                "FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' "
                "ORDER BY checkpoint_id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._ref(row)

    def get(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> CheckpointRef | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id, length(checkpoint) AS n "
                "FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
                (thread_id, checkpoint_ns, checkpoint_id),
            ).fetchone()
        finally:
            conn.close()
        return self._ref(row)

    def read_blob(self, ref: CheckpointRef) -> bytes:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT checkpoint FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? "
                "AND checkpoint_id=?",
                (ref.thread_id, ref.checkpoint_ns, ref.checkpoint_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row[0] is None:
            raise SuspendError("checkpoint vanished from the agent store")
        return bytes(row[0])
