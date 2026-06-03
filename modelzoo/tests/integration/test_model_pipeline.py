"""
Integration tests: full pipeline per auto-discovered model.

For every concrete SeanergysModel subclass found in models/tasks/:
  1. test_get_train_config_structure — get_train_config() returns valid structure
  2. test_full_pipeline             — train → predict → save → load

Both tests are fully dynamic. No model or dataset names are hardcoded.
Adding a new model to models/tasks/ automatically adds it to both tests.

CI contract for model authors:
  - Implement get_train_config() returning Dict[str, Tuple[model, DatasetClass, (ds_params, dl_params)]]
  - Ensure the dataset supports is_dummy=True so CI does not need real data or network
  Tests skip with a clear message if these are not met.
"""

import os

import pytest

from tests.conftest import MODEL_CLASSES


# ---------------------------------------------------------------------------
# Test 1: get_train_config() returns a valid structure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model_cls", list(MODEL_CLASSES.values()))
def test_get_train_config_structure(model_cls):
    """
    get_train_config() must return a non-empty dict where each value is a tuple:
      (model_instance, DatasetClass, (SeanergysDatasetParams, SeanergysDataloaderParams))

    Also checks that the model instance has all required interface methods.
    """
    pytest.importorskip("sklearn")

    model_name = model_cls.__name__
    config = model_cls.get_train_config()

    assert isinstance(config, dict), (
        f"{model_name}.get_train_config() must return a dict"
    )
    assert len(config) > 0, (
        f"{model_name}.get_train_config() returned an empty dict"
    )

    for entry_name, entry in config.items():
        assert len(entry) == 3, (
            f"{model_name}/{entry_name}: expected (model_instance, DatasetClass, (ds_params, dl_params))"
        )
        model_instance, dataset_cls, params = entry
        assert len(params) == 2, (
            f"{model_name}/{entry_name}: third element must be (SeanergysDatasetParams, SeanergysDataloaderParams)"
        )

        for method in ("train", "predict", "save", "load"):
            assert hasattr(model_instance, method), (
                f"{model_name} is missing required method: {method}"
            )

        from seanergys_modelzoo.datasets.common.seanergys_dataset import SeanergysDataset
        assert issubclass(dataset_cls, SeanergysDataset), (
            f"{model_name}/{entry_name}: DatasetClass must subclass SeanergysDataset, "
            f"got {dataset_cls}"
        )


# ---------------------------------------------------------------------------
# Test 2: full pipeline — train → predict → save → load
# ---------------------------------------------------------------------------

@pytest.mark.zenodo
@pytest.mark.parametrize("model_cls", list(MODEL_CLASSES.values()))
def test_full_pipeline(model_cls, tmp_path):
    """
    Full pipeline for each auto-discovered model using its own get_train_config().

    Uses is_dummy=True on the dataset config. NOTE: Francesco's is_dummy=True still
    downloads from Zenodo with filters, so this test requires network access.
    It will become network-free once is_dummy=True generates data in memory.

    Skip with: CI_SKIP_ZENODO=1 make test-integration

    Skips if the dataset does not support is_dummy=True properly (len == 0 or load fails).
    This skip is a signal to the dataset author to implement is_dummy support.
    """
    if os.environ.get("CI_SKIP_ZENODO") == "1":
        pytest.skip("SKIP: CI_SKIP_ZENODO=1 — Zenodo tests disabled for this run.")

    pytest.importorskip("sklearn")
    pytest.importorskip("torch")

    from seanergys_modelzoo.dataloader.seanergys_dataloader import SeanergysDataloader

    model_name = model_cls.__name__
    config = model_cls.get_train_config()

    for entry_name, (model_instance, dataset_cls, (ds_params, dl_params)) in config.items():

        # Signal CI to use a small subset — dataset must honour this
        ds_params.is_dummy = True

        # Load dataset
        try:
            dataset = dataset_cls.from_config(ds_params)
            dataset.load_data()
        except Exception as e:
            pytest.skip(
                f"{model_name}/{entry_name}: dataset setup failed — {e}. "
                f"Ensure {dataset_cls.__name__} supports is_dummy=True without network access."
            )

        if len(dataset) == 0:
            pytest.skip(
                f"{model_name}/{entry_name}: dataset returned 0 samples with is_dummy=True. "
                f"Implement is_dummy support in {dataset_cls.__name__} to enable this test."
            )

        loader = SeanergysDataloader.from_config(
            data_loader_config=dl_params, dataset=dataset
        )

        # Train
        history = model_instance.train(loader)
        assert model_instance.is_trained, (
            f"{model_name}: is_trained must be True after train()"
        )
        assert history is not None, (
            f"{model_name}: train() must return a history dict, got None"
        )

        # Predict
        predictions = model_instance.predict(loader)
        assert predictions is not None, f"{model_name}: predict() returned None"
        assert len(predictions) > 0, f"{model_name}: predict() returned empty result"

        # Save
        save_path = tmp_path / model_name / entry_name
        save_path.mkdir(parents=True)
        saved = model_instance.save(str(save_path / "model"))
        assert saved, f"{model_name}: save() must return True"

        # Load
        loaded_model = type(model_instance).load(str(save_path / "model"))
        assert loaded_model is not None, f"{model_name}: load() returned None"
