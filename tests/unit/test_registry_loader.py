# tests/unit/test_registry_loader.py
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml  # noqa: E402

from pipelines.registry_loader import (  # noqa: E402
    ModelEntry,
    export_registry,
    load_registry,
    resolve_entries,
)

BASE_YAML = textwrap.dedent("""
    version: "1"
    defaults:
      backend: zenodo
      dummy: false
      enabled: true
      serve_aliases: [Production, Canary, Staging]
    models:
      - name: JPCP
        model_class: JPCP
        config_class: JPCPConfiguration
        datasets: [PM100Dataset, FDataDataset]
        lifecycle:
          - name: Production
            metric: rmse
            threshold: 50.0
            direction: lower_is_better
        serve_aliases: [Production, Canary, Staging]
        prefect:
          schedule: "0 2 * * *"
          deployment_name: examlops-jpcp-nightly
          work_pool: default-agent
          concurrency_limit: 1
      - name: MACK
        model_class: MACK
        config_class: MACKConfiguration
        datasets: [FDataDataset]
        enabled: false
        lifecycle:
          - name: Production
            metric: accuracy
            threshold: 0.80
            direction: higher_is_better
        serve_aliases: [Production]
        prefect:
          schedule: "0 2 * * *"
          deployment_name: examlops-mack-nightly
          work_pool: default-agent
          concurrency_limit: 1
""")

ENV_YAML = textwrap.dedent("""
    defaults:
      backend: minio
      serve_aliases: [Production]
    models:
      - name: JPCP
        lifecycle:
          - name: Production
            metric: rmse
            threshold: 30.0
            direction: lower_is_better
      - name: MACK
        enabled: true
      - name: MACK_dataplane
        model_class: MACK
        config_class: MACKConfiguration
        datasets: [FDataDataset]
        lifecycle:
          - name: Production
            metric: accuracy
            threshold: 0.90
            direction: higher_is_better
        prefect:
          schedule: null
          deployment_name: examlops-mack-dataplane
""")


@pytest.fixture
def base_yaml(tmp_path) -> Path:
    p = tmp_path / "model_registry.yaml"
    p.write_text(BASE_YAML)
    return p


@pytest.fixture
def env_yaml(tmp_path) -> Path:
    p = tmp_path / "prod.yaml"
    p.write_text(ENV_YAML)
    return p


def test_load_base_returns_all_entries_including_disabled(base_yaml):
    entries = load_registry(base_yaml)
    assert len(entries) == 2
    by_name = {e.name: e for e in entries}
    assert by_name["JPCP"].enabled is True
    assert by_name["MACK"].enabled is False


def test_load_base_applies_defaults(base_yaml):
    entries = load_registry(base_yaml)
    jpcp = next(e for e in entries if e.name == "JPCP")
    assert jpcp.backend == "zenodo"
    assert jpcp.dummy is False
    assert jpcp.datasets == ["PM100Dataset", "FDataDataset"]
    assert jpcp.lifecycle[0]["threshold"] == 50.0
    assert jpcp.serve_aliases == ["Production", "Canary", "Staging"]
    assert jpcp.prefect["schedule"] == "0 2 * * *"


def test_load_with_env_overrides_lifecycle(base_yaml, env_yaml):
    entries = load_registry(base_yaml, env_yaml)
    jpcp = next(e for e in entries if e.name == "JPCP")
    assert jpcp.lifecycle[0]["threshold"] == 30.0


def test_load_with_env_overrides_defaults_backend(base_yaml, env_yaml):
    entries = load_registry(base_yaml, env_yaml)
    jpcp = next(e for e in entries if e.name == "JPCP")
    assert jpcp.backend == "minio"
    assert jpcp.serve_aliases == ["Production"]


def test_load_with_env_enables_disabled_model(base_yaml, env_yaml):
    entries = load_registry(base_yaml, env_yaml)
    mack = next(e for e in entries if e.name == "MACK")
    assert mack.enabled is True


def test_load_with_env_adds_additive_entry(base_yaml, env_yaml):
    entries = load_registry(base_yaml, env_yaml)
    names = {e.name for e in entries}
    assert "MACK_dataplane" in names
    dp = next(e for e in entries if e.name == "MACK_dataplane")
    assert dp.lifecycle[0]["threshold"] == 0.90


# ── export_registry tests ──────────────────────────────────────────────────


class FakeConfig:
    SUPPORTED_DATASETS: list = []
    MODEL_CLASS: type  # set below after FakeModel is defined

    @classmethod
    def get_inference_params(cls) -> dict:
        return {
            "lifecycle": [
                {
                    "name": "Staging",
                    "metric": "rmse",
                    "threshold": 200.0,
                    "direction": "lower_is_better",
                },
            ]
        }


class FakeModel:
    pass


FakeConfig.MODEL_CLASS = FakeModel

FAKE_REGISTRY: dict = {
    "FakeModel": (FakeModel, FakeConfig, {}),
}


@pytest.fixture
def registry_yaml(tmp_path) -> Path:
    p = tmp_path / "model_registry.yaml"
    p.write_text(BASE_YAML)
    return p


def test_export_registry_writes_valid_yaml(tmp_path):
    out = tmp_path / "model_registry.yaml"
    export_registry(FAKE_REGISTRY, out)
    data = yaml.safe_load(out.read_text())
    assert data["version"] == "1"
    assert len(data["models"]) == 1
    m = data["models"][0]
    assert m["name"] == "FakeModel"
    assert m["model_class"] == "FakeModel"
    assert m["lifecycle"][0]["name"] == "Staging"


def test_export_registry_lifecycle_empty_when_not_defined(tmp_path):
    class NoLifecycleConfig:
        SUPPORTED_DATASETS = []

        @classmethod
        def get_inference_params(cls):
            return {"promotion_metric": "rmse", "promotion_threshold": 50.0}

    NoLifecycleConfig.MODEL_CLASS = FakeModel
    reg = {"FakeModel": (FakeModel, NoLifecycleConfig, {})}
    out = tmp_path / "model_registry.yaml"
    export_registry(reg, out)
    data = yaml.safe_load(out.read_text())
    assert data["models"][0]["lifecycle"] == []


def test_export_round_trip(tmp_path):
    """export → reload should produce identical enabled entries."""

    # Build a minimal fake registry that matches the YAML fixture
    class FakeMACKConfig:
        SUPPORTED_DATASETS = []

        @classmethod
        def get_inference_params(cls):
            return {
                "lifecycle": [
                    {
                        "name": "Production",
                        "metric": "accuracy",
                        "threshold": 0.80,
                        "direction": "higher_is_better",
                    }
                ]
            }

    class FakeMACKModel:
        pass

    FakeMACKConfig.MODEL_CLASS = FakeMACKModel
    # Registry key matches the model class name so the round-trip name assertion holds
    reg = {"FakeMACKModel": (FakeMACKModel, FakeMACKConfig, {})}
    out = tmp_path / "exported.yaml"
    export_registry(reg, out)
    reloaded = load_registry(out)
    # All entries that export produces must be enabled=True
    assert all(e.enabled for e in reloaded)
    assert reloaded[0].name == "FakeMACKModel"


# ── resolve_entries tests ──────────────────────────────────────────────────


def test_resolve_entries_resolves_by_class_name():
    entries = [
        ModelEntry(
            name="FakeModel",
            model_class_name="FakeModel",
            config_class_name="FakeConfig",
            datasets=[],
            backend="zenodo",
            dummy=False,
            enabled=True,
            lifecycle=[],
            serve_aliases=["Production"],
            prefect={},
        )
    ]
    resolved = resolve_entries(entries, FAKE_REGISTRY)
    assert len(resolved) == 1
    assert resolved[0].model_cls is FakeModel
    assert resolved[0].config_cls is FakeConfig


def test_resolve_entries_resolves_without_config_class_name():
    entries = [
        ModelEntry(
            name="FakeModel",
            model_class_name="FakeModel",
            config_class_name=None,
            datasets=[],
            backend="zenodo",
            dummy=False,
            enabled=True,
            lifecycle=[],
            serve_aliases=["Production"],
            prefect={},
        )
    ]
    resolved = resolve_entries(entries, FAKE_REGISTRY)
    assert resolved[0].config_cls is FakeConfig


def test_resolve_entries_raises_on_unknown_model():
    entries = [
        ModelEntry(
            name="Ghost",
            model_class_name="GhostModel",
            config_class_name=None,
            datasets=[],
            backend="zenodo",
            dummy=False,
            enabled=True,
            lifecycle=[],
            serve_aliases=["Production"],
            prefect={},
        )
    ]
    with pytest.raises(ValueError, match="GhostModel"):
        resolve_entries(entries, FAKE_REGISTRY)
