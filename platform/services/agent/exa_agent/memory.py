from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from exa_agent import config


def build_checkpointer(db_path: str | None = None) -> SqliteSaver:
    conn = sqlite3.connect(db_path or config.AGENT_DB, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver
