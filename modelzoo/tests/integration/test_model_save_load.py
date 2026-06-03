"""
Integration tests: model save and load round-trip.

Auto-discovers all models via MODEL_CLASSES. For each model, uses
get_train_config() to find the right dataset class and params, then loads
data from the local fixture file in tests/fixtures/sample_data/.

No model or dataset names are hardcoded. Adding a new model to models/tasks/
automatically adds it to this test.
"""

import pytest

from tests.conftest import MODEL_CLASSES
from tests.integration.conftest import load_first_fixture_entry


@pytest.mark.parametrize("model_name,model_cls", list(MODEL_CLASSES.items()), ids=list(MODEL_CLASSES.keys()))
def test_model_save_load_roundtrip(model_name, model_cls, integration_data_dir, temp_model_dir):
    """
    Train → save → load → predict for each auto-discovered model.

    PASS: save() returns True, loaded model is_trained, predictions before and
          after save/load are identical.
    SKIP: no fixture file found — run `make sample-data`.
    FAIL: save() fails, load() returns None, or predictions differ after reload.
    """
    pytest.importorskip("joblib")

    model_instance, dataset, loader = load_first_fixture_entry(
        model_name, model_cls, integration_data_dir
    )

    model_instance.train(train_data_loader=loader)
    pred_before = model_instance.predict(loader)

    save_path = temp_model_dir / model_name
    save_path.mkdir(parents=True, exist_ok=True)
    saved = model_instance.save(path=str(save_path / "model"))
    assert saved is True, f"{model_name}: save() must return True"

    loaded = type(model_instance).load(path=str(save_path / "model"))
    assert loaded is not None, f"{model_name}: load() returned None"
    assert loaded.is_trained is True, f"{model_name}: loaded model must be trained"

    pred_after = loaded.predict(loader)

    assert len(pred_before) == len(pred_after), (
        f"{model_name}: prediction length changed after save/load "
        f"({len(pred_before)} → {len(pred_after)})"
    )
    for i, (a, b) in enumerate(zip(pred_before, pred_after, strict=True)):
        assert a == b, (
            f"{model_name}: prediction[{i}] changed after save/load: {a} → {b}"
        )
