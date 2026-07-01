from __future__ import annotations

import logging
import os
import sqlite3

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from skipper import config

try:
    from examlops.resilience import db as _rdb
except Exception:  # pragma: no cover - examlops always present in the agent image
    _rdb = None

log = logging.getLogger("skipper.memory")


def build_checkpointer(db_path: str | None = None):
    """Build a SQLite checkpointer hardened for concurrent chat sessions.

    The agent server runs graph execution in thread-pool executors, so multiple
    WebSocket/HTTP chat sessions write checkpoints through the same SQLite file
    concurrently. Without WAL + a busy_timeout that races into
    ``database is locked``/cursor corruption. We also resolve the DB path to an
    absolute location (``config.AGENT_DB`` defaults to a CWD-relative name) and,
    on any setup failure, fall back to an in-memory saver so the agent stays up
    (losing only cross-restart conversation persistence).
    """
    path = os.path.abspath(db_path or config.AGENT_DB)
    try:
        conn = sqlite3.connect(path, check_same_thread=False)
        if _rdb is not None:
            _rdb.harden(conn, wal=True)
        else:  # defensive fallback if the shared lib is unavailable
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
        saver = SqliteSaver(conn)
        saver.setup()
        return saver
    except Exception as exc:  # noqa: BLE001 — degrade rather than crash graph build
        log.error("SQLite checkpointer setup failed (%s) — falling back to in-memory", exc)
        return MemorySaver()
