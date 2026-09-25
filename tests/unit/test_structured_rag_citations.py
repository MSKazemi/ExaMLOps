# tests/unit/test_structured_rag_citations.py
"""ADR 0035 clause 1 — "RAG citations use it": a structured RAG answer is schema-validated and
its citations are the chunks the model cited, checked against what was actually retrieved."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.platform_db import init_db  # noqa: E402
from examlops.rag import RagPipeline  # noqa: E402
from examlops.structured import StructuredOutputError  # noqa: E402

DOCS = [
    ("d1", "The JPCP model predicts job power consumption on HPC clusters."),
    ("d2", "MinIO stores dataset snapshots and model artifacts."),
    ("d3", "Prefect orchestrates the training pipelines."),
]


@pytest.fixture
def rag(monkeypatch, tmp_path):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("EXAMLOPS_GUARDRAIL_MODE", "off")
    monkeypatch.setenv("EXAMLOPS_STRUCTURED_CONFIG", str(tmp_path / "none.yaml"))
    monkeypatch.setattr("examlops.policy.POLICY_YAML", tmp_path / "policy.yaml")
    init_db()
    p = RagPipeline()
    p.ingest("kb", [{"id": i, "text": t} for i, t in DOCS])
    return p


def _hits(rag):
    return rag.query("kb", "what predicts power", k=2, generate_fn=lambda _p: "x").citations


def test_structured_answer_returns_only_cited_chunks(rag):
    retrieved = _hits(rag)
    seen: list[str] = []

    def gen(prompt: str) -> str:
        seen.append(prompt)
        return "Sure!\n```json\n" + json.dumps({"answer": "JPCP", "citations": [2, 2]}) + "\n```"

    ans = rag.query("kb", "what predicts power", k=2, generate_fn=gen, structured=True)
    assert ans.structured is not None and ans.grounded is True
    assert ans.answer == "JPCP"
    assert [c.doc_id for c in ans.citations] == [retrieved[1].doc_id]  # de-duplicated
    assert ans.dropped_citations == [2]  # the repeat is reported, not silently discarded
    assert '"citations"' in seen[0]  # the model was told the shape


def test_hallucinated_chunk_numbers_are_dropped_not_returned(rag):
    gen = lambda _p: json.dumps({"answer": "a", "citations": [1, 9, 0]})  # noqa: E731
    ans = rag.query("kb", "what predicts power", k=2, generate_fn=gen, structured=True)
    assert len(ans.citations) == 1
    assert ans.dropped_citations == [9, 0]


def test_repairable_answer_is_repaired(rag):
    gen = lambda _p: json.dumps({"answer": "a", "citations": ["1"], "extra": True})  # noqa: E731
    ans = rag.query("kb", "what predicts power", k=2, generate_fn=gen, structured=True)
    assert len(ans.citations) == 1


def test_unusable_answer_is_ungrounded_and_cites_nothing(rag):
    """Prose instead of JSON is repaired by B8 (metered) and marked ungrounded — it never comes
    back looking like a grounded answer, and it carries no citations it did not make."""
    ans = rag.query("kb", "q", k=2, generate_fn=lambda _p: "I cannot answer that", structured=True)
    assert ans.grounded is False
    assert ans.citations == []


def test_schema_invalid_answer_raises_instead_of_returning_text(rag):
    with pytest.raises(StructuredOutputError):
        rag.query(
            "kb",
            "q",
            k=2,
            generate_fn=lambda _p: json.dumps({"answer": 7, "citations": "x"}),
            structured=True,
        )


def test_default_path_goes_through_the_gateway_schema(rag, monkeypatch):
    from examlops import gateway as gw

    asked: dict = {}

    def fake_chat(self, model, messages, **kw):
        asked.update(kw)
        return gw.Completion(
            text="", model=model, backend="t", parsed={"answer": "ok", "citations": [1]}
        )

    monkeypatch.setattr(gw.GatewayClient, "chat", fake_chat)
    ans = rag.query("kb", "what predicts power", k=2, structured=True)
    assert asked["response_schema"] == "rag_answer"
    assert ans.answer == "ok" and len(ans.citations) == 1


def test_unstructured_query_is_unchanged(rag):
    ans = rag.query("kb", "what predicts power", k=2, generate_fn=lambda _p: "free text")
    assert ans.answer == "free text" and ans.structured is None and len(ans.citations) == 2


def test_non_integer_citations_are_dropped_without_jsonschema(rag, monkeypatch):
    """The dependency-free validator checks neither array items nor bool-vs-int, so a float, a
    string, a bool or a list can reach the citation loop. None may crash it or map to a chunk."""
    import examlops.structured as st

    monkeypatch.setattr(st, "validate_object", st._minimal_validate)
    gen = lambda _p: json.dumps(  # noqa: E731
        {"answer": "a", "citations": [2, 1.0, "1", True, [1]]}
    )
    ans = rag.query("kb", "what predicts power", k=2, generate_fn=gen, structured=True)
    # 2 and the integral float 1.0 name chunks; "1" then repeats chunk 1; a bool or a list never
    # names one (examlops.rag.grounded.check_grounding).
    assert len(ans.citations) == 2
    assert ans.dropped_citations == ["1", True, [1]]


def test_policy_refusal_is_not_turned_into_an_answer(rag, tmp_path):
    """The unstructured default path swallowed every gateway error into '(no answer)', which made
    a D5 refusal look like a successful, empty answer."""
    from examlops.gateway import ReasoningPolicyDenied

    (tmp_path / "policy.yaml").write_text(
        "policies:\n  - action: reasoning_request\n    effect: deny\n"
    )
    with pytest.raises(ReasoningPolicyDenied):
        rag.query("kb", "what predicts power", k=2)
