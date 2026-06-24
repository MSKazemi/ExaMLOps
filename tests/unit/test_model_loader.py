"""Tests for the per-model YAML loader."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if p not in sys.path:
        sys.path.insert(0, p)

MODELS_DIR = REPO_ROOT / "pipelines" / "models"

from pipelines.model_loader import load_model_yaml, scan_model_yamls  # noqa: E402


def test_load_jpcp_basic_fields():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    assert cfg.name == "JPCP"
    assert cfg.task_type == "regression"
    assert cfg.framework == "sklearn"
    assert cfg.enabled is True
    assert cfg.config_class == "jpcp_config.JPCPConfiguration"


def test_jpcp_has_two_datasets():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    assert len(cfg.datasets) == 2
    names = [d.name for d in cfg.datasets]
    assert "PM100Dataset" in names
    assert "FDataDataset" in names


def test_jpcp_dataset_lookup():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    pm100 = cfg.dataset("PM100Dataset")
    assert pm100.batch_size == 1
    assert "num_nodes_req_cat" in pm100.input_features
    assert pm100.backend == "zenodo"


def test_jpcp_pm100_train_filters():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    pm100 = cfg.dataset("PM100Dataset")
    train = pm100.splits["train"]
    assert len(train.filters) == 2
    assert train.filters[0][0] == "submit_time"
    assert train.filters[0][1] == ">="
    assert "2020-05-01" in train.filters[0][2]


def test_jpcp_fdata_train_has_files():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    fdata = cfg.dataset("FDataDataset")
    assert "23_12" in fdata.splits["train"].files
    assert "24_01" in fdata.splits["train"].files


def test_jpcp_fdata_validation_files():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    fdata = cfg.dataset("FDataDataset")
    assert fdata.splits["validation"].files == ["24_02"]


def test_jpcp_lifecycle_three_stages():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    assert len(cfg.lifecycle) == 3
    names = [s["name"] for s in cfg.lifecycle]
    assert names == ["Staging", "Canary", "Production"]
    prod = cfg.lifecycle[2]
    assert prod["metric"] == "rmse"
    assert prod["threshold"] == 50.0
    assert prod["direction"] == "lower_is_better"


def test_jpcp_serving_fields():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    assert cfg.serving["model_id"] == "jpcp"
    assert "Production" in cfg.serving["aliases"]


def test_jpcp_prefect_fields():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    assert cfg.prefect["schedule"] == "0 2 * * *"
    assert cfg.prefect["deployment_name"] == "examlops-jpcp-nightly"


def test_mack_single_dataset_classification():
    cfg = load_model_yaml(MODELS_DIR / "mack.yaml")
    assert cfg.task_type == "classification"
    assert len(cfg.datasets) == 1
    assert cfg.datasets[0].name == "FDataDataset"


def test_mack_model_extra_params():
    cfg = load_model_yaml(MODELS_DIR / "mack.yaml")
    assert cfg.model.get("embedding_type") == "SB"
    assert cfg.model.get("k_flops") == 2
    assert cfg.model.get("k_memory_bw") == 2
    assert cfg.model.get("hyperparameters") == {"n_jobs": -1}


def test_mcbound_embedding_none():
    cfg = load_model_yaml(MODELS_DIR / "mcbound.yaml")
    assert cfg.model.get("embedding_type") == "NONE"


def test_scan_finds_all_three():
    configs = scan_model_yamls(MODELS_DIR)
    names = [c.name for c in configs]
    assert "JPCP" in names
    assert "MACK" in names
    assert "MCBound" in names


def test_split_config_test_falls_back_to_validation():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    test_split = cfg.split_config("PM100Dataset", "test")
    val_split = cfg.split_config("PM100Dataset", "validation")
    assert test_split.filters == val_split.filters


def test_dataset_not_found_raises():
    cfg = load_model_yaml(MODELS_DIR / "jpcp.yaml")
    with pytest.raises(KeyError):
        cfg.dataset("NonExistentDataset")
