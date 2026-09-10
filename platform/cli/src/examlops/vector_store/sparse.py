"""The sparse (lexical) retrieval channel: Okapi BM25 (ADR 0020 clause 2).

Dense embeddings capture meaning and miss exact tokens — an error code, a model name, a job id,
a rare acronym. A lexical channel catches exactly those. Hybrid search runs both and fuses the
rankings (:mod:`examlops.vector_store.fusion`).

The scorer is Okapi BM25 (Robertson & Zaragoza, "The Probabilistic Relevance Framework: BM25 and
Beyond", Foundations and Trends in IR 3(4), 2009) in the form Lucene has used since 6.0:

    score(D, Q) = Σ_{q ∈ Q}  idf(q) · tf(q,D)·(k1 + 1) / ( tf(q,D) + k1·(1 − b + b·|D|/avgdl) )
    idf(q)      = ln( 1 + (N − df(q) + 0.5) / (df(q) + 0.5) )

That IDF is the classic Robertson–Spärck Jones weight with a ``1 +`` inside the log, which keeps it
positive for a term that appears in more than half the corpus. The textbook form goes negative
there, and a negative weight would rank a document *lower* for containing a query term.

``k1 = 1.2`` and ``b = 0.75`` are the standard defaults (Manning, Raghavan & Schütze,
*Introduction to Information Retrieval*, 2008, §11.4.3).

Scores are computed over the candidate set the caller passes — for the SQLite store, the whole
(filtered) collection, which is what makes corpus statistics exact rather than estimated.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

_TOKEN = re.compile(r"\w+", re.UNICODE)

K1 = 1.2
B = 0.75


def tokenize(text: str | None) -> list[str]:
    """Lower-cased unicode word tokens. Deliberately no stemming or stop-word list.

    Stemming and stop words are language-specific, and the platform's corpora are mixed
    (English prose, Italian prose, and identifiers such as ``JPCP`` or ``PM100Dataset`` that a
    stemmer would mangle). The identifiers are the reason the lexical channel exists at all.
    """
    return _TOKEN.findall(text.lower()) if text else []


def bm25_scores(
    query: str,
    docs: Iterable[tuple[str, str | None]],
    *,
    k1: float = K1,
    b: float = B,
) -> dict[str, float]:
    """BM25 score of every document with at least one query term.

    ``docs`` is ``(doc_id, text)``. Documents without text still count toward ``N`` and
    ``avgdl`` (they are in the corpus) but can never match. The result omits zero scores: a
    document sharing no term with the query has no lexical evidence, and giving it a rank in the
    sparse list would let fusion reward it for nothing.
    """
    q_terms = set(tokenize(query))
    if not q_terms:
        return {}
    tfs: dict[str, Counter[str]] = {}
    lengths: dict[str, int] = {}
    df: Counter[str] = Counter()
    for doc_id, text in docs:
        toks = tokenize(text)
        lengths[doc_id] = len(toks)
        tf = Counter(t for t in toks if t in q_terms)
        if tf:
            tfs[doc_id] = tf
            df.update(tf.keys())
    n = len(lengths)
    if n == 0 or not tfs:
        return {}
    avgdl = (sum(lengths.values()) / n) or 1.0
    idf = {t: math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in df}
    scores: dict[str, float] = {}
    for doc_id, tf in tfs.items():
        norm = k1 * (1.0 - b + b * lengths[doc_id] / avgdl)
        scores[doc_id] = sum(idf[t] * f * (k1 + 1.0) / (f + norm) for t, f in tf.items())
    return scores


def tsquery_or(text: str | None) -> str:
    """An OR-ed ``to_tsquery`` string for the pgvector backend's lexical channel.

    ``websearch_to_tsquery``/``plainto_tsquery`` AND the terms together, which is right for a
    search box and wrong for a recall channel: a chunk that contains the rare identifier but not
    every other word of a long question would vanish. Tokens come from :func:`tokenize`, so they
    are ``\\w+`` only and cannot carry tsquery operators (``& | ! ( ) : *``).
    """
    return " | ".join(dict.fromkeys(tokenize(text)))
