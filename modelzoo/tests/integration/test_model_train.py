"""
Integration tests: model training on fixture data.

Auto-discovers all models via MODEL_CLASSES. For each model, uses
get_train_config() to find the right dataset class and params, then loads
data from the local fixture file in tests/fixtures/sample_data/.

No model or dataset names are hardcoded. Adding a new model to models/tasks/
automatically adds it to both tests here.

Complements test_model_pipeline.py (which uses is_dummy=True for fully offline
testing). These tests use fixture files created by `make sample-data`.
"""

import pytest

from tests.conftest import MODEL_CLASSES
from tests.integration.conftest import load_first_fixture_entry


@pytest.mark.parametrize("model_name,model_cls", list(MODEL_CLASSES.items()), ids=list(MODEL_CLASSES.keys()))
def test_model_train_on_fixture_data(model_name, model_cls, integration_data_dir):
    """
    Train each auto-discovered model on its fixture dataset.

    PASS: train() completes, model.is_trained is True, history is returned.
    SKIP: no fixture file found — run `make sample-data`.
    FAIL: train() raises, or is_trained is still False after training.
    """
    model_instance, dataset, loader = load_first_fixture_entry(
        model_name, model_cls, integration_data_dir
    )

    history = model_instance.train(train_data_loader=loader)

    assert model_instance.is_trained is True, (
        f"{model_name}: is_trained must be True after train()"
    )
    assert history is not None, (
        f"{model_name}: train() must return a history dict, got None"
    )
    assert "training" in history, (
        f"{model_name}: history must contain a 'training' key"
    )
    assert len(dataset) >= 1, (
        f"{model_name}: dataset must have at least 1 sample"
    )


@pytest.mark.parametrize("model_name,model_cls", list(MODEL_CLASSES.items()), ids=list(MODEL_CLASSES.keys()))
def test_model_predict_after_train(model_name, model_cls, integration_data_dir):
    """
    Train then predict on each auto-discovered model.

    PASS: predict() returns a non-empty result with the same length as the dataset.
    SKIP: no fixture file found — run `make sample-data`.
    FAIL: predict() returns None or wrong length.
    """
    model_instance, dataset, loader = load_first_fixture_entry(
        model_name, model_cls, integration_data_dir
    )

    model_instance.train(train_data_loader=loader)
    predictions = model_instance.predict(loader)

    assert predictions is not None, (
        f"{model_name}: predict() returned None"
    )
    assert len(predictions) == len(dataset), (
        f"{model_name}: len(predictions)={len(predictions)} != len(dataset)={len(dataset)}"
    )
