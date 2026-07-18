"""Checkpointer backend selection for agent HA (Phase 1 item 1.8).

Single-node Skipper uses a SQLite LangGraph checkpointer, so conversation state lives on one host and
can't be shared by N replicas behind a load balancer. For HA the checkpointer must be Postgres (all
replicas read/write one store). This module is the *pure* selection logic — no LangGraph import — so
it's testable in isolation; ``memory.build_checkpointer`` calls it and constructs the actual saver.

``AGENT_CHECKPOINT_BACKEND`` picks the backend (``sqlite`` default / ``postgres``); the Postgres DSN
comes from ``AGENT_POSTGRES_DSN`` or ``DATABASE_URL``. A ``postgres`` selection with no DSN falls back
to ``sqlite`` (degrade-gracefully) — surfaced here so the caller can log it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def select_backend(env: Mapping[str, str] | None = None) -> str:
    """Return the effective checkpointer backend: ``sqlite`` (default) or ``postgres``.

    A ``postgres`` request without a DSN degrades to ``sqlite`` so the agent never fails to start.
    """
    env = env if env is not None else os.environ
    backend = (env.get("AGENT_CHECKPOINT_BACKEND", "sqlite") or "sqlite").strip().lower()
    if backend == "postgres" and not postgres_dsn(env):
        return "sqlite"  # no DSN → fall back
    return "postgres" if backend == "postgres" else "sqlite"


def postgres_dsn(env: Mapping[str, str] | None = None) -> str | None:
    """The Postgres DSN for the checkpointer, or None."""
    env = env if env is not None else os.environ
    return env.get("AGENT_POSTGRES_DSN") or env.get("DATABASE_URL") or None
