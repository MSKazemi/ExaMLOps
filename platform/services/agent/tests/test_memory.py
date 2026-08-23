"""SM1 substrate tests — long-term memory store, embeddings degradation, and the
context-trimming middleware. See design/vision/specs/SM1-skipper-memory-substrate.md."""

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


def _long_thread(n=200):
    from langchain_core.messages import AIMessage, HumanMessage

    msgs = []
    for i in range(n):
        msgs.append(HumanMessage(content=f"question {i} " * 40))
        msgs.append(AIMessage(content=f"answer {i} " * 40))
    return msgs


def test_trim_to_budget_shortens_long_thread():
    # GWT-4: a thread over budget is trimmed to fit.
    msgs = _long_thread()
    assert 0 < len(memory.trim_to_budget(msgs)) < len(msgs)


def test_trim_middleware_shrinks_what_the_model_receives():
    """The middleware must trim the *model input* and leave graph state alone.

    Asserting only that the agent compiles/returns is not enough: a middleware hooked
    on ``before_model`` (the old ``pre_model_hook`` contract) is silently ignored, so a
    weaker test passes while trimming has actually stopped. This asserts on the message
    list the model was handed.
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    seen = []

    class _Recorder(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            seen.append(list(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kw)

    msgs = _long_thread()
    agent = create_agent(
        _Recorder(messages=iter([AIMessage(content="ok")] * 5)),
        tools=[],
        middleware=[memory.build_trim_middleware()],
    )
    out = agent.invoke({"messages": msgs})

    assert seen, "the model was never called"
    assert len(seen[0]) < len(msgs), "model input was NOT trimmed — middleware is a no-op"
    assert len(out["messages"]) > len(seen[0]), "graph state must keep the full history"


def test_build_graph_binds_store_and_middleware(tmp_path, monkeypatch):
    # The graph compiles with a store + trim middleware wired in (no LLM/embedding call made).
    from skipper import graph

    monkeypatch.setattr(config, "AGENT_DB", str(tmp_path / "g.db"))
    monkeypatch.setattr(config, "AGENT_MEMORY_DB", str(tmp_path / "mem.db"))
    monkeypatch.setattr(config, "AGENT_EMBED_DIMS", 8)
    monkeypatch.setattr(config, "AGENT_SUMMARIZE_ENABLED", True)
    monkeypatch.setattr(memory, "build_embeddings", lambda: _dummy_embed)

    g = graph.build_graph(model="llama3.1:8b")
    assert g is not None


# ── is long-term memory on? nothing could answer that before ────────────────────────────────
#
# A missing embedding backend drops Skipper to short-term memory with a single log line at
# startup. Every operator-QA run on this laptop happened that way and nobody could tell.


def test_status_reports_what_memory_is_configured_to_be(tmp_path, monkeypatch):
    from skipper import config, memory

    monkeypatch.setattr(config, "AGENT_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "AGENT_EMBED_BACKEND", "sentence-transformers")
    monkeypatch.setattr(config, "AGENT_EMBED_MODEL", "all-MiniLM-L6-v2")
    monkeypatch.setattr(config, "AGENT_EMBED_DIMS", 384)
    monkeypatch.setattr(config, "AGENT_MEMORY_DB", str(tmp_path / "m.db"))

    st = memory.status()
    assert st["enabled"] is True
    assert st["backend"] == "sentence-transformers"
    assert st["model"] == "all-MiniLM-L6-v2"
    assert st["dims"] == 384
    assert st["db_exists"] is False  # never created — which is the whole point

    (tmp_path / "m.db").write_text("")
    assert memory.status()["db_exists"] is True


def test_status_never_probes_the_embedding_backend(monkeypatch):
    """It must stay cheap enough for a status endpoint — no model load, no HTTP, no download."""
    from skipper import config, memory

    def explode(*a, **k):
        raise AssertionError("status() must not build embeddings")

    monkeypatch.setattr(memory, "build_embeddings", explode)
    monkeypatch.setattr(config, "AGENT_MEMORY_ENABLED", True)
    assert memory.status()["enabled"] is True


def test_status_reports_disabled_when_the_switch_is_off(monkeypatch):
    from skipper import config, memory

    monkeypatch.setattr(config, "AGENT_MEMORY_ENABLED", False)
    assert memory.status()["enabled"] is False
