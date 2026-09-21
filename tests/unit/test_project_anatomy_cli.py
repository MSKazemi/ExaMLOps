"""ADR 0091/0093 — guards for the CLI surface and the storage-to-training binding.

The library half lives in ``test_project_anatomy.py``. This file pins what that one does not:
``exa project storage|pipelines|show`` render the anatomy, and ``log_mlflow_task`` routes a
project's run into its own MLflow experiment whose artifact_location is the project prefix.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))
sys.path.insert(0, str(ROOT / "pipelines"))

from examlops.cli.main import app  # noqa: E402

runner = CliRunner()

# `pipeline_generator` (imported only by the two tests below) reaches `seanergys_modelzoo`, an
# UPSTREAM library not vendored in the public tree (ADR 0094) — see test_pipeline.py's own guard.
# Only those two tests need it; skip precisely them rather than this whole file.
_MZ = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or (ROOT / "modelzoo"))
_NEEDS_MODELZOO = pytest.mark.skipif(
    not (_MZ / "seanergys_modelzoo").is_dir(),
    reason="seanergys_modelzoo not present — upstream library fetched at deploy/CI time",
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    from examlops import platform_db

    platform_db.init_db()
    platform_db.create_project("demo", storage_gb=100.0)
    platform_db.assign_resource_to_project("demo", "model", "JPCP")
    yield platform_db


def test_cli_storage_shows_default_location() -> None:
    res = runner.invoke(app, ["project", "storage", "demo"])
    assert res.exit_code == 0, res.output
    assert "examlops-projects" in res.output
    assert "demo/" in res.output


def test_cli_storage_refresh_is_fail_open(monkeypatch) -> None:
    monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", "http://127.0.0.1:1")
    res = runner.invoke(app, ["project", "storage", "demo", "--refresh"])
    assert res.exit_code == 0, res.output


def test_cli_storage_bind_missing_connection_fails() -> None:
    res = runner.invoke(app, ["project", "storage", "demo", "--bind-connection", "nope"])
    assert res.exit_code == 1


def test_cli_pipelines_and_show_render_anatomy() -> None:
    res = runner.invoke(app, ["project", "pipelines", "demo"])
    assert res.exit_code == 0, res.output
    assert "JPCP" in res.output
    res = runner.invoke(app, ["project", "show", "demo"])
    assert res.exit_code == 0, res.output
    assert "examlops-projects" in res.output


@_NEEDS_MODELZOO
def test_training_run_routes_into_project_experiment(monkeypatch) -> None:
    import pipeline_generator as pg

    class _Stop(Exception):
        pass

    mlf = MagicMock()
    mlf.get_experiment_by_name.return_value = None
    mlf.set_experiment.side_effect = _Stop
    monkeypatch.setattr(pg, "mlflow", mlf)
    cfg = MagicMock()
    cfg.get_inference_params.return_value = {"model_id": "jpcp"}
    monkeypatch.setitem(pg.MODEL_REGISTRY, "JPCP", (None, cfg, None))
    with pytest.raises(_Stop):
        pg.log_mlflow_task.fn(MagicMock(), {}, "JPCP", "PM100Dataset")
    mlf.create_experiment.assert_called_once_with(
        "project/demo", artifact_location="s3://examlops-projects/demo/artifacts"
    )
    mlf.set_experiment.assert_called_once_with("project/demo")


@_NEEDS_MODELZOO
def test_non_project_model_keeps_default_experiment(monkeypatch, _db) -> None:
    import pipeline_generator as pg

    class _Stop(Exception):
        pass

    mlf = MagicMock()
    mlf.set_experiment.side_effect = _Stop
    monkeypatch.setattr(pg, "mlflow", mlf)
    cfg = MagicMock()
    cfg.get_inference_params.return_value = {"model_id": "other"}
    monkeypatch.setitem(pg.MODEL_REGISTRY, "OTHER", (None, cfg, None))
    with pytest.raises(_Stop):
        pg.log_mlflow_task.fn(MagicMock(), {}, "OTHER", "PM100Dataset")
    mlf.create_experiment.assert_not_called()
    mlf.set_experiment.assert_called_once_with("other_pm100dataset")
