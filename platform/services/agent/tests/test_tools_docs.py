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


# ── the natural-language failure this search was rebuilt for ────────────────────────────────
#
# `search_docs` used to hand the operator's phrase straight to `rg`. A question never appears
# verbatim in a guide, so it returned "No matches" — and the model read that as "the platform
# does not do this", denying a capability that was fully documented. These pin the fallback.


def _setup_judge_docs(tmp_path, monkeypatch):
    (tmp_path / "guides").mkdir()
    (tmp_path / "guides" / "judge-calibration.md").write_text(
        "# Judge calibration — no uncalibrated judge may gate\n"
        "An LLM judge must be calibrated before it may gate a promotion.\n"
    )
    (tmp_path / "guides" / "unrelated.md").write_text("# Storage\nBuckets and prefixes.\n")
    monkeypatch.setattr(config, "AGENT_DOCS_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "CLAUDE_MD", str(tmp_path / "CLAUDE.md"))


def test_a_question_finds_the_guide_even_though_the_phrase_is_absent(tmp_path, monkeypatch):
    """The regression itself: a whole question, no verbatim match, must still find the guide."""
    _setup_judge_docs(tmp_path, monkeypatch)
    out = docs.search_docs.invoke({"query": "can I use an LLM judge to gate promotion?"})
    assert "judge-calibration.md" in out
    assert "unrelated.md" not in out


def test_a_hyphenated_compound_is_split_into_its_words(tmp_path, monkeypatch):
    """'LLM-as-a-judge' is one token to a tokenizer that keeps hyphens, and matches nothing."""
    _setup_judge_docs(tmp_path, monkeypatch)
    assert "judge-calibration.md" in docs.search_docs.invoke({"query": "LLM-as-a-judge"})


def test_an_exact_phrase_still_wins_and_is_not_diluted(tmp_path, monkeypatch):
    """The literal search runs first — a phrase that IS present returns line hits, not a ranking."""
    _setup_judge_docs(tmp_path, monkeypatch)
    out = docs.search_docs.invoke({"query": "no uncalibrated judge may gate"})
    assert "judge-calibration.md" in out
    assert "ranking by its terms" not in out


def test_ranking_prefers_the_file_that_matches_more_of_the_question(tmp_path, monkeypatch):
    _setup_judge_docs(tmp_path, monkeypatch)
    (tmp_path / "guides" / "passing-mention.md").write_text("# Other\nA promotion happens here.\n")
    out = docs.search_docs.invoke({"query": "LLM judge promotion gate"})
    assert out.index("judge-calibration.md") < out.index("passing-mention.md")


def test_stop_words_alone_do_not_match_everything(tmp_path, monkeypatch):
    """A question made only of stop-words must not rank every file — it has no signal."""
    _setup_judge_docs(tmp_path, monkeypatch)
    out = docs.search_docs.invoke({"query": "what can I do with the"})
    assert "No documentation matched" in out


def test_an_empty_result_says_so_without_implying_the_feature_is_missing(tmp_path, monkeypatch):
    """The wording is the fix: 'no matches' must never read as 'not supported'."""
    _setup_judge_docs(tmp_path, monkeypatch)
    out = docs.search_docs.invoke({"query": "zzzqqq nonexistent subject"})
    assert "NOT that the platform lacks the capability" in out
    assert "list_docs" in out
