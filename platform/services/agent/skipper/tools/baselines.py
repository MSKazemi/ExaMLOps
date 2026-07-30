"""Baseline-recall tool (T3, Phase 6) — the agent's "what's normal?" reference.

Surfaces the recorded drift/cost/SLO baselines for a model so the agent can judge whether a live
value is anomalous. Read-only, and explicitly framed as *recorded normals* — the agent must still
confirm the *current* value with the live drift/cost tools (the memory-retrieval intent-gate exists
precisely so stored baselines never impersonate live state).
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from skipper import baselines


@tool
def recall_baseline(model: str) -> str:
    """Recall the recorded 'normal' baselines (drift/input/cost/SLO) for a model.

    Use this to judge whether a live reading is anomalous — but ALWAYS confirm the current value
    with the live drift/cost tools; these are recorded normals, not the present state.
    """
    normal = baselines.whats_normal(model)
    return json.dumps(normal, default=str, indent=2)


TOOLS = [recall_baseline]
