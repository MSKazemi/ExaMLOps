from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from skipper import config

try:
    from examlops.resilience import db as _rdb
except Exception:  # pragma: no cover - examlops always present in the agent image
    _rdb = None

log = logging.getLogger("skipper.memory")


def _harden(conn: sqlite3.Connection) -> None:
    """Apply WAL + busy_timeout so concurrent chat sessions don't race into
    ``database is locked``. Uses the shared resilience helper when available."""
    if _rdb is not None:
        _rdb.harden(conn, wal=True)
    else:  # defensive fallback if the shared lib is unavailable
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")


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
    # Agent HA (item 1.8): when AGENT_CHECKPOINT_BACKEND=postgres (+ a DSN), all replicas share one
    # Postgres checkpointer so conversation state survives a replica loss and load-balances. Degrades
    # to the single-node SQLite saver when unset / the Postgres driver or endpoint is unavailable.
    from skipper.checkpoint_backend import postgres_dsn, select_backend

    if select_backend() == "postgres":
        pg = _build_postgres_checkpointer(postgres_dsn())
        if pg is not None:
            return pg
        log.warning("postgres checkpointer unavailable — falling back to SQLite (single-node)")

    path = os.path.abspath(db_path or config.AGENT_DB)
    try:
        conn = sqlite3.connect(path, check_same_thread=False)
        _harden(conn)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver
    except Exception as exc:  # noqa: BLE001 — degrade rather than crash graph build
        log.error("SQLite checkpointer setup failed (%s) — falling back to in-memory", exc)
        return MemorySaver()


def _build_postgres_checkpointer(dsn: str | None):
    """Build a LangGraph Postgres checkpointer for HA, or None to fall back (item 1.8).

    Lazy import + fail-graceful: an absent ``langgraph-checkpoint-postgres`` extra or an unreachable
    DB returns None so the agent still starts on SQLite. NOT runtime-verified in the sandbox (needs
    the extra + a live Postgres); shipped as a reviewable seam like the item-0.1 Postgres backend.
    """
    if not dsn:
        return None
    try:  # pragma: no cover - requires langgraph postgres extra + live PG
        from langgraph.checkpoint.postgres import PostgresSaver

        saver = PostgresSaver.from_conn_string(dsn)
        saver.setup()
        log.info("using Postgres LangGraph checkpointer (agent HA)")
        return saver
    except Exception as exc:  # noqa: BLE001
        log.warning("Postgres checkpointer init failed (%s)", exc)
        return None


def build_embeddings() -> Callable[[list[str]], list[list[float]]] | None:
    """Return a *local* embedding callable ``(texts) -> vectors``, or ``None``.

    Local backends only (no cloud): ``ollama`` (via ``AGENT_OLLAMA_URL``) or
    ``sentence-transformers`` (fully in-process/offline). The backend is probed
    once here so a missing Ollama tunnel / model / dimension-mismatch degrades the
    agent to short-term memory *at startup* rather than crashing a chat turn later.
    Returns ``None`` on any failure — the caller then runs without long-term memory.
    """
    backend = config.AGENT_EMBED_BACKEND
    try:
        if backend in ("sentence-transformers", "st", "hf"):
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(config.AGENT_EMBED_MODEL)

            def _embed(texts: list[str]) -> list[list[float]]:
                return [list(map(float, v)) for v in model.encode(list(texts))]
        else:  # default: ollama (local, offline once the model is pulled)
            from langchain_ollama import OllamaEmbeddings

            emb = OllamaEmbeddings(model=config.AGENT_EMBED_MODEL, base_url=config.AGENT_OLLAMA_URL)

            def _embed(texts: list[str]) -> list[list[float]]:
                return emb.embed_documents(list(texts))

        dims = len(_embed(["ping"])[0])  # probe
        if dims != config.AGENT_EMBED_DIMS:
            log.error(
                "embedding model '%s' returns dim %d but AGENT_EMBED_DIMS=%d — set them to "
                "match; long-term memory disabled",
                config.AGENT_EMBED_MODEL,
                dims,
                config.AGENT_EMBED_DIMS,
            )
            return None
        return _embed
    except Exception as exc:  # noqa: BLE001 — degrade to short-term memory
        log.warning(
            "embedding backend '%s' unavailable (%s) — long-term memory disabled", backend, exc
        )
        return None


def status(db_path: str | None = None) -> dict:
    """What long-term memory is *configured* to be — cheap, and never probes a backend.

    Whether the store actually built is a different question, answered by the compiled graph
    (``graph.store is not None``). This exists because the only signal today is a log line at
    startup: a reachable-embeddings failure silently drops Skipper to short-term memory, and an
    operator asking "does it remember anything?" has nowhere to look.
    """
    path = os.path.abspath(db_path or config.AGENT_MEMORY_DB)
    return {
        "enabled": bool(config.AGENT_MEMORY_ENABLED),
        "backend": config.AGENT_EMBED_BACKEND,
        "model": config.AGENT_EMBED_MODEL,
        "dims": config.AGENT_EMBED_DIMS,
        "db": path,
        "db_exists": os.path.isfile(path),
    }


def build_store(db_path: str | None = None):
    """Build the long-term (cross-thread) memory store, or ``None`` if unavailable.

    Uses the **sync** ``SqliteStore`` (sqlite-vec) to match the agent's
    sync-graph-in-threadpool execution model, in its OWN sqlite file (separate
    from ``platform.db`` and the checkpointer). Local embeddings only. Returns
    ``None`` — the agent then runs with short-term memory only — when disabled or
    when the store / embedding backend is unavailable (memory is additive).
    """
    if not config.AGENT_MEMORY_ENABLED:
        return None
    embed = build_embeddings()
    if embed is None:
        return None
    try:
        from langgraph.store.sqlite import SqliteStore
    except Exception as exc:  # noqa: BLE001 — older checkpoint-sqlite without a Store
        log.warning("langgraph SqliteStore unavailable (%s) — long-term memory disabled", exc)
        return None
    path = os.path.abspath(db_path or config.AGENT_MEMORY_DB)
    try:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.isolation_level = None  # autocommit — SqliteStore manages its own transactions
        _harden(conn)
        # SqliteIndexConfig is not exported from langgraph.store.sqlite for typing,
        # so the index dict is annotated loosely here.
        index = {"dims": config.AGENT_EMBED_DIMS, "embed": embed, "fields": ["text"]}
        store = SqliteStore(conn, index=index)  # type: ignore[arg-type]
        store.setup()
        log.info(
            "long-term memory enabled: %s (embed=%s/%s, dims=%d)",
            path,
            config.AGENT_EMBED_BACKEND,
            config.AGENT_EMBED_MODEL,
            config.AGENT_EMBED_DIMS,
        )
        return store
    except Exception as exc:  # noqa: BLE001 — degrade rather than crash graph build
        log.error("SQLite store setup failed (%s) — long-term memory disabled", exc)
        return None


def trim_to_budget(messages):
    """Trim a message list to ``AGENT_MAX_CONTEXT_TOKENS``.

    Uses langchain-core's ``trim_messages`` (keeps valid human/tool boundaries so the
    ReAct tool loop is not broken). Pure computation — no model call, no cost.
    A full LangMem running-summary is a follow-up phase.
    """
    from langchain_core.messages.utils import count_tokens_approximately, trim_messages

    return trim_messages(
        messages,
        strategy="last",
        token_counter=count_tokens_approximately,
        max_tokens=config.AGENT_MAX_CONTEXT_TOKENS,
        start_on="human",
        end_on=("human", "tool"),
    )


def build_trim_middleware():
    """Middleware that shrinks *model input* only; graph state is untouched.

    Replaces the ``pre_model_hook`` that ``create_react_agent`` took before it was
    superseded by ``langchain.agents.create_agent``.

    ⛔ Must use ``wrap_model_call``, NOT ``before_model``. A ``before_model`` hook
    returning ``{"llm_input_messages": ...}`` — the old hook's contract — is accepted
    and **silently ignored**: no error, no warning, and trimming just stops, so the
    context grows unbounded until requests fail on token limits. This behavior is
    covered by the middleware regression tests.
    """
    from langchain.agents.middleware import AgentMiddleware

    class _TrimMiddleware(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            return handler(request.override(messages=trim_to_budget(request.messages)))

    return _TrimMiddleware()
