"""A7 — fidelity + privacy release gate (ADR 0042, spec R3).

A synthetic dataset is releasable only when it clears **both** a fidelity floor and a
privacy floor (spec R3). The gate fails **closed**: any error computing the metrics blocks
release rather than letting an unvetted dataset through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .metrics import fidelity_metrics, privacy_metrics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateThresholds:
    """Configurable release floors (spec R3). Both must be met to release."""

    min_fidelity: float = 0.6
    min_privacy: float = 0.5


def evaluate_and_gate(
    real: pd.DataFrame,
    synth: pd.DataFrame,
    thresholds: GateThresholds | None = None,
) -> dict:
    """Compute fidelity + privacy and decide releasability (spec R2/R3, GWT-2/GWT-3).

    Returns ``{"fidelity", "privacy", "released", "reasons"}``. ``released`` is True only
    when fidelity ≥ ``min_fidelity`` **and** privacy ≥ ``min_privacy``. On any metric
    error the dataset is blocked (fail-closed) with an explanatory reason.
    """
    th = thresholds or GateThresholds()
    try:
        fidelity = fidelity_metrics(real, synth)
        privacy = privacy_metrics(real, synth)
    except Exception as exc:  # fail closed — never release an unvetted dataset
        logger.warning("synthetic evaluation failed; blocking release: %s", exc)
        return {
            "fidelity": {"score": 0.0},
            "privacy": {"score": 0.0},
            "released": False,
            "reasons": [f"evaluation error: {exc}"],
        }

    reasons: list[str] = []
    if fidelity["score"] < th.min_fidelity:
        reasons.append(f"fidelity {fidelity['score']:.3f} < min_fidelity {th.min_fidelity:.3f}")
    if privacy["score"] < th.min_privacy:
        reasons.append(
            f"privacy {privacy['score']:.3f} < min_privacy {th.min_privacy:.3f} "
            f"(possible memorisation)"
        )
    return {
        "fidelity": fidelity,
        "privacy": privacy,
        "released": not reasons,
        "reasons": reasons,
    }
