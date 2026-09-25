"""ADR 0019 — lost RAG audit events are counted, and the operation's outcome stands.

Two sites: `RagPipeline.ingest` records `rag_ingest` after indexing, and the RAG service's
`_tenant_for` (inside `create_app`) records `rag_tenant_denied` before refusing a cross-tenant
token. An audit outage must neither un-index the documents nor turn the 403 into anything else,
and each loss must reach `dropped_audit_events()`. Registered in
`tests/unit/test_audit_losses_are_recorded.py::COVERED_AUDIT_SITES`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))

ADMIN = "admin-token-0123456789abcdef"
ACME = "acme-token-0123456789abcdef"
_DOCS = [
    {"id": "d1", "text": "promotion moves a model alias from staging to production"},
    {"id": "d2", "text": "brownies are baked with chocolate butter sugar and flour"},
]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    for var in (
        "EXAMLOPS_RAG_TOKEN",
        "EXAMLOPS_RAG_TOKENS",
        "EXAMLOPS_GUARDRAIL_MODE",
        "EXAMLOPS_RAG_CONTEXT_GUARD",
        "EXAMLOPS_RAG_MAX_BODY",
        "EXAMLOPS_RAG_TIMEOUT",
        "EXAMLOPS_RAG_MAX_CONCURRENT",
    ):
        monkeypatch.delenv(var, raising=False)
    from examlops import platform_db
    from examlops.data.audit import reset_dropped_audit_events

    platform_db.init_db()
    reset_dropped_audit_events()
    yield
    reset_dropped_audit_events()


def _break_the_audit_log(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_a_lost_ingest_audit_is_counted_and_the_documents_are_still_indexed(monkeypatch):
    from examlops.data import get_db
    from examlops.data.audit import dropped_audit_events
    from examlops.rag import RagPipeline

    _break_the_audit_log(monkeypatch)
    pipe = RagPipeline()
    chunks = pipe.ingest("kb", _DOCS, tenant="acme", source_revision="r1")

    assert chunks == 2
    with get_db() as conn:
        row = conn.execute(
            "SELECT chunk_count, source_revision FROM rag_kbs WHERE kb='kb' AND tenant='acme'"
        ).fetchone()
    assert tuple(row) == (2, "r1")
    hits = pipe.retrieve("kb", "how does promotion work", tenant="acme", k=1)
    assert hits, "ingested chunks must be retrievable although the audit write failed"
    assert dropped_audit_events().get("rag_ingest") == 1, dropped_audit_events()


def test_a_lost_tenant_denial_audit_is_counted_and_the_request_is_still_refused(monkeypatch):
    from fastapi.testclient import TestClient

    from examlops.data.audit import dropped_audit_events
    from examlops.rag.service import create_app

    monkeypatch.setenv("EXAMLOPS_RAG_TOKENS", f"*:{ADMIN},acme:{ACME}")
    c = TestClient(create_app(generate_fn=lambda p: "echo:" + p))
    _break_the_audit_log(monkeypatch)

    r = c.post(
        "/v1/rag/query",
        json={"kb": "kb", "question": "q", "tenant": "globex"},
        headers={"Authorization": f"Bearer {ACME}"},
    )

    assert r.status_code == 403 and r.json()["error"]["code"] == "tenant_forbidden"
    assert dropped_audit_events().get("rag_tenant_denied") == 1, dropped_audit_events()
