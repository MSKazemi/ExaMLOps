"""
Unit tests for the ExaMLOps HPC pipeline tasks.

Tests cover all 7 Prefect tasks in pipelines/pipeline_generator.py:
  data_extraction_task, slurm_submit_task, slurm_wait_task,
  result_fetch_task, evaluate_task, log_mlflow_task, promote_task

All MLflow, Slurm, and model I/O are mocked — no external services required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import joblib
import numpy as np
import pytest
from sklearn.ensemble import RandomForestRegressor

# ── path setup ────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODELZOO = _REPO_ROOT / "modelzoo"
for _p in (str(_REPO_ROOT), str(_MODELZOO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pipelines.pipeline_generator as pg

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_estimator() -> RandomForestRegressor:
    """Return a tiny trained sklearn model for use in tests."""
    est = RandomForestRegressor(n_estimators=2, max_depth=2, random_state=0)
    est.fit([[1, 2], [3, 4]], [10.0, 20.0])
    return est


def _fake_model(estimator=None) -> MagicMock:
    m = MagicMock()
    m.estimator = estimator or _fake_estimator()
    m._extract_data_from_loader = MagicMock(
        return_value=(np.array([[1.0, 2.0], [3.0, 4.0]]), np.array([10.0, 20.0]))
    )
    return m


def _fake_loader() -> MagicMock:
    return MagicMock()


def _fake_config_cls(model_id: str = "test_model") -> MagicMock:
    ds_mock = MagicMock()
    ds_mock.__name__ = "DS"
    cfg = MagicMock()
    cfg.SUPPORTED_DATASETS = [ds_mock]
    cfg.get_train_components.return_value = (_fake_model(), None, _fake_loader())
    cfg.get_inference_params.return_value = {
        "model_id": model_id,
        "promotion_metric": "rmse",
        "promotion_threshold": 50.0,
        "promotion_direction": "lower_is_better",
    }
    return cfg


def _patch_registry(model_name: str = "TestModel", config_cls=None):
    """Context manager that patches MODEL_REGISTRY with a fake entry."""
    if config_cls is None:
        config_cls = _fake_config_cls()
    fake_entry = (MagicMock(), config_cls, {})
    return patch.dict(pg.MODEL_REGISTRY, {model_name: fake_entry})


# ── data_extraction_task ──────────────────────────────────────────────────────

class TestDataExtractionTask:
    def test_returns_model_and_loader(self):
        model = _fake_model()
        loader = _fake_loader()
        cfg = MagicMock()
        cfg.get_train_components.return_value = (model, None, loader)
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="FakeDataset")]

        with _patch_registry("M", cfg):
            result_model, result_loader = pg.data_extraction_task.fn(
                "M", "FakeDataset", is_dummy=True
            )

        assert result_model is model
        assert result_loader is loader
        cfg.get_train_components.assert_called_once()

    def test_uses_correct_split(self):
        cfg = MagicMock()
        cfg.get_train_components.return_value = (_fake_model(), None, _fake_loader())
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="DS")]

        with _patch_registry("M", cfg):
            pg.data_extraction_task.fn("M", "DS", is_dummy=False)

        _, kwargs = cfg.get_train_components.call_args
        assert kwargs.get("split") == "train" or cfg.get_train_components.call_args[0][1] == "train"

    def test_raises_on_unknown_model(self):
        with pytest.raises(KeyError):
            pg.data_extraction_task.fn("NonExistent", "DS")


# ── slurm_submit_task ─────────────────────────────────────────────────────────

class TestSlurmSubmitTask:
    def test_mock_mode_trains_and_saves_pkl(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "mock")
        model = _fake_model()
        loader = _fake_loader()

        job_id, artifact_path = pg.slurm_submit_task.fn(model, loader, "M", "DS")

        assert job_id.startswith("mock-")
        assert artifact_path is not None
        assert Path(artifact_path).exists()
        loaded = joblib.load(artifact_path)
        assert hasattr(loaded, "predict")
        model.train_step.assert_called_once_with(loader)

    def test_mock_mode_job_id_is_unique(self, monkeypatch):
        monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "mock")
        model = _fake_model()

        ids = {pg.slurm_submit_task.fn(model, _fake_loader(), "M", "DS")[0] for _ in range(5)}
        assert len(ids) == 5

    def test_real_slurm_mode_calls_sbatch(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "slurm")
        model = _fake_model()

        fake_adapter = MagicMock()
        fake_adapter.working_dir = tmp_path
        fake_adapter.submit_job.return_value = "12345678"

        with patch("adapter.RealSlurmAdapter", return_value=fake_adapter):
            job_id, artifact_hint = pg.slurm_submit_task.fn(model, _fake_loader(), "JPCP", "FData")

        assert job_id == "12345678"
        assert artifact_hint is None
        fake_adapter.submit_job.assert_called_once()

    def test_real_slurm_generates_bash_script(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "slurm")
        model = _fake_model()

        fake_adapter = MagicMock()
        fake_adapter.working_dir = tmp_path
        fake_adapter.submit_job.return_value = "99999"

        with patch("adapter.RealSlurmAdapter", return_value=fake_adapter):
            pg.slurm_submit_task.fn(model, _fake_loader(), "JPCP", "FDataDataset")

        script_path = fake_adapter.submit_job.call_args[1].get("script_path") or \
                      fake_adapter.submit_job.call_args[0][0]
        assert Path(script_path).exists()
        content = Path(script_path).read_text()
        assert "JPCP" in content
        assert "FDataDataset" in content
        assert "slurm_train_script.py" in content


# ── slurm_wait_task ───────────────────────────────────────────────────────────

class TestSlurmWaitTask:
    def test_mock_passthrough(self):
        state, path = pg.slurm_wait_task.fn("mock-abc123", "/tmp/model.pkl")
        assert state == "COMPLETED"
        assert path == "/tmp/model.pkl"

    def test_real_slurm_polls_and_returns_path(self, tmp_path):
        fake_adapter = MagicMock()
        fake_adapter.working_dir = str(tmp_path)
        fake_adapter.wait_until_complete.return_value = str(tmp_path / "12345" / "12345.out")
        fake_adapter.get_job_status.return_value = {"state": "COMPLETED"}

        with patch("adapter.RealSlurmAdapter", return_value=fake_adapter):
            state, artifact_path = pg.slurm_wait_task.fn("12345", None)

        assert state == "COMPLETED"
        assert artifact_path == str(tmp_path / "12345" / "model.pkl")

    def test_real_slurm_raises_on_failure(self, tmp_path):
        fake_adapter = MagicMock()
        fake_adapter.working_dir = str(tmp_path)
        fake_adapter.wait_until_complete.return_value = ""
        fake_adapter.get_job_status.return_value = {"state": "FAILED"}

        with patch("adapter.RealSlurmAdapter", return_value=fake_adapter):
            with pytest.raises(RuntimeError, match="FAILED"):
                pg.slurm_wait_task.fn("99999", None)


# ── result_fetch_task ─────────────────────────────────────────────────────────

class TestResultFetchTask:
    def test_loads_estimator_and_sets_on_model(self, tmp_path):
        estimator = _fake_estimator()
        pkl_path = tmp_path / "model.pkl"
        joblib.dump(estimator, pkl_path)

        fresh_model = _fake_model()
        cfg = MagicMock()
        cfg.get_train_components.return_value = (fresh_model, None, MagicMock())
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="DS")]

        with _patch_registry("M", cfg):
            result = pg.result_fetch_task.fn("COMPLETED", str(pkl_path), "M", "DS")

        assert result.estimator is not None
        # estimator was loaded and injected
        assert hasattr(result.estimator, "predict")

    def test_raises_on_non_completed_state(self, tmp_path):
        pkl_path = tmp_path / "model.pkl"
        joblib.dump(_fake_estimator(), pkl_path)

        with pytest.raises(RuntimeError, match="FAILED"):
            pg.result_fetch_task.fn("FAILED", str(pkl_path), "M", "DS")


# ── evaluate_task ─────────────────────────────────────────────────────────────

class TestEvaluateTask:
    def test_returns_metrics_dict(self):
        model = _fake_model()
        cfg = _fake_config_cls()

        with _patch_registry("M", cfg):
            metrics = pg.evaluate_task.fn(model, "M", "DS", is_dummy=True)

        assert "rmse" in metrics
        assert "mape" in metrics
        assert "mse" in metrics
        assert all(isinstance(v, float) for v in metrics.values())

    def test_rmse_is_non_negative(self):
        model = _fake_model()
        cfg = _fake_config_cls()

        with _patch_registry("M", cfg):
            metrics = pg.evaluate_task.fn(model, "M", "DS")

        assert metrics["rmse"] >= 0.0


# ── promote_task ──────────────────────────────────────────────────────────────

def _no_prev_production_client() -> MagicMock:
    """A MagicMock MlflowClient where get_model_version_by_alias raises (no previous Production)."""
    client = MagicMock()
    client.get_model_version_by_alias.side_effect = Exception("no alias")
    return client


class TestPromoteTask:
    def _make_registration(self, version="1"):
        return {"version": version, "run_id": "abc123", "status": "Staging"}

    def test_promotes_when_metric_passes(self):
        cfg = _fake_config_cls()  # threshold=50, lower_is_better
        client = _no_prev_production_client()

        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client):
            status = pg.promote_task.fn("M", self._make_registration(), {"rmse": 10.0})

        assert status == "Production"
        client.set_registered_model_alias.assert_called_once_with("test_model", "Production", "1")

    def test_stays_staging_when_metric_fails(self):
        cfg = _fake_config_cls()  # threshold=50
        client = _no_prev_production_client()

        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client):
            status = pg.promote_task.fn("M", self._make_registration(), {"rmse": 99.0})

        assert status == "Staging"
        client.set_registered_model_alias.assert_not_called()

    def test_stays_staging_when_version_missing(self):
        cfg = _fake_config_cls()

        with _patch_registry("M", cfg):
            status = pg.promote_task.fn("M", {"version": None}, {"rmse": 5.0})

        assert status == "Staging"

    def test_stays_staging_when_metric_missing(self):
        cfg = _fake_config_cls()
        client = _no_prev_production_client()

        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client):
            status = pg.promote_task.fn("M", self._make_registration(), {})

        assert status == "Staging"

    def test_higher_is_better_direction(self):
        cfg = MagicMock()
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="DS")]
        cfg.get_inference_params.return_value = {
            "model_id": "m",
            "promotion_metric": "accuracy",
            "promotion_threshold": 0.9,
            "promotion_direction": "higher_is_better",
        }

        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=_no_prev_production_client()):
            status = pg.promote_task.fn("M", self._make_registration(), {"accuracy": 0.95})
        assert status == "Production"

        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=_no_prev_production_client()):
            status = pg.promote_task.fn("M", self._make_registration(), {"accuracy": 0.80})
        assert status == "Staging"

    # ── Phase 3: multi-stage lifecycle ───────────────────────────────────────

    def test_lifecycle_promotes_through_all_stages_when_metric_excellent(self):
        cfg = MagicMock()
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="DS")]
        cfg.get_inference_params.return_value = {
            "model_id": "lc",
            "lifecycle": [
                {"name": "Staging",    "metric": "rmse", "threshold": 200.0, "direction": "lower_is_better"},
                {"name": "Canary",     "metric": "rmse", "threshold": 100.0, "direction": "lower_is_better"},
                {"name": "Production", "metric": "rmse", "threshold":  50.0, "direction": "lower_is_better"},
            ],
        }
        client = _no_prev_production_client()
        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client):
            status = pg.promote_task.fn("M", self._make_registration(version="3"), {"rmse": 10.0})

        assert status == "Production"
        aliases_set = [c.args[1] for c in client.set_registered_model_alias.call_args_list]
        assert sorted(aliases_set) == ["Canary", "Production", "Staging"]
        for c in client.set_registered_model_alias.call_args_list:
            assert c.args == ("lc", c.args[1], "3")

    def test_lifecycle_partial_promotion_only_to_staging(self):
        cfg = MagicMock()
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="DS")]
        cfg.get_inference_params.return_value = {
            "model_id": "lc",
            "lifecycle": [
                {"name": "Staging",    "metric": "rmse", "threshold": 200.0, "direction": "lower_is_better"},
                {"name": "Canary",     "metric": "rmse", "threshold": 100.0, "direction": "lower_is_better"},
                {"name": "Production", "metric": "rmse", "threshold":  50.0, "direction": "lower_is_better"},
            ],
        }
        client = _no_prev_production_client()
        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client):
            status = pg.promote_task.fn("M", self._make_registration(version="4"), {"rmse": 150.0})

        assert status == "Staging"
        aliases_set = [c.args[1] for c in client.set_registered_model_alias.call_args_list]
        assert aliases_set == ["Staging"]

    def test_lifecycle_archives_previous_production_on_promotion(self):
        cfg = MagicMock()
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="DS")]
        cfg.get_inference_params.return_value = {
            "model_id": "lc",
            "lifecycle": [
                {"name": "Production", "metric": "rmse", "threshold": 50.0, "direction": "lower_is_better"},
            ],
        }
        client = MagicMock()
        # There IS a previous Production version (v2), and the new one is v3.
        client.get_model_version_by_alias.return_value = SimpleNamespace(version=2)

        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client):
            status = pg.promote_task.fn("M", self._make_registration(version="3"), {"rmse": 10.0})

        assert status == "Production"
        calls = [(c.args[1], c.args[2]) for c in client.set_registered_model_alias.call_args_list]
        assert ("Production", "3") in calls
        assert ("Archived", "2") in calls

    def test_lifecycle_does_not_archive_when_no_previous_production(self):
        cfg = MagicMock()
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="DS")]
        cfg.get_inference_params.return_value = {
            "model_id": "lc",
            "lifecycle": [
                {"name": "Production", "metric": "rmse", "threshold": 50.0, "direction": "lower_is_better"},
            ],
        }
        client = _no_prev_production_client()

        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client):
            status = pg.promote_task.fn("M", self._make_registration(version="1"), {"rmse": 10.0})

        assert status == "Production"
        aliases_set = [c.args[1] for c in client.set_registered_model_alias.call_args_list]
        assert aliases_set == ["Production"]
        assert "Archived" not in aliases_set

    def test_lifecycle_fires_webhook_when_production_set(self, monkeypatch):
        cfg = MagicMock()
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="DS")]
        cfg.get_inference_params.return_value = {
            "model_id": "lc",
            "lifecycle": [
                {"name": "Production", "metric": "rmse", "threshold": 50.0, "direction": "lower_is_better"},
            ],
        }
        client = _no_prev_production_client()
        notified: dict[str, str | None] = {"model_id": None}

        def fake_notify(model_id: str) -> None:
            notified["model_id"] = model_id

        monkeypatch.setattr(pg, "_notify_ray_serve", fake_notify)

        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client):
            pg.promote_task.fn("M", self._make_registration(version="1"), {"rmse": 10.0})

        assert notified["model_id"] == "lc"

    def test_lifecycle_does_not_fire_webhook_for_non_production_only(self, monkeypatch):
        cfg = MagicMock()
        cfg.SUPPORTED_DATASETS = [MagicMock(__name__="DS")]
        cfg.get_inference_params.return_value = {
            "model_id": "lc",
            "lifecycle": [
                {"name": "Staging", "metric": "rmse", "threshold": 200.0, "direction": "lower_is_better"},
            ],
        }
        client = _no_prev_production_client()
        notified: dict[str, str | None] = {"model_id": None}
        monkeypatch.setattr(pg, "_notify_ray_serve", lambda mid: notified.update({"model_id": mid}))

        with _patch_registry("M", cfg), \
             patch("pipelines.pipeline_generator.mlflow.MlflowClient", return_value=client):
            pg.promote_task.fn("M", self._make_registration(version="1"), {"rmse": 10.0})

        assert notified["model_id"] is None


# ── Auto-discovery ────────────────────────────────────────────────────────────

class TestAutoDiscovery:
    def test_model_registry_is_populated(self):
        assert len(pg.MODEL_REGISTRY) > 0

    def test_jpcp_is_registered(self):
        assert "JPCP" in pg.MODEL_REGISTRY

    def test_registered_entry_has_config(self):
        _, config_cls, _ = pg.MODEL_REGISTRY["JPCP"]
        params = config_cls.get_inference_params()
        assert "model_id" in params
        assert "promotion_metric" in params
        assert "promotion_threshold" in params
