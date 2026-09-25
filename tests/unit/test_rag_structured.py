"""ADR 0019 decision 2 — B8 structured, citation-grounded RAG answers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import rag  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.rag import grounded  # noqa: E402

_DOCS = [
    {
        "id": "d1",
        "text": "promotion moves a model alias from staging to production when the gate passes",
    },
    {"id": "d2", "text": "brownies are baked with chocolate butter sugar and flour in an oven"},
]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    init_db()


def _outcomes() -> list[str]:
    from examlops.data import get_db

    with get_db() as conn:
        return [r["outcome"] for r in conn.execute("SELECT outcome FROM structured_output_events")]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"answer": "a", "citations": [1]}', {"answer": "a", "citations": [1]}),
        ('```json\n{"answer": "a", "citations": []}\n```', {"answer": "a", "citations": []}),
        (
            'Sure! {"answer": "a", "citations": [2]} hope it helps',
            {"answer": "a", "citations": [2]},
        ),
        ("plain prose, no json", {"answer": "plain prose, no json"}),
        ({"answer": "d"}, {"answer": "d"}),
    ],
)
def test_parse_json_answer(raw, expected):
    assert grounded.parse_json_answer(raw) == expected


def test_grounding_drops_citations_that_name_no_retrieved_chunk():
    ga = grounded.check_grounding({"answer": "a", "citations": [1, 3, 0, "x", True, 1, "2"]}, 2)
    assert ga.citations == [1, 2]
    assert ga.dropped_citations == [3, 0, "x", True, 1]
    assert ga.grounded is True


def test_a_fractional_citation_is_dropped_not_truncated():
    # int(2.7) == 2: without an integrality check a citation the model never made (chunk 2)
    # would be kept and mark an ungrounded answer grounded.
    ga = grounded.check_grounding({"answer": "a", "citations": [2.7, 1.5, "2.7", "٢"]}, 3)
    assert ga.citations == [] and ga.grounded is False
    assert ga.dropped_citations == [2.7, 1.5, "2.7", "٢"]
    assert grounded.check_grounding({"answer": "a", "citations": [2.0]}, 3).citations == [2]


def test_no_valid_citation_is_ungrounded_unless_context_insufficient():
    assert grounded.check_grounding({"answer": "a", "citations": [9]}, 2).grounded is False
    ga = grounded.check_grounding({"answer": "?", "citations": [], "insufficient_context": True}, 2)
    assert ga.grounded is True and ga.insufficient_context is True


def test_structured_query_is_valid_grounded_and_metered():
    p = rag.RagPipeline()
    p.ingest("kb", _DOCS, tenant="acme")
    seen: list[str] = []

    def gen(prompt: str) -> str:
        seen.append(prompt)
        return json.dumps({"answer": "via the gate", "citations": [1, 7]})

    ans = p.query(
        "kb", "how does promotion work", tenant="acme", k=2, generate_fn=gen, structured=True
    )
    assert "Respond with ONLY a JSON object" in seen[0]
    assert ans.answer == "via the gate"
    assert ans.grounded is True
    assert ans.structured["citations"] == [1]
    assert ans.structured["dropped_citations"] == [7]
    assert ans.structured["cited_chunks"] == [ans.citations[0].doc_id]
    assert _outcomes() == ["valid"]


def test_prose_answer_is_repaired_and_marked_ungrounded():
    p = rag.RagPipeline()
    p.ingest("kb", _DOCS)
    ans = p.query("kb", "promotion", k=1, generate_fn=lambda _p: "it just works", structured=True)
    assert ans.answer == "it just works"
    assert ans.grounded is False
    assert ans.structured["citations"] == []
    assert _outcomes() == ["repaired"]


def test_free_text_query_is_unchanged():
    p = rag.RagPipeline()
    p.ingest("kb", _DOCS)
    ans = p.query("kb", "promotion", k=1, generate_fn=lambda _p: "free")
    assert ans.answer == "free" and ans.structured is None and ans.grounded is None
    assert _outcomes() == []


def test_cli_structured_json(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    docs = tmp_path / "d.jsonl"
    docs.write_text("\n".join(json.dumps(d) for d in _DOCS) + "\n")
    runner = CliRunner()
    assert runner.invoke(app, ["rag", "ingest", "kb", "--docs", str(docs)]).exit_code == 0
    # Stand in for the gateway-backed generator the CLI uses, returning a JSON answer.
    monkeypatch.setattr(
        rag.RagPipeline, "_default_generate", lambda self, p: '{"answer": "g", "citations": [1]}'
    )
    res = runner.invoke(
        app, ["--json", "rag", "query", "kb", "--question", "promotion", "--structured"]
    )
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["grounded"] is True
    assert doc["structured"]["cited_chunks"] == [doc["citations"][0]["doc_id"]]
