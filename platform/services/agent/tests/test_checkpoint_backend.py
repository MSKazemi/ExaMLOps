"""Agent-HA checkpointer backend selection (enterprise-readiness Phase 1, item 1.8).

Pure selection logic (no LangGraph): postgres is chosen only when requested AND a DSN is present,
otherwise it degrades to sqlite so the agent always starts.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from skipper.checkpoint_backend import postgres_dsn, select_backend  # noqa: E402


def test_default_is_sqlite():
    assert select_backend({}) == "sqlite"


def test_postgres_selected_with_dsn():
    env = {"AGENT_CHECKPOINT_BACKEND": "postgres", "AGENT_POSTGRES_DSN": "postgresql://x"}
    assert select_backend(env) == "postgres"


def test_postgres_without_dsn_falls_back_to_sqlite():
    env = {"AGENT_CHECKPOINT_BACKEND": "postgres"}  # no DSN
    assert select_backend(env) == "sqlite"


def test_database_url_is_accepted_as_dsn():
    env = {"AGENT_CHECKPOINT_BACKEND": "postgres", "DATABASE_URL": "postgresql://y"}
    assert select_backend(env) == "postgres"
    assert postgres_dsn(env) == "postgresql://y"


def test_dsn_prefers_agent_specific_over_database_url():
    env = {"AGENT_POSTGRES_DSN": "postgresql://agent", "DATABASE_URL": "postgresql://shared"}
    assert postgres_dsn(env) == "postgresql://agent"


def test_unknown_backend_is_sqlite():
    assert select_backend({"AGENT_CHECKPOINT_BACKEND": "mysql"}) == "sqlite"
