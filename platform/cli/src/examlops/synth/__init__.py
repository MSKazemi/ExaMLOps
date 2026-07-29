"""A7 — Synthetic data generation (ADR 0042, spec ``A7-synthetic-data-generation``).

Generate synthetic datasets from a real A1 revision, gate them on fidelity + privacy, and
carry provenance so synthetic data can never pass as real (spec R1–R6). The heavy generator
(SDV) is an optional ``examlops[synth]`` extra; without it a pure-python Gaussian-copula
fallback keeps the whole feature — including the release gate — working offline.

Public interface (spec §4):

* :func:`synth_fit` — fit a generator to a real dataset.
* :func:`synth_generate` — sample a provenance-carrying :class:`SyntheticDataset`.
* :func:`synth_evaluate` — score fidelity + privacy and apply the release gate.

This module is **pure** (pandas/numpy only): it does no database or lineage I/O so it stays
free of the ``platform_db`` coupling and is trivially testable. The ``exa data synth`` CLI
layer wires the outputs into A1 revision recording, A2 lineage, and the audit log.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .gate import GateThresholds, evaluate_and_gate
from .generators import METHODS, Synthesizer, fit_synthesizer, generate
from .metrics import fidelity_metrics, privacy_metrics

__all__ = [
    "METHODS",
    "GateThresholds",
    "SyntheticDataset",
    "Synthesizer",
    "evaluate_and_gate",
    "fidelity_metrics",
    "privacy_metrics",
    "synth_evaluate",
    "synth_fit",
    "synth_generate",
]


@dataclass
class SyntheticDataset:
    """Generated synthetic records + the provenance needed to record them as an A1 revision.

    ``revision_id`` is a deterministic content hash (same generator config + seed + real
    data ⇒ same id), so the CLI can record it as an A1 revision flagged ``synthetic=true``
    with an A2 lineage edge to ``source_revision`` (spec R4).
    """

    data: pd.DataFrame
    method: str
    source_revision: str
    revision_id: str
    n_rows: int
    params: dict[str, Any] = field(default_factory=dict)


def _content_revision(df: pd.DataFrame, *, method: str, source_revision: str, seed: int) -> str:
    """Deterministic content revision id for a synthetic frame (A1-style content hash)."""
    h = hashlib.sha256()
    h.update(f"synthetic|{method}|{source_revision}|{seed}|".encode())
    # CSV serialisation is stable given fixed column order and renders list cells
    # (e.g. embeddings) deterministically.
    h.update(df.to_csv(index=False).encode())
    return h.hexdigest()


def synth_fit(
    source_revision: str,
    method: str = "gaussian_copula",
    *,
    data: pd.DataFrame,
    seed: int = 0,
) -> Synthesizer:
    """Fit a generator to a real dataset (spec R1).

    ``source_revision`` is the A1 revision id of the real data (recorded as provenance).
    ``data`` is the materialised real dataframe to fit — the pure core never does I/O; the
    CLI resolves a revision/path into a dataframe before calling this.
    """
    return fit_synthesizer(data, method, source_revision=source_revision, seed=seed)


def synth_generate(synth: Synthesizer, n: int, *, seed: int | None = None) -> SyntheticDataset:
    """Sample ``n`` synthetic records as a provenance-carrying dataset (spec R1, GWT-1)."""
    used_seed = synth.seed if seed is None else seed
    df = generate(synth, n, seed=used_seed)
    revision_id = _content_revision(
        df, method=synth.method, source_revision=synth.source_revision, seed=used_seed
    )
    return SyntheticDataset(
        data=df,
        method=synth.method,
        source_revision=synth.source_revision,
        revision_id=revision_id,
        n_rows=len(df),
        params={**synth.params, "seed": used_seed},
    )


def synth_evaluate(
    real: pd.DataFrame,
    synthetic: pd.DataFrame,
    *,
    thresholds: GateThresholds | None = None,
) -> dict:
    """Score fidelity + privacy and apply the release gate (spec R2/R3).

    Returns ``{"fidelity", "privacy", "released", "reasons"}``. The spec's revision-string
    signature (``synth_evaluate(real_rev, synthetic_rev)``) is realised at the CLI layer,
    which resolves each revision to a dataframe before calling this pure core.
    """
    return evaluate_and_gate(real, synthetic, thresholds)
