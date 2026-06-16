"""SyntheticAnomalyDataset — fully in-code synthetic anomaly-detection data.

Unlike :class:`FDataDataset` / :class:`PM100Dataset`, this dataset downloads
nothing. It generates a deterministic mixture of *normal* and *anomalous*
384-dimensional job embeddings on the fly, so the whole ExaMLOps demo
(train -> MLflow -> Ray Serve -> SeanerBUS inference -> retrain) runs offline
and reproducibly.

Layout per sample::

    (embedding: np.ndarray[float64] shape (embedding_dim,),  label: int in {0, 1})

where ``label == 1`` marks an anomaly. Normal points are drawn from an
isotropic Gaussian ``N(0, 1)``; anomalies are shifted, scaled outliers so a
distance-/density-based detector (e.g. IsolationForest) separates them.

The ``seed`` is offset per ``split`` so train / validation / test draw
independent-but-reproducible samples. ``is_dummy=True`` shrinks the sample
counts for fast CI.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Tuple

import numpy as np
from pydantic import Field

from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset

# Per-split seed offsets keep train / validation / test disjoint yet deterministic.
_SPLIT_OFFSET = {"train": 0, "validation": 1000, "test": 2000}


class SyntheticAnomalyDataset(SeanergysDataset):
    """Self-contained synthetic dataset for binary anomaly detection."""

    embedding_dim: int = Field(default=384, description="Dimensionality of each embedding")
    n_normal: int = Field(default=400, description="Number of normal samples")
    n_anomaly: int = Field(default=100, description="Number of anomalous samples")
    anomaly_shift: float = Field(default=4.0, description="Mean shift applied to anomalies")
    anomaly_scale: float = Field(default=2.5, description="Std-dev multiplier for anomalies")
    seed: int = Field(default=42, description="Base RNG seed (offset per split)")
    split: str = Field(default="train", description="train | validation | test")

    # Pydantic v2 private attributes — populated in validate_model().
    _X: Optional[np.ndarray] = None
    _y: Optional[np.ndarray] = None

    def validate_model(self) -> "SyntheticAnomalyDataset":
        """Generate the synthetic mixture once, at construction time."""
        t0 = time.time()
        n_normal, n_anomaly = self.n_normal, self.n_anomaly
        if self.is_dummy:
            # Tiny, fast split for CI / --dummy runs (still both classes present).
            n_normal, n_anomaly = 24, 6

        rng = np.random.default_rng(self.seed + _SPLIT_OFFSET.get(self.split, 0))

        normal = rng.normal(0.0, 1.0, size=(n_normal, self.embedding_dim))
        # Anomalies: shifted + inflated-variance cluster in a random direction.
        direction = rng.normal(0.0, 1.0, size=(self.embedding_dim,))
        direction /= np.linalg.norm(direction) + 1e-12
        anomalies = (
            rng.normal(0.0, self.anomaly_scale, size=(n_anomaly, self.embedding_dim))
            + self.anomaly_shift * direction
        )

        X = np.vstack([normal, anomalies]).astype(np.float64)
        y = np.concatenate([np.zeros(n_normal, dtype=int), np.ones(n_anomaly, dtype=int)])

        # Shuffle so batches mix both classes.
        perm = rng.permutation(X.shape[0])
        self._X = X[perm]
        self._y = y[perm]

        self.stats.update(
            {
                "n_samples": int(self._X.shape[0]),
                "n_features": int(self.embedding_dim),
                "n_anomaly": int(n_anomaly),
                "n_normal": int(n_normal),
                "load_time": time.time() - t0,
            }
        )
        if self.metadata is None:
            self.metadata = {}
        self.metadata.update(
            {"dataset_name": "SyntheticAnomalyDataset", "split": self.split, "synthetic": True}
        )
        return self

    def __len__(self) -> int:
        return 0 if self._X is None else int(self._X.shape[0])

    def __getitem__(self, idx: int) -> Tuple[Any, Any]:
        if self._X is None:
            raise RuntimeError("Dataset not generated; validate_model() did not run.")
        if idx >= len(self):
            raise IndexError(f"Index {idx} out of range for dataset of size {len(self)}")

        input_data: Any = self._X[idx]
        output_data: Any = int(self._y[idx])

        if self.transform is not None:
            input_data = self.transform(input_data)
        if self.target_transform is not None:
            output_data = self.target_transform(output_data)

        return input_data, output_data
