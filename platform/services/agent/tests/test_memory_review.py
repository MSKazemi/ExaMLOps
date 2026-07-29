"""SM3 — procedure-write review-queue (ADR 0034, BL-009).

Queue → list → approve (commits to the store) / reject (drops). Off by default; enabling
``AGENT_MEMORY_REVIEW_QUEUE`` makes ``record_procedure`` enqueue instead of inline-HITL.
"""

from __future__ import annotations

import sqlite3

from skipper import memory_review


def _store(tmp_path):
    from langgraph.store.sqlite import SqliteStore

    def embed(texts):
        return [[float(len(t) % 7)] * 8 for t in texts]

    conn = sqlite3.connect(str(tmp_path / "mem.db"), check_same_thread=False)
    conn.isolation_level = None  # autocommit — SqliteStore manages its own BEGIN/COMMIT
    store = SqliteStore(conn, index={"dims": 8, "embed": embed, "fields": ["text"]})
    store.setup()
    return store


def _db(tmp_path) -> str:
    return str(tmp_path / "review.db")


def test_enqueue_then_list(tmp_path):
    db = _db(tmp_path)
    rid = memory_review.enqueue("safe-promote", ["validate", "canary", "promote"], db_path=db)
    assert rid >= 1
    pending = memory_review.list_pending(db_path=db)
    assert len(pending) == 1
    assert pending[0]["task_class"] == "safe-promote"
    assert pending[0]["steps"] == ["validate", "canary", "promote"]
    assert pending[0]["status"] == "pending"


def test_reject_removes_from_pending(tmp_path):
    db = _db(tmp_path)
    rid = memory_review.enqueue("drift-response", ["a", "b"], db_path=db)
    msg = memory_review.reject(rid, reviewer="alice", reason="incomplete", db_path=db)
    assert f"#{rid}" in msg
    assert memory_review.list_pending(db_path=db) == []
    rec = memory_review.get(rid, db_path=db)
    assert rec["status"] == "rejected" and rec["reason"] == "incomplete"


def test_approve_commits_to_store(tmp_path):
    from skipper import memory_types as mt

    db = _db(tmp_path)
    store = _store(tmp_path)
    rid = memory_review.enqueue("safe-promote", ["validate", "promote"], db_path=db)
    msg = memory_review.approve(rid, store, reviewer="bob", db_path=db)
    assert "committed to memory" in msg
    # no longer pending, marked approved
    assert memory_review.list_pending(db_path=db) == []
    assert memory_review.get(rid, db_path=db)["status"] == "approved"
    # the procedure is now in the store
    items = mt.list_kind(store, "proc")
    assert any("validate" in it.value.get("text", "") for it in items)


def test_approve_and_reject_unknown_id(tmp_path):
    db = _db(tmp_path)
    store = _store(tmp_path)
    assert "No pending review #999" in memory_review.approve(999, store, db_path=db)
    assert "No pending review #999" in memory_review.reject(999, db_path=db)


def test_double_review_is_noop(tmp_path):
    db = _db(tmp_path)
    rid = memory_review.enqueue("x", ["s"], db_path=db)
    memory_review.reject(rid, db_path=db)
    # a second reject on the now-rejected item does nothing
    assert "No pending review" in memory_review.reject(rid, db_path=db)


def test_record_procedure_enqueues_when_review_queue_on(tmp_path, monkeypatch):
    from skipper import config
    from skipper.tools import memory as memtool

    monkeypatch.setattr(config, "AGENT_MEMORY_REVIEW_QUEUE", True)
    monkeypatch.setattr(config, "AGENT_MEMORY_REVIEW_DB", _db(tmp_path))
    store = _store(tmp_path)

    # .func unwraps the langchain @tool decorator to call the plain function
    result = memtool.record_procedure.func("safe-promote", ["validate", "promote"], store=store)
    assert "review #" in result
    # queued, NOT yet written to the store
    from skipper import memory_types as mt

    assert mt.list_kind(store, "proc") == []
    assert len(memory_review.list_pending(db_path=_db(tmp_path))) == 1
