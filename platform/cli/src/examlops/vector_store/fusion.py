"""Rank fusion for hybrid dense+sparse search (ADR 0020 clause 2).

A cosine similarity and a BM25 score are not on the same scale: cosine lives in [-1, 1], BM25 is
unbounded and grows with query length. Adding them raw lets whichever happens to be larger decide.
Two principled ways out, both offered:

- **Reciprocal Rank Fusion** (Cormack, Clarke & Büttcher, SIGIR 2009) — the default. Uses ranks
  only, so the scale problem disappears:

      RRF(d) = Σ_{channels c}  1 / (k + rank_c(d))      (rank from 1; absent ⇒ no term)

  ``k = 60`` is the constant from the paper and damps the influence of the very top ranks. It
  needs no tuning and no labelled data, which is why it is the default.

- **Convex combination** of min-max-normalised scores:

      score(d) = α · dense_n(d) + (1 − α) · sparse_n(d)

  Bruch, Gai & Ingber ("An Analysis of Fusion Functions for Hybrid Retrieval", ACM TOIS 42(1),
  2023) found a *tuned* convex combination beats RRF in and out of domain and needs only a small
  labelled sample to tune α — but an untuned α is a guess. Use it when you have relevance
  judgements to pick α from (``exa eval`` can produce them); otherwise keep RRF.

Both are deterministic: ties break by document id, so the same inputs always give the same order —
a property the evaluation gate depends on (a ranking that reshuffles between runs makes every
regression test flaky).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

RRF_K = 60
FUSIONS = ("rrf", "convex")


def rrf(rankings: Mapping[str, Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion of several ranked id lists (best first)."""
    if k < 1:
        raise ValueError(f"rrf k must be >= 1, got {k}")
    fused: dict[str, float] = {}
    for ranked in rankings.values():
        for rank, doc_id in enumerate(ranked, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused


def _minmax(scores: Mapping[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    if hi == lo:
        # One candidate, or all tied: every candidate is equally the best this channel has.
        # Mapping them to 1.0 (not 0.0) keeps a lone lexical match from contributing nothing.
        return {d: 1.0 for d in scores}
    return {d: (s - lo) / (hi - lo) for d, s in scores.items()}


def convex(
    dense: Mapping[str, float], sparse: Mapping[str, float], alpha: float = 0.5
) -> dict[str, float]:
    """``alpha``·dense + (1−``alpha``)·sparse over min-max-normalised channel scores.

    A document missing from a channel contributes 0 from it — the lowest normalised score, which
    is what "not retrieved by this channel" means.
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must be in [0, 1], got {alpha}")
    dn, sn = _minmax(dense), _minmax(sparse)
    return {d: alpha * dn.get(d, 0.0) + (1.0 - alpha) * sn.get(d, 0.0) for d in set(dn) | set(sn)}


def ranked(scores: Mapping[str, float]) -> list[str]:
    """Ids ordered by score descending, ties broken by id ascending (deterministic)."""
    return sorted(scores, key=lambda d: (-scores[d], d))


def fuse(
    dense: Mapping[str, float],
    sparse: Mapping[str, float],
    *,
    method: str = "rrf",
    alpha: float = 0.5,
    rrf_k: int = RRF_K,
) -> dict[str, float]:
    """Fuse a dense and a sparse channel (``{id: score}``, higher is better) by ``method``."""
    if method == "rrf":
        return rrf({"dense": ranked(dense), "sparse": ranked(sparse)}, k=rrf_k)
    if method == "convex":
        return convex(dense, sparse, alpha)
    raise ValueError(f"fusion '{method}' not in {FUSIONS}")
