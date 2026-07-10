"""SM1 substrate tests — long-term memory store, embeddings degradation, and the
context-trimming pre_model_hook. See design/vision/specs/SM1-skipper-memory-substrate.md."""

from __future__ import annotations

from skipper import config, memory


def _dummy_embed(texts):
    """Deterministic 8-dim embedder — avoids a real model download in unit tests."""
    out = []
    for t in texts:
        v = [0.0] * 8
        for i, ch in enumerate(str(t).lower()):
            v[i % 8] += (ord(ch) % 7) / 7.0
        out.append(v)
    return out


def test_build_store_disabled(monkeypatch):
    monkeypatch.setattr(config, "AGENT_MEMORY_ENABLED", False)
    assert memory.build_store() is None


def test_build_store_degrades_when_embeddings_unavailable(monkeypatch):
    # GWT-5: embedding backend down → run with short-term memory only, no crash.
    monkeypatch.setattr(config, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(memory, "build_embeddings", lambda: None)
    assert memory.build_store(":memory:") is None


def test_build_store_roundtrip(tmp_path, monkeypatch):
    # GWT-1/GWT-2: persist a memory and retrieve it via (local) vector search.
    monkeypatch.setattr(config, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_EMBED_DIMS", 8)
    monkeypatch.setattr(memory, "build_embeddings", lambda: _dummy_embed)

    store = memory.build_store(str(tmp_path / "mem.db"))
    assert store is not None
    store.put(("proc", "promote"), "p1", {"text": "validate then canary then promote"})
    store.put(("proc", "promote"), "p2", {"text": "restart the service quickly"})

    hits = store.search(("proc", "promote"), query="how do I promote a model", limit=2)
    assert hits and any(h.value["text"].startswith("validate") for h in hits)
    assert len(store.search(("proc", "promote"))) == 2  # list/filter without a query


def test_build_store_isolated_to_its_own_file(tmp_path, monkeypatch):
    # GWT-3: memory lands only in the given file (not platform.db / the checkpointer).
    monkeypatch.setattr(config, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_EMBED_DIMS", 8)
    monkeypatch.setattr(memory, "build_embeddings", lambda: _dummy_embed)

    path = tmp_path / "skipper_memory.db"
    store = memory.build_store(str(path))
    store.put(("kb",), "k1", {"text": "operators call mbwidth memory bandwidth"})
    assert path.exists()


def test_summarization_hook_trims_long_thread():
    # GWT-4: a thread over budget yields a shorter llm_input_messages (state untouched).
    from langchain_core.messages import AIMessage, HumanMessage

    hook = memory.build_summarization_hook()
    msgs = []
    for i in range(200):
        msgs.append(HumanMessage(content=f"question {i} " * 40))
        msgs.append(AIMessage(content=f"answer {i} " * 40))

    out = hook({"messages": msgs})
    assert "llm_input_messages" in out
    assert 0 < len(out["llm_input_messages"]) < len(msgs)


def test_build_graph_binds_store_and_hook(tmp_path, monkeypatch):
    # The graph compiles with a store + hook wired in (no LLM/embedding call made).
    from skipper import graph

    monkeypatch.setattr(config, "AGENT_DB", str(tmp_path / "g.db"))
    monkeypatch.setattr(config, "AGENT_MEMORY_DB", str(tmp_path / "mem.db"))
    monkeypatch.setattr(config, "AGENT_EMBED_DIMS", 8)
    monkeypatch.setattr(config, "AGENT_SUMMARIZE_ENABLED", True)
    monkeypatch.setattr(memory, "build_embeddings", lambda: _dummy_embed)

    g = graph.build_graph(model="llama3.1:8b")
    assert g is not None
