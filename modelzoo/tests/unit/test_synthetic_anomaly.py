"""Unit tests for SyntheticAnomalyDataset + DemoAnomaly (offline, no downloads)."""

from __future__ import annotations

import numpy as np
import pytest

from seanergys_modelzoo.datasets.synthetic_anomaly import SyntheticAnomalyDataset


# ── Dataset ────────────────────────────────────────────────────────────────


def test_dataset_shapes_and_length():
    ds = SyntheticAnomalyDataset(n_normal=40, n_anomaly=10, embedding_dim=384)
    assert len(ds) == 50
    x, y = ds[0]
    assert np.asarray(x).shape == (384,)
    assert int(y) in (0, 1)


def test_dataset_has_both_classes():
    ds = SyntheticAnomalyDataset(n_normal=40, n_anomaly=10)
    labels = {int(ds[i][1]) for i in range(len(ds))}
    assert labels == {0, 1}


def test_dummy_mode_is_small_but_balanced():
    ds = SyntheticAnomalyDataset(is_dummy=True)
    assert len(ds) == 30  # 24 normal + 6 anomaly
    labels = [int(ds[i][1]) for i in range(len(ds))]
    assert sum(labels) == 6  # six anomalies


def test_determinism_same_seed_same_data():
    a = SyntheticAnomalyDataset(n_normal=20, n_anomaly=5, seed=123)
    b = SyntheticAnomalyDataset(n_normal=20, n_anomaly=5, seed=123)
    assert np.allclose(
        np.vstack([a[i][0] for i in range(len(a))]), np.vstack([b[i][0] for i in range(len(b))])
    )


def test_splits_are_disjoint():
    train = SyntheticAnomalyDataset(n_normal=20, n_anomaly=5, split="train", seed=42)
    val = SyntheticAnomalyDataset(n_normal=20, n_anomaly=5, split="validation", seed=42)
    train_x = np.vstack([train[i][0] for i in range(len(train))])
    val_x = np.vstack([val[i][0] for i in range(len(val))])
    # Different per-split seed offset → different draws.
    assert not np.allclose(train_x, val_x)


def test_transform_is_applied():
    ds = SyntheticAnomalyDataset(
        n_normal=4, n_anomaly=1, transform=lambda x: np.zeros(3), target_transform=lambda y: 99
    )
    x, y = ds[0]
    assert np.asarray(x).shape == (3,)
    assert y == 99


# ── Model integration ────────────────────────────────────────────────────────


def test_demoanomaly_trains_and_separates_anomalies():
    pytest.importorskip("sklearn")
    from seanergys_modelzoo.dataloader.seanergys_dataloader import SeanergysDataloader
    from seanergys_modelzoo.models.tasks.anomaly_detection.demoanomaly.demoanomaly_model import (
        DemoAnomaly,
    )

    ds = SyntheticAnomalyDataset(n_normal=200, n_anomaly=50, seed=1)
    model = DemoAnomaly(model_hyperparameters={"contamination": 0.2, "random_state": 42})
    loader = SeanergysDataloader(ds, batch_size=1)

    model.train(loader)
    assert model.is_trained is True

    X, y = model._extract_data_from_loader(loader)
    preds = model.estimator.predict(X)
    # Binary {0,1} output and strong separation on the clean synthetic mixture.
    assert set(np.unique(preds)).issubset({0, 1})
    accuracy = float((preds == y).mean())
    assert accuracy >= 0.9, f"expected clean separation, got accuracy={accuracy}"


def test_model_version_constant_present():
    from seanergys_modelzoo.models.tasks.anomaly_detection.demoanomaly import demoanomaly_model

    assert demoanomaly_model.MODEL_VERSION.startswith("v")
