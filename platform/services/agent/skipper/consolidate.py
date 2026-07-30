"""Memory consolidation / reflection (T5, Phase 7, ADR 0106).

An **offline** reflection pass (peer of ``memory_admin``), runnable on a schedule:

1. **Promote recurring episodes → a candidate procedure.** When a model accumulates
   ``AGENT_CONSOLIDATE_MIN_EPISODES`` incidents, a deterministic (no-LLM) candidate procedure is
   built from their most common resolution and **enqueued to the HITL review queue** — an operator
   approves before it becomes a live, recallable procedure (governance preserved, ADR 0034).
2. **Reinforce.** Deprecate procedures whose tools are chronically failing (see ``reinforce``).

Local-first: the summary is deterministic; an optional local-LLM polish can be added later but is
never required and never a paid API. Runs against the real long-term store, or any store passed in.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from skipper import config, memory_types, reinforce

log = logging.getLogger("skipper.consolidate")


def _summarize(model: str, episodes: list[Any]) -> tuple[list[str], str]:
    """Build deterministic procedure steps + success condition from recurring incidents."""
    resolutions = [
        (it.value.get("data", {}) or {}).get("resolution", "").strip() for it in episodes
    ]
    resolutions = [r for r in resolutions if r]
    common = Counter(resolutions).most_common(1)
    best = common[0][0] if common else "apply the previously successful remediation"
    steps = [
        f"Confirm the current state of {model} with the live drift/health tools.",
        f"Recall past incidents for {model} and compare against the recorded baseline.",
        f"Apply the remediation that resolved prior incidents: {best}.",
        "Verify recovery and record the outcome.",
    ]
    return steps, f"{model} incident resolved and metrics back within baseline"


def promote_recurring_episodes(
    store: Any, *, min_episodes: int | None = None
) -> list[dict[str, Any]]:
    """Enqueue a candidate procedure for each model with enough recurring incidents (review-gated)."""
    if store is None:
        return []
    min_episodes = config.AGENT_CONSOLIDATE_MIN_EPISODES if min_episodes is None else min_episodes

    by_model: dict[str, list[Any]] = {}
    for it in memory_types.list_kind(store, memory_types.KIND_EPISODE, limit=1000):
        data = it.value.get("data", {}) or {}
        model = data.get("model") or (it.namespace[1] if len(it.namespace) > 1 else "unknown")
        by_model.setdefault(model, []).append(it)

    from skipper import memory_review

    promoted: list[dict[str, Any]] = []
    for model, episodes in by_model.items():
        if len(episodes) < min_episodes:
            continue
        steps, success = _summarize(model, episodes)
        review_id = memory_review.enqueue(
            f"handle-{model}-incidents",
            steps,
            success_conditions=success,
            operator=config.AGENT_ACTOR,
        )
        promoted.append({"model": model, "review_id": review_id, "from_episodes": len(episodes)})
    return promoted


def consolidate(store: Any) -> dict[str, Any]:
    """Run one full offline reflection pass: promote recurring episodes + reinforce procedures."""
    promoted = promote_recurring_episodes(store)
    deprecated = reinforce.deprecate_failing_procedures(store)
    return {"promoted": promoted, "deprecated": deprecated}


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from skipper.memory import build_store

    argparse.ArgumentParser(prog="python -m skipper.consolidate").parse_args(argv)
    store = build_store()
    if store is None:
        print("long-term memory store unavailable (no embeddings) — nothing to consolidate")
        return 1
    result = consolidate(store)
    print(
        f"consolidate: promoted {len(result['promoted'])} candidate procedure(s) to review; "
        f"deprecated {len(result['deprecated'])} failing procedure(s)"
    )
    for p in result["promoted"]:
        print(f"  review #{p['review_id']}: {p['model']} (from {p['from_episodes']} incidents)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
