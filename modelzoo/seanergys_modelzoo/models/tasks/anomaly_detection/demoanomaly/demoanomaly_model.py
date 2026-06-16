"""DemoAnomaly — a self-contained demo anomaly-detection model.

A genuine (unsupervised) anomaly detector built on scikit-learn's
``IsolationForest``, wrapped so it plugs into the ExaMLOps classification
machinery unchanged: its ``predict`` returns **binary anomaly labels**
(``1`` = anomaly, ``0`` = normal) instead of IsolationForest's native
``{-1, +1}``. This matters because the pipeline's ``evaluate_task`` calls
``model.estimator.predict(...)`` *directly* and scores it with
``accuracy_score`` against ``{0, 1}`` ground-truth labels.

Trained on :class:`SyntheticAnomalyDataset` (no external data download), this
model drives the full demo end-to-end.

────────────────────────────────────────────────────────────────────────────
``MODEL_VERSION`` is the demo *staleness knob*. Bump it (``v1`` -> ``v2``) and
commit/push the modelzoo: the control-plane freshness tracker treats any
modelzoo commit as a reason to mark deployed models stale, after which a
retrain brings them current. The value is logged to MLflow on every training
run so ``exa models diff demoanomaly <v1> <v2>`` can show which source version
produced each registered model version.
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import numpy as np
from pydantic import model_validator
from sklearn.base import BaseEstimator
from sklearn.ensemble import IsolationForest

from seanergys_modelzoo.models.common.seanergys_model import SeanergysModelTask
from seanergys_modelzoo.models.common.seanergys_model_metadata import SeanergysModelMetadata
from seanergys_modelzoo.models.common.sklearn_seanergys_model import SeanergysSklearnModel

# ── Demo staleness knob ───────────────────────────────────────────────────────
# Change this and commit the modelzoo to make DemoAnomaly go "stale" downstream.
MODEL_VERSION = "v1"


class IsolationForestClassifier(BaseEstimator):
    """sklearn-compatible wrapper turning IsolationForest into a binary classifier.

    ``fit(X, y=None)`` trains unsupervised (``y`` is ignored — anomaly detection
    is unsupervised); ``predict(X)`` returns ``1`` for anomalies, ``0`` for
    normal points. ``score_samples`` / ``decision_function`` are forwarded so a
    continuous anomaly score is still available downstream.
    """

    def __init__(
        self,
        n_estimators: int = 100,
        contamination: float = 0.2,
        max_samples: str | int | float = "auto",
        random_state: int = 42,
        n_jobs: int = -1,
    ) -> None:
        self.n_estimators = n_estimators
        self.contamination = contamination
        self.max_samples = max_samples
        self.random_state = random_state
        self.n_jobs = n_jobs

    def fit(self, X: np.ndarray, y: Any = None) -> "IsolationForestClassifier":
        self._iforest = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            max_samples=self.max_samples,
            random_state=self.random_state,
            n_jobs=self.n_jobs,
        )
        self._iforest.fit(np.asarray(X, dtype=float))
        self.classes_ = np.array([0, 1])
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        # IsolationForest: +1 = inlier (normal), -1 = outlier (anomaly).
        raw = self._iforest.predict(np.asarray(X, dtype=float))
        return (raw == -1).astype(int)

    def score_samples(self, X: np.ndarray) -> np.ndarray:
        return self._iforest.score_samples(np.asarray(X, dtype=float))

    def decision_function(self, X: np.ndarray) -> np.ndarray:
        return self._iforest.decision_function(np.asarray(X, dtype=float))


class DemoAnomaly(SeanergysSklearnModel):
    """Demo HPC-job anomaly detector (IsolationForest, binary output).

    Example::

        model = DemoAnomaly(model_hyperparameters={"contamination": 0.2})
        model.train(loader)
        preds = model.predict(features)   # 1 = anomaly, 0 = normal
    """

    # Surface the source version on the instance so the pipeline can log it.
    model_version: str = MODEL_VERSION

    @model_validator(mode="before")
    @classmethod
    def _inject_defaults(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        values.setdefault("model_class", IsolationForestClassifier)
        values.setdefault("task_type", SeanergysModelTask.CLASSIFICATION)
        values.setdefault(
            "metadata",
            SeanergysModelMetadata(
                name="DemoAnomaly",
                created_at=datetime.now().isoformat(),
                task_type=SeanergysModelTask.CLASSIFICATION,
            ),
        )
        return values

    def embedding_parsing(self, embedding: np.ndarray) -> np.ndarray:
        """Coerce a raw embedding into a flat float vector of length 384.

        SyntheticAnomalyDataset already yields a 384-vector; this stays robust
        if a length-1 wrapper array is passed (the FData convention).
        """
        emb = np.asarray(embedding, dtype=object)
        if emb.shape == (1,):  # FData-style [[...384...]] wrapper
            emb = np.asarray(embedding[0])
        flat = np.asarray(emb, dtype=float).ravel()
        out = np.zeros((384,), dtype=float)
        out[: min(384, flat.shape[0])] = flat[:384]
        return out

    def anomaly_score(self, X: np.ndarray) -> np.ndarray:
        """Continuous anomaly score (lower = more anomalous)."""
        return self.estimator.score_samples(np.asarray(X, dtype=float))
