"""``examlops.drift`` — prediction-drift status through the stable SDK (ADR 0078 clause 1).

A thin typed view over :mod:`examlops.drift_status`, the one drift computation shared with
``exa drift status``, ``exa drift trigger``, the autopilot and the control plane's evaluator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from examlops.sdk.errors import UnavailableError

__all__ = ["DriftStatus", "status"]


@dataclass(frozen=True)
class DriftStatus:
    """The drift verdict of one model over its most recent prediction window."""

    model: str
    status: str
    z_score: float
    live_mean: float
    live_std: float
    baseline_mean: float | None
    n_snapshots: int
    recent: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The historical ``exa --json drift status`` row (same keys, same order)."""
        return {
            "model": self.model,
            "live_mean": self.live_mean,
            "live_std": self.live_std,
            "baseline_mean": self.baseline_mean,
            "z_score": self.z_score,
            "status": self.status,
            "n_snapshots": self.n_snapshots,
            "recent": list(self.recent),
        }


def status(model: str | None = None) -> list[DriftStatus]:
    """Drift status of every model with predictions, or of ``model`` only.

    A model with no recorded predictions has no status and is absent — not reported as OK.
    """
    from examlops import drift_status

    try:
        rows = drift_status.model_rows(model)
    except Exception as exc:  # noqa: BLE001 - the datastore's own error types are private
        raise UnavailableError(f"drift status unavailable: {exc}") from exc
    return [
        DriftStatus(
            model=str(r["model"]),
            status=str(r["status"]),
            z_score=r["z_score"],
            live_mean=r["live_mean"],
            live_std=r["live_std"],
            baseline_mean=r.get("baseline_mean"),
            n_snapshots=int(r["n_snapshots"]),
            recent=list(r.get("recent") or []),
        )
        for r in rows
    ]
