"""Promotion gates composed from platform provenance (A7 R5/GWT-5, ADR 0042).

A model trained **only on synthetic data** must not be silently promoted to production
(spec R5): synthetic data is for augmentation/eval, and reported quality must rest on real
data. This module resolves a model's training dataset revisions from its A2 lineage and
tells whether they are synthetic-only, so both the manual promote path (`exa pipeline
promote`) and the self-driving autopilot can refuse — via an env gate or a D5 policy rule.

Pure composition over the data facades (`examlops.data.*`) — no ``platform_db`` coupling.
"""

from __future__ import annotations

import os


def training_dataset_revisions(model: str) -> list[str]:
    """Distinct A1 dataset revisions a model was trained on, from its A2 lineage.

    Empty when the model has no recorded lineage — callers treat "unknown" as *not*
    synthetic-only (fail-open: never block a promotion on missing provenance).
    """
    from examlops.data.events import lineage_graph

    graph = lineage_graph(model)
    revs = {r["dataset_revision"] for r in graph.get("runs", []) if r.get("dataset_revision")}
    return sorted(revs)


def synthetic_only_training(model: str) -> tuple[bool, list[str]]:
    """Return ``(is_synthetic_only, training_revisions)`` for a model (spec R5/GWT-5).

    ``is_synthetic_only`` is True only when the model has recorded training revisions and
    *every* one of them is flagged ``synthetic=true`` (A7). No lineage ⇒ ``(False, [])``.
    """
    from examlops.data.data_assets import is_synthetic_only

    revs = training_dataset_revisions(model)
    if not revs:
        return False, []
    return is_synthetic_only(revs), revs


def synthetic_only_gate_enabled() -> bool:
    """Whether the synthetic-only promotion gate is active (``EXAMLOPS_SYNTHETIC_ONLY_GATE``).

    Off by default (backward-compatible). Mirrors the C6 SLO / C8 fairness gate toggles.
    """
    return os.getenv("EXAMLOPS_SYNTHETIC_ONLY_GATE", "").lower() in ("1", "true", "yes", "on")
