"""Three guarantees the GenAI docs describe that the code did not keep (found 2026-09-10).

* **RAG's encoder guard could never fire.** `RagPipeline.query` asked the vector store to check
  the collection's stamp against the knowledge base's *own* recorded encoder — the collection
  compared with itself. A pipeline embedding with a different encoder scored its vectors against
  the KB's and returned confident, cited nonsense.
* **Enforce mode sent secrets on.** The inbound guardrail detected a credential in a prompt and
  redacted only personal data, so the credential reached the model.
* **The semantic cache crashed on content parts.** `bind_to_gateway` embedded a message whose
  content is a list (an image with a question) and raised ``AttributeError``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

_DOC = [{"id": "d1", "text": "drift is a change in the distribution of model inputs"}]
# Built at runtime, as the repo's other secret tests do: a credential-shaped literal in a public
# file trips the dual-git leak scan even when, as here, it is a fixture.
_AWS = "AKIA" + "IOSFODNN7" + "EXAMPLE"
_SLACK = "xox" + "b-1234567890-abc"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    from examlops.data import init_db

    init_db()


# ── RAG encoder guard ─────────────────────────────────────────────────────────


def test_a_pipeline_with_another_encoder_is_refused():
    from examlops.rag import RagPipeline
    from examlops.vector_store import EncoderMismatch

    RagPipeline(encoder_id="minilm@v1").ingest("kb", _DOC)
    with pytest.raises(EncoderMismatch):
        RagPipeline(encoder_id="e5@v2").query("kb", "what is drift?")


def test_the_default_pipeline_round_trips_under_its_own_encoder():
    from examlops.rag import RagPipeline
    from examlops.vector_store import select_store

    rag = RagPipeline()
    assert rag.encoder_id == "token-hash"
    rag.ingest("kb", _DOC)
    assert select_store()._collection("kb", "default")["encoder_id"] == "token-hash"
    assert rag.query("kb", "what is drift?").citations


def test_a_default_pipeline_cannot_query_a_kb_built_by_a_real_model():
    """The case the guard exists for: token-hash vectors against a sentence-model collection."""
    from examlops.rag import RagPipeline
    from examlops.vector_store import EncoderMismatch

    RagPipeline(encoder_id="minilm@v1").ingest("kb", _DOC)
    with pytest.raises(EncoderMismatch):
        RagPipeline().query("kb", "what is drift?")


def test_a_custom_embed_fn_without_an_id_keeps_the_old_behaviour():
    """It has no identity to claim, so it is checked as before rather than guessed at."""
    from examlops.rag import RagPipeline, default_embed

    rag = RagPipeline(embed_fn=lambda t: default_embed(t))
    assert rag.encoder_id is None
    rag.ingest("kb", _DOC)
    assert rag.query("kb", "what is drift?").citations


# ── inbound secrets ───────────────────────────────────────────────────────────


def test_enforce_redacts_a_secret_in_the_request():
    from examlops.guardrails import DefaultGuardrail

    res = DefaultGuardrail(mode="enforce").check_input(f"deploy with key {_AWS} please")
    assert res.action == "redact" and "secret" in res.findings
    assert _AWS not in res.text and "[redacted-secret]" in res.text
    assert res.text.startswith("deploy with key ")  # the rest of the prompt survives


def test_monitor_still_changes_nothing():
    from examlops.guardrails import DefaultGuardrail

    res = DefaultGuardrail(mode="monitor").check_input(f"key {_AWS}")
    assert res.action == "allow" and res.text == f"key {_AWS}" and "secret" in res.findings


def test_redact_secrets_reports_the_rules_it_hit():
    from examlops.secrets import redact_secrets

    text, rules = redact_secrets(f"a {_AWS} b {_SLACK}")
    assert rules == ["aws-access-key", "slack-token"]
    assert _AWS not in text and _SLACK not in text


def test_the_gateway_does_not_send_a_secret_in_enforce_mode(monkeypatch):
    from examlops import gateway as gw

    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_MODE", "enforce")
    seen: list[str] = []

    def backend(model, messages, **kw):
        seen.append(messages[-1]["content"])
        return "ok"

    router = gw.Router()
    router.add_route("m", [("stub", backend)])
    gw.GatewayClient(router).chat("m", [{"role": "user", "content": f"use {_AWS}"}])
    assert _AWS not in seen[0]


# ── semantic cache and content parts ──────────────────────────────────────────


def test_the_cache_hooks_never_key_on_a_content_part_message():
    from examlops.semantic_cache import SemanticCache, bind_to_gateway

    lookup, store = bind_to_gateway(SemanticCache())
    parts = [{"role": "user", "content": [{"type": "text", "text": "what is in this chart?"}]}]
    store("m", parts, "a bar chart")  # used to raise AttributeError
    assert lookup("m", parts) is None
    text = [{"role": "user", "content": "what is in this chart?"}]
    assert lookup("m", text) is None  # nothing was stored under the text either
