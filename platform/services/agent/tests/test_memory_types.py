"""SM2 tests — memory-type schemas, store helpers, and the store-backed tools.
See design/vision/specs/SM2-skipper-memory-types.md."""

from __future__ import annotations

import sqlite3

import pytest
from skipper import memory_types as mt
from skipper.tools import memory as memtools


def _store(tmp_path):
    """A real SqliteStore with a deterministic dummy embedder (no model download)."""
    from langgraph.store.sqlite import SqliteStore

    def embed(texts):
        out = []
        for t in texts:
            v = [0.0] * 8
            for i, ch in enumerate(str(t).lower()):
                v[i % 8] += (ord(ch) % 7) / 7.0
            out.append(v)
        return out

    conn = sqlite3.connect(str(tmp_path / "m.db"), check_same_thread=False)
    conn.isolation_level = None
    store = SqliteStore(conn, index={"dims": 8, "embed": embed, "fields": ["text"]})
    store.setup()
    return store


def test_namespace_validates():
    assert mt.namespace("proc") == ("proc",)
    assert mt.namespace("pref", "alice") == ("pref", "alice")
    with pytest.raises(ValueError):
        mt.namespace("bogus")


def test_record_and_recall_procedure(tmp_path):
    store = _store(tmp_path)
    mt.record_procedure(store, "safe-promote", ["validate", "canary 10%", "promote"], "latency ok")
    hits = mt.recall(store, "proc", "how do I promote a model safely")
    assert hits and hits[0].value["data"]["task_class"] == "safe-promote"


def test_deprecated_procedure_is_filtered(tmp_path):
    store = _store(tmp_path)
    key = mt.record_procedure(store, "x", ["a"])
    item = store.get(("proc", "x"), key)
    value = item.value
    value["data"]["deprecated"] = True
    store.put(("proc", "x"), key, value)
    assert mt.recall(store, "proc", "anything") == []


def test_preference_roundtrip_scoped_and_prefix(tmp_path):
    store = _store(tmp_path)
    mt.record_preference(store, "canary", "5%", operator="alice")
    scoped = mt.recall(store, "pref", "canary preference", scope="alice")
    assert scoped and scoped[0].value["data"]["value"] == "5%"
    # prefix search (all operators) also finds it
    assert mt.recall(store, "pref", "canary")


def test_incident_stores_pointers_not_copies(tmp_path):
    store = _store(tmp_path)
    mt.record_incident(
        store,
        "jpcp",
        "drift CRITICAL",
        root_cause="schema change",
        resolution="rebaseline",
        audit_event_id=42,
        drift_snapshot_id=7,
    )
    hits = mt.recall(store, "episode", "jpcp drift incident")
    data = hits[0].value["data"]
    assert data["audit_event_id"] == 42 and data["drift_snapshot_id"] == 7
    # the abstraction is stored, not the platform_db rows themselves
    assert "symptom" in data and data["model"] == "jpcp"


def test_list_kind(tmp_path):
    store = _store(tmp_path)
    mt.record_kb_fact(store, "operators call mbwidth memory bandwidth", tags=["fdata"])
    assert len(mt.list_kind(store, "kb")) == 1


def test_memory_tools_work_with_injected_store(tmp_path):
    store = _store(tmp_path)
    # exercise the tool bodies directly (.func) with an injected store
    out = memtools.remember_preference.func(topic="canary", value="5%", store=store)
    assert "canary" in out
    recalled = memtools.recall_memory.func(query="canary", kind="pref", store=store)
    assert "5%" in recalled or "preference" in recalled.lower()
    assert memtools.recall_memory.func(query="x", kind="bogus", store=store).startswith("Unknown")
    assert {t.name for t in memtools.TOOLS} == {
        "recall_memory",
        "remember_preference",
        "record_procedure",
    }
