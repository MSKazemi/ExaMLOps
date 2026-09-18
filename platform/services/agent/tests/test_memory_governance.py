"""SM3 tests — memory audit, write-gating, lifecycle erase, admin CLI, and the
safety red-team invariant. See design/vision/specs/SM3-skipper-memory-governance-eval.md."""

from __future__ import annotations

import sqlite3

from skipper import config
from skipper import memory_types as mt


def _store(tmp_path):
    from langgraph.store.sqlite import SqliteStore

    def embed(texts):
        return [[(sum(bytearray(str(t).encode())) % 13) / 13.0] + [0.0] * 7 for t in texts]

    conn = sqlite3.connect(str(tmp_path / "m.db"), check_same_thread=False)
    conn.isolation_level = None
    store = SqliteStore(conn, index={"dims": 8, "embed": embed, "fields": ["text"]})
    store.setup()
    return store


def _capture_audit(monkeypatch) -> list[dict]:
    """Record every audit write, patched at the seam the code actually uses.

    These tests used to patch `platform_db.write_audit_event`. `memory_types` now goes through
    `audit_best_effort`, which calls `examlops.data.audit.write_audit_event` — a different binding,
    so the old patch silently intercepted nothing. Patching here keeps the assertions below
    unchanged *and* runs them through the helper that counts a lost event instead of hiding it.
    """
    import examlops.data.audit as audit_mod

    calls: list[dict] = []

    def record(source, actor, action, target, details=None, **kw):
        calls.append({"source": source, "actor": actor, "action": action, "target": target, **kw})

    monkeypatch.setattr(audit_mod, "write_audit_event", record)
    return calls


def test_record_writes_audit_event(tmp_path, monkeypatch):
    # GWT-1: every memory write produces an audit_events row.
    calls = _capture_audit(monkeypatch)
    monkeypatch.setattr(config, "AGENT_MEMORY_AUDIT", True)

    store = _store(tmp_path)
    mt.record_preference(store, "canary", "5%", operator="alice")
    assert calls and calls[0]["action"] == "memory_record"
    assert calls[0]["actor"] == "alice" and calls[0]["source"] == "agent-memory"


def test_audit_disabled_is_silent(tmp_path, monkeypatch):
    calls = _capture_audit(monkeypatch)
    monkeypatch.setattr(config, "AGENT_MEMORY_AUDIT", False)
    mt.record_preference(_store(tmp_path), "x", "y")
    assert calls == []


def test_erase_cascade_and_audit(tmp_path, monkeypatch):
    # GWT-3: deletion removes items and is audited; GWT-4 separation is by design
    # (audit is a separate store, untouched here).
    calls = _capture_audit(monkeypatch)
    store = _store(tmp_path)
    mt.record_preference(store, "a", "1", operator="bob")
    mt.record_preference(store, "b", "2", operator="bob")
    n = mt.erase(store, "pref", scope="bob", operator="bob")
    assert n == 2
    assert mt.list_kind(store, "pref", scope="bob") == []
    assert any(c["action"] == "memory_erase" for c in calls)


def test_record_procedure_is_gated(monkeypatch, tmp_path):
    # GWT-2: procedure writes are confirmation-gated (in WRITE_TOOLS).
    from skipper import confirm
    from skipper.tools import memory as memtools

    assert "record_procedure" in confirm.WRITE_TOOLS
    # With confirmation disabled it writes directly.
    monkeypatch.setattr(config, "AGENT_MEMORY_REQUIRE_CONFIRM", False)
    store = _store(tmp_path)
    out = memtools.record_procedure.func(task_class="x", steps=["a", "b"], store=store)
    assert "Recorded" in out
    assert mt.list_kind(store, "proc", scope="x")


def test_dangerous_tools_all_confirmation_gated():
    # GWT-6 (red-team invariant): no dangerous tool can be triggered by a poisoned
    # memory without a confirmation gate.
    from skipper import confirm, memory_eval
    from skipper.tools import (
        TOOLS,  # noqa: F401 — import populates WRITE_TOOLS
        memory,  # noqa: F401
    )
    from skipper.tools.mcp_bridge import mcp_tools

    mcp_tools(include_writes=True)  # registers every MCP write wrapper with the HITL gate
    assert memory_eval.unguarded_write_tools(confirm.WRITE_TOOLS) == []


def test_memory_admin_stats_export_delete(tmp_path, monkeypatch):
    from skipper import memory_admin

    monkeypatch.setattr(config, "AGENT_MEMORY_AUDIT", False)  # don't touch platform.db in tests
    store = _store(tmp_path)
    mt.record_kb_fact(store, "fact one")
    mt.record_preference(store, "canary", "5%", operator="alice")

    assert '"kb": 1' in memory_admin.main(["stats"], store=store)
    assert "fact one" in memory_admin.main(["export"], store=store)
    assert "Deleted 1" in memory_admin.main(["delete", "kb"], store=store)
    assert '"kb": 0' in memory_admin.main(["stats"], store=store)
