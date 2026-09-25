"""RAG framework seam (ADR 0019 decision 1): which library turns a document into chunks.

ADR 0019 names **LlamaIndex** as the pipeline's foundation (Haystack as the alternative) and keeps
"a thin platform seam so the framework is swappable". This module is that seam for the *ingest*
side: a :class:`Chunker` is selected by name and every chunker returns plain ``list[str]`` so the
embed → index half of the pipeline, the B5 store, encoder stamping (ADR 0043) and tenant isolation
(D6) are the same whichever library cut the text.

``native``
    The hand-written overlapping word window this package always used (the default, byte-identical
    to every knowledge base ingested before this seam existed).
``llamaindex``
    LlamaIndex's ``SentenceSplitter`` (``llama-index-core``, the ``rag-llamaindex`` extra). It packs
    whole sentences into a chunk instead of cutting mid-sentence. It is configured with a word
    tokenizer and a regex sentence splitter so ``chunk_size``/``chunk_overlap`` mean the same unit
    (words) as ``native`` and **no tokenizer or NLTK data is downloaded at runtime** — an HPC login
    node or an air-gapped site can use it.
``auto``
    ``llamaindex`` when the library imports, ``native`` otherwise.

Selection: an explicit argument, else ``EXAMLOPS_RAG_FRAMEWORK``, else ``native``. An *explicit*
request for a framework that is not installed fails with :class:`RagFrameworkUnavailable` rather
than silently chunking differently — two ingests of one corpus must not produce different chunk
ids because one host lacked a wheel. Only ``auto`` degrades.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass

FRAMEWORKS = ("native", "llamaindex", "auto")
_ENV = "EXAMLOPS_RAG_FRAMEWORK"
#: Split *after* the whitespace that ends a sentence, so every piece keeps its trailing space and
#: LlamaIndex re-joins sentences as ``"a. b."`` rather than ``"a.b."``.
_SENTENCE = re.compile(r"(?<=[.!?]\s)")


class RagFrameworkUnavailable(RuntimeError):
    """An explicitly requested RAG framework is not installed."""


@dataclass(frozen=True)
class Chunker:
    """A named text splitter: ``split(text, size, overlap) -> chunks`` (size/overlap in words)."""

    name: str
    split: Callable[[str, int, int], list[str]]


def native_split(text: str, size: int = 40, overlap: int = 10) -> list[str]:
    """Split text into overlapping word-window chunks (the original ``chunk_text``)."""
    words = text.split()
    if not words:
        return []
    if len(words) <= size:
        return [" ".join(words)]
    step = max(1, size - overlap)
    return [
        " ".join(words[i : i + size]) for i in range(0, len(words), step) if words[i : i + size]
    ]


def _words(text: str) -> list[str]:
    return text.split()


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.split(text) if s]


def _llamaindex_split(text: str, size: int = 40, overlap: int = 10) -> list[str]:
    from llama_index.core.node_parser import SentenceSplitter

    if not text.strip():
        return []
    size = max(1, int(size))
    # LlamaIndex refuses overlap > size; native clamps its step instead. Clamp the same way so a
    # configuration that works on one framework never raises on the other.
    overlap = max(0, min(int(overlap), size - 1))
    splitter = SentenceSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        tokenizer=_words,
        chunking_tokenizer_fn=_sentences,
    )
    return [c for c in splitter.split_text(text) if c.strip()]


def llamaindex_available() -> bool:
    try:
        import llama_index.core.node_parser  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "not usable here"
        return False
    return True


def resolve_framework(name: str | None = None) -> str:
    """The concrete framework (``native`` | ``llamaindex``) a request resolves to."""
    chosen = (name or os.getenv(_ENV) or "native").strip().lower()
    if chosen not in FRAMEWORKS:
        raise ValueError(f"RAG framework {chosen!r} not in {FRAMEWORKS}")
    if chosen == "auto":
        return "llamaindex" if llamaindex_available() else "native"
    if chosen == "llamaindex" and not llamaindex_available():
        raise RagFrameworkUnavailable(
            "RAG framework 'llamaindex' requested but llama-index-core is not installed — "
            "pip install 'examlops[rag-llamaindex]', or use --framework native|auto"
        )
    return chosen


def get_chunker(name: str | None = None) -> Chunker:
    """Resolve ``name`` (see :func:`resolve_framework`) to a :class:`Chunker`."""
    resolved = resolve_framework(name)
    if resolved == "llamaindex":
        return Chunker("llamaindex", _llamaindex_split)
    return Chunker("native", native_split)


__all__ = [
    "FRAMEWORKS",
    "Chunker",
    "RagFrameworkUnavailable",
    "get_chunker",
    "llamaindex_available",
    "native_split",
    "resolve_framework",
]
