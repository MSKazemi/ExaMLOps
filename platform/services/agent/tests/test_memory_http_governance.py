"""Authenticated, principal-scoped memory governance HTTP tests."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_memory_administration_never_uses_anonymous_development_identity(monkeypatch):
    from skipper import config, server

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", "")

    assert TestClient(server.app).get("/api/memory/stats").status_code == 401


def test_memory_routes_require_auth_and_isolate_principals(monkeypatch):
    from skipper import config, memory_types, scoping, server

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", '{"alice":"key-a","bob":"key-b"}')
    monkeypatch.setattr(config, "AGENT_TENANT", "tenant-one")
    monkeypatch.setattr(config, "AGENT_MEMORY_AUDIT", True)
    audited: list[tuple[str, str]] = []
    monkeypatch.setattr(
        memory_types,
        "audit_memory_op",
        lambda action, _kind, _scope, operator, _digest, _session: audited.append(
            (action, operator)
        ),
    )
    store = InMemoryStore()
    with scoping.identity_scope("alice", "tenant-one"):
        alice_key = memory_types.record_kb_fact(store, uuid.uuid4().hex, operator="alice")
    with scoping.identity_scope("bob", "tenant-one"):
        bob_key = memory_types.record_kb_fact(store, uuid.uuid4().hex, operator="bob")
    with scoping.identity_scope("alice", "tenant-two"):
        memory_types.record_kb_fact(store, uuid.uuid4().hex, operator="alice")
    audited.clear()
    monkeypatch.setattr(server, "_memory_store", lambda: store)
    client = TestClient(server.app)

    assert client.get("/api/memory/stats").status_code == 401
    alice = client.get("/api/memory/list/kb", headers=_headers("key-a")).json()["items"]
    bob = client.get("/api/memory/list/kb", headers=_headers("key-b")).json()["items"]
    assert [item["key"] for item in alice] == [alice_key]
    assert [item["key"] for item in bob] == [bob_key]
    exported = client.get("/api/memory/export", headers=_headers("key-a")).json()["memories"]
    assert [item["key"] for item in exported["kb"]] == [alice_key]

    denied = client.post(
        "/api/memory/delete",
        headers=_headers("key-a"),
        json={"kind": "kb", "scope": None, "confirmation": ""},
    )
    assert denied.status_code == 409
    deleted = client.post(
        "/api/memory/delete",
        headers=_headers("key-a"),
        json={"kind": "kb", "scope": None, "confirmation": "erase-owned-memory"},
    )
    assert deleted.json()["erased"] == 1
    assert audited == [("memory_erase", "alice")]
    assert client.get("/api/memory/stats", headers=_headers("key-a")).json()["counts"]["kb"] == 0
    assert client.get("/api/memory/stats", headers=_headers("key-b")).json()["counts"]["kb"] == 1


def test_review_routes_enforce_owner_and_audit_mutations(monkeypatch, tmp_path):
    from skipper import config, memory_review, memory_types, scoping, server

    monkeypatch.setattr(config, "AGENT_API_KEY", "")
    monkeypatch.setattr(config, "AGENT_API_KEYS_JSON", '{"alice":"key-a","bob":"key-b"}')
    monkeypatch.setattr(config, "AGENT_TENANT", "tenant-one")
    monkeypatch.setattr(config, "AGENT_MEMORY_REVIEW_DB", str(tmp_path / "reviews.db"))
    monkeypatch.setattr(config, "AGENT_MEMORY_AUDIT", True)
    store = InMemoryStore()
    monkeypatch.setattr(server, "_memory_store", lambda: store)
    audited: list[tuple[str, str]] = []
    monkeypatch.setattr(
        memory_types,
        "audit_memory_op",
        lambda action, _kind, _scope, operator, _digest, _session: audited.append(
            (action, operator)
        ),
    )
    with scoping.identity_scope("alice", "tenant-one"):
        review_id = memory_review.enqueue("workflow", ["validate"])
    client = TestClient(server.app)

    assert client.get("/api/memory/reviews", headers=_headers("key-b")).json() == {"reviews": []}
    forbidden = client.post(
        f"/api/memory/reviews/{review_id}/approve", headers=_headers("key-b"), json={}
    )
    assert forbidden.status_code == 404
    approved = client.post(
        f"/api/memory/reviews/{review_id}/approve", headers=_headers("key-a"), json={}
    )
    assert approved.json() == {"review_id": review_id, "status": "approved"}
    assert ("memory_review_approve", "alice") in audited
    assert client.get("/api/memory/stats", headers=_headers("key-a")).json()["counts"]["proc"] == 1
    assert client.get("/api/memory/stats", headers=_headers("key-b")).json()["counts"]["proc"] == 0
