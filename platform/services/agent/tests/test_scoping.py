"""Phase 8 (Skipper next-gen) — tenant/project memory scoping (X, ADR 0105).

Verifies that with scoping OFF (the default) namespaces are byte-for-byte unchanged; that with
scoping ON memories are written under the active tenant and only recalled by that tenant + the
shared bucket; that a different tenant cannot recall another's memory; and that the authz gate
excludes an unauthorized tenant's memory while the shared bucket stays readable. In-memory store.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CLI_SRC = Path(__file__).resolve().parents[3] / "platform" / "cli" / "src"
sys.path.insert(0, str(_CLI_SRC))

from skipper import config, memory_types, scoping  # noqa: E402


@pytest.fixture
def store():
    from langgraph.store.memory import InMemoryStore

    return InMemoryStore()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "scope.db"))
    from examlops.data import init_db

    init_db()
    yield


# ── default OFF → unchanged ───────────────────────────────────────────────────


def test_scoping_off_is_identity(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MEMORY_TENANT_SCOPED", False)
    assert scoping.active_tenant() is None
    assert scoping.write_prefix() == ()
    assert scoping.read_prefixes() == [()]
    assert memory_types.namespace("proc", "drift") == ("proc", "drift")
    assert memory_types.namespace("kb") == ("kb",)


# ── ON → tenant isolation ─────────────────────────────────────────────────────


def _enable(monkeypatch, project):
    monkeypatch.setattr(config, "AGENT_MEMORY_TENANT_SCOPED", True)
    monkeypatch.setenv("EXAMLOPS_PROJECT", project)


def test_namespace_prefixed_when_scoped(monkeypatch):
    _enable(monkeypatch, "alpha")
    assert scoping.write_prefix() == ("t:alpha",)
    assert memory_types.namespace("proc", "drift") == ("t:alpha", "proc", "drift")


def test_recall_isolated_per_tenant(db, store, monkeypatch):
    _enable(monkeypatch, "alpha")
    memory_types.record_kb_fact(store, "alpha-only secret", tags=["x"])
    assert any("alpha-only" in h.value.get("text", "") for h in memory_types.list_kind(store, "kb"))

    # switch to a different project → alpha's memory is not visible
    _enable(monkeypatch, "beta")
    assert not any(
        "alpha-only" in h.value.get("text", "") for h in memory_types.list_kind(store, "kb")
    )


def test_shared_bucket_is_readable_by_all(db, store, monkeypatch):
    # write a fact into the shared bucket (as the 'global' tenant) …
    _enable(monkeypatch, config.AGENT_MEMORY_SHARED_BUCKET)
    memory_types.record_kb_fact(store, "shared platform fact", tags=["kb"])
    # … any other tenant can recall it
    _enable(monkeypatch, "gamma")
    texts = [h.value.get("text", "") for h in memory_types.list_kind(store, "kb")]
    assert any("shared platform fact" in t for t in texts)


def test_authz_gate_excludes_unauthorized_tenant(db, store, monkeypatch):
    _enable(monkeypatch, "alpha")
    memory_types.record_kb_fact(store, "alpha private", tags=["x"])
    # deny read on the active tenant → only the shared bucket is searched
    monkeypatch.setattr(scoping, "can_read", lambda operator, tenant: False)
    texts = [h.value.get("text", "") for h in memory_types.list_kind(store, "kb")]
    assert not any("alpha private" in t for t in texts)
