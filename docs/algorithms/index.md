# Algorithms

The methods behind the platform's decisions: how it retrieves, compares, measures, and decides
when to act. Each page gives the formulas, the parameters and their defaults, the guarantees the
code makes, and the tests that pin them down, with references to the literature they come from.

<div class="grid cards" markdown>

-   **Hybrid retrieval**

    ---

    BM25 and dense-vector search, fused by Reciprocal Rank Fusion or a convex combination, over
    HNSW / IVFFlat indexes. Finds exact identifiers an embedding blurs.

    [Read: hybrid retrieval →](hybrid-retrieval.md)

-   **Carbon-aware placement**

    ---

    A carbon policy is simulated against two simple baselines and a perfect-foresight oracle on a
    real intensity trace. It may place jobs on carbon only if it wins by a declared margin, and
    it is retired once it stops paying.

    [Read: carbon-aware placement →](carbon-aware-placement.md)

</div>

## Where the other methods are documented

These algorithms are described in the guide for the feature that uses them:

| Area | Method | Page |
|---|---|---|
| Evaluation | Cohen's κ agreement, Wilson score intervals, Rogan–Gladen correction for judge error | [Judge calibration](../guides/judge-calibration.md) |
| Experimentation | Welch's t-test on per-sample error (normal-approximation z-test fallback) for champion–challenger comparison | [Shadow & champion–challenger](../guides/shadow-champion-challenger.md) |
| Reliability | Multi-window burn-rate alerting on model-quality SLOs | [SLOs](../guides/slos.md) |
| Carbon | Typed carbon signals: average (accounting) vs marginal (decision) grid intensity | [Carbon signals](../guides/carbon-signals.md) |
| Monitoring | Concept drift by a one-sided mean-shift z-test, label-free performance estimation, data-quality checks | [Advanced drift](../guides/drift-advanced.md) |
| Monitoring | Telling corrupted inputs apart from real drift | [Corruption detection](../guides/corruption-detection.md) |
| Governance | SHA-256 hash chain over the audit log, with correlation and causation edges | [Evidence chain](../guides/evidence-chain.md) |
| Governance | Subgroup performance and fairness gates | [Fairness](../guides/fairness.md) |
