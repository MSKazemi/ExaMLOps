"""ADR 0019 decision 1 — the swappable RAG framework seam (native | LlamaIndex | auto)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import rag  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.rag import frameworks  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.delenv("EXAMLOPS_RAG_FRAMEWORK", raising=False)
    init_db()


class _FakeSentenceSplitter:
    """Faithful to llama_index.core.node_parser.SentenceSplitter's constructor + split_text.

    Refuses overlap > size exactly as the real class does, packs whole sentences (as produced by
    the chunking tokenizer) up to ``chunk_size`` tokens (as counted by the tokenizer).
    """

    calls: list[dict] = []

    def __init__(self, chunk_size, chunk_overlap, tokenizer=None, chunking_tokenizer_fn=None):
        if chunk_overlap > chunk_size:
            raise ValueError("Got a larger chunk overlap than chunk size")
        self.size, self.tok, self.sent = chunk_size, tokenizer, chunking_tokenizer_fn
        type(self).calls.append(
            {
                "size": chunk_size,
                "overlap": chunk_overlap,
                "tok": tokenizer,
                "sent": chunking_tokenizer_fn,
            }
        )

    def split_text(self, text):
        out, cur, n = [], "", 0
        for s in self.sent(text):
            t = len(self.tok(s))
            if cur and n + t > self.size:
                out.append(cur.strip())
                cur, n = "", 0
            cur += s
            n += t
        if cur.strip():
            out.append(cur.strip())
        return out


@pytest.fixture
def fake_llamaindex(monkeypatch):
    _FakeSentenceSplitter.calls = []
    pkg = types.ModuleType("llama_index")
    core = types.ModuleType("llama_index.core")
    np = types.ModuleType("llama_index.core.node_parser")
    np.SentenceSplitter = _FakeSentenceSplitter
    pkg.core = core
    core.node_parser = np
    monkeypatch.setitem(sys.modules, "llama_index", pkg)
    monkeypatch.setitem(sys.modules, "llama_index.core", core)
    monkeypatch.setitem(sys.modules, "llama_index.core.node_parser", np)
    return _FakeSentenceSplitter


@pytest.fixture
def no_llamaindex(monkeypatch):
    monkeypatch.setitem(sys.modules, "llama_index", None)
    monkeypatch.setitem(sys.modules, "llama_index.core", None)
    monkeypatch.setitem(sys.modules, "llama_index.core.node_parser", None)


_TEXT = (
    "The job failed. It ran out of memory on node a12. Operators restarted it with more RAM. "
    "It then succeeded after two hours."
)


def test_native_is_default_and_byte_identical_to_chunk_text():
    text = " ".join(str(i) for i in range(100))
    chunker = frameworks.get_chunker()
    assert chunker.name == "native"
    assert chunker.split(text, 40, 10) == rag.chunk_text(text, 40, 10)


def test_env_selects_framework_and_bad_name_is_refused(monkeypatch, fake_llamaindex):
    monkeypatch.setenv("EXAMLOPS_RAG_FRAMEWORK", "llamaindex")
    assert frameworks.get_chunker().name == "llamaindex"
    with pytest.raises(ValueError):
        frameworks.resolve_framework("haystack-v9")


def test_explicit_llamaindex_missing_fails_instead_of_degrading(no_llamaindex):
    with pytest.raises(frameworks.RagFrameworkUnavailable, match="rag-llamaindex"):
        frameworks.get_chunker("llamaindex")


def test_auto_degrades_to_native_when_missing(no_llamaindex):
    assert frameworks.resolve_framework("auto") == "native"


def test_auto_prefers_llamaindex_when_installed(fake_llamaindex):
    assert frameworks.resolve_framework("auto") == "llamaindex"


def test_llamaindex_split_keeps_sentences_whole_and_counts_words(fake_llamaindex):
    chunks = frameworks.get_chunker("llamaindex").split(_TEXT, 10, 50)
    call = fake_llamaindex.calls[-1]
    # overlap clamped below size, as native clamps its step — never a ValueError
    assert call["size"] == 10 and call["overlap"] == 9
    # size is in words, and no network tokenizer is involved
    assert call["tok"]("a b c") == ["a", "b", "c"]
    assert all(c.rstrip().endswith((".", "!", "?")) for c in chunks)
    # sentences are rejoined with their whitespace, never "failed.It"
    assert all(". " in c or c.count(".") == 1 for c in chunks)
    assert "failed.It" not in " ".join(chunks)


def test_ingest_uses_selected_framework_and_audits_it(fake_llamaindex):
    from examlops.data import get_db

    p = rag.RagPipeline(framework="llamaindex", chunk_size=10, chunk_overlap=2)
    n = p.ingest("kb", [{"id": "d1", "text": _TEXT}], tenant="acme", source_revision="r1")
    assert n >= 2 and fake_llamaindex.calls
    ans = p.query("kb", "why did the job fail", tenant="acme", k=1, generate_fn=lambda _p: "x")
    assert ans.citations[0].doc_id.startswith("d1#")
    with get_db() as conn:
        row = conn.execute(
            "SELECT target, details FROM audit_events WHERE action='rag_ingest'"
        ).fetchone()
    assert row["target"] == "acme/kb"
    details = json.loads(row["details"])
    assert details["framework"] == "llamaindex" and details["chunks"] == n
    assert details["source_revision"] == "r1"


def test_unavailable_framework_writes_nothing(no_llamaindex):
    from examlops.data import get_db

    with pytest.raises(frameworks.RagFrameworkUnavailable):
        rag.RagPipeline(framework="llamaindex").ingest("kb", [{"id": "d", "text": _TEXT}])
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rag_kbs").fetchone()[0] == 0


def test_real_llamaindex_when_installed():
    pytest.importorskip("llama_index.core.node_parser")
    chunks = frameworks.get_chunker("llamaindex").split(_TEXT * 3, 20, 5)
    assert len(chunks) >= 2
    assert "failed.It" not in " ".join(chunks)


def test_cli_ingest_rejects_unknown_framework(tmp_path):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    docs = tmp_path / "d.jsonl"
    docs.write_text(json.dumps({"id": "d1", "text": _TEXT}) + "\n")
    res = CliRunner().invoke(
        app, ["rag", "ingest", "kb", "--docs", str(docs), "--framework", "nope"]
    )
    assert res.exit_code != 0
    assert "--framework" in res.output
