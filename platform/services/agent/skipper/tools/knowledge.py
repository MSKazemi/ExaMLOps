"""Knowledge tool — semantic docs search with graceful ripgrep fallback (T2, Phase 3).

``search_knowledge`` is the agent's authoritative "how does the platform work / how do I …?"
tool. It first tries the T2 docs-RAG tier (:mod:`skipper.knowledge`, semantic retrieval with
citations); if that tier is unavailable (no embeddings / vector store / not yet ingested) it falls
back to the existing ripgrep docs tool (:func:`skipper.tools.docs.search_docs`) — so it is never
worse than today.
"""

from __future__ import annotations

from langchain_core.tools import tool

from skipper import config, knowledge
from skipper.tools import docs


@tool
def search_knowledge(query: str) -> str:
    """Answer a "how does it work / how do I …?" question from the ExaMLOps documentation.

    Semantically retrieves the most relevant documentation chunks (guides, tutorials, ADRs, CLI
    reference) with their source paths as citations. Use this before answering any question about
    how the platform works or how to perform an operation — the docs are authoritative. Falls back
    to a keyword search when the semantic index is unavailable.
    """
    # k comes from config, not a literal: the default 5 was measured to cut off the correct
    # answer for a realistic operator question (see AGENT_KNOWLEDGE_K).
    hits = knowledge.query(query, k=config.AGENT_KNOWLEDGE_K)
    if not hits:
        # Degrade to the ripgrep docs tool — identical to the pre-T2 behaviour.
        # search_docs is a LangChain @tool, so invoke it rather than calling directly.
        return docs.search_docs.invoke({"query": query})
    lines = ["Relevant documentation (semantic search):", ""]
    for h in hits:
        lines.append(f"**{h.path}** (score {h.score:.2f})")
        lines.append(f"> {h.text.strip()[:400]}")
        lines.append("")
    return "\n".join(lines)


TOOLS = [search_knowledge]
