from skipper import config
from skipper.tools import docs


def _setup_docs(tmp_path, monkeypatch):
    (tmp_path / "guides").mkdir()
    (tmp_path / "guides" / "agent.md").write_text("# Agent\nThe agent uses Ollama and LangGraph.\n")
    (tmp_path / "architecture.md").write_text("# Architecture\nControl plane on port 18002.\n")
    monkeypatch.setattr(config, "AGENT_DOCS_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "CLAUDE_MD", str(tmp_path / "CLAUDE.md"))


def test_list_docs(tmp_path, monkeypatch):
    _setup_docs(tmp_path, monkeypatch)
    out = docs.list_docs.invoke({})
    assert "guides/agent.md" in out and "architecture.md" in out


def test_search_docs_finds_term(tmp_path, monkeypatch):
    _setup_docs(tmp_path, monkeypatch)
    out = docs.search_docs.invoke({"query": "Ollama"})
    assert "agent.md" in out


def test_read_doc(tmp_path, monkeypatch):
    _setup_docs(tmp_path, monkeypatch)
    out = docs.read_doc.invoke({"path": "architecture.md"})
    assert "Control plane on port 18002" in out


def test_read_doc_rejects_traversal(tmp_path, monkeypatch):
    _setup_docs(tmp_path, monkeypatch)
    out = docs.read_doc.invoke({"path": "../../../etc/passwd"})
    assert "outside the docs root" in out


def test_get_howto_known_topic(tmp_path, monkeypatch):
    _setup_docs(tmp_path, monkeypatch)
    out = docs.get_howto.invoke({"topic": "add a model"})
    assert "exa scaffold" in out
