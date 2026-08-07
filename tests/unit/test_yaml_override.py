# tests/unit/test_yaml_override.py
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── upstream-library guard ───────────────────────────────────────────────────
# `seanergys_modelzoo` is an UPSTREAM library, not part of ExaMLOps (ADR 0094):
# the platform core never imports it — only the use-case pack does, through the
# loader seam. It is therefore not vendored in the public tree; CI and the
# deploy node fetch it from its own repo. Skip rather than fail when absent.
_MZ = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or (REPO_ROOT / "modelzoo"))
if not (_MZ / "seanergys_modelzoo").is_dir():
    pytest.skip(
        "seanergys_modelzoo not present — upstream library fetched at deploy/CI "
        "time. Set EXAMLOPS_MODELZOO_DIR to a checkout to run these tests.",
        allow_module_level=True,
    )
if str(_MZ) not in sys.path:
    sys.path.insert(0, str(_MZ))


# ── Minimal fakes (no MLflow, no Prefect, no Ray) ─────────────────────────────


class FakeDS:
    __name__ = "FakeDS"


class FakeModel:
    __name__ = "FAKE"


class FakeCfg:
    SUPPORTED_DATASETS = [FakeDS]

    @classmethod
    def get_inference_params(cls) -> dict:
        return {
            "model_id": "fake",
            "lifecycle": [
                {
                    "name": "Production",
                    "metric": "rmse",
                    "threshold": 50.0,
                    "direction": "lower_is_better",
                }
            ],
            "promotion_metric": "rmse",
            "promotion_threshold": 50.0,
            "promotion_direction": "lower_is_better",
        }

    @classmethod
    def get_train_components(cls, dataset_cls, split="train", is_dummy=False, backend_name=None):
        return (object(), None, {"is_dummy": is_dummy, "backend": backend_name})


def _make_fake_registry(monkeypatch):
    import pipelines.pipeline_generator as pg

    fake = {"FAKE": (FakeModel, FakeCfg, {})}
    monkeypatch.setattr(pg, "MODEL_REGISTRY", dict(fake))
    return pg


YAML_LIFECYCLE_OVERRIDE = textwrap.dedent("""
    version: "1"
    defaults:
      backend: zenodo
      dummy: false
      enabled: true
      serve_aliases: [Production]
    models:
      - name: FAKE
        model_class: FAKE
        datasets: [FakeDS]
        lifecycle:
          - name: Production
            metric: rmse
            threshold: 25.0
            direction: lower_is_better
        serve_aliases: [Production]
        prefect: {}
""")

YAML_BACKEND_OVERRIDE = textwrap.dedent("""
    version: "1"
    defaults:
      backend: zenodo
      dummy: false
      enabled: true
      serve_aliases: [Production]
    models:
      - name: FAKE
        model_class: FAKE
        datasets: [FakeDS]
        backend: minio
        lifecycle: []
        serve_aliases: [Production]
        prefect: {}
""")

YAML_DISABLED = textwrap.dedent("""
    version: "1"
    defaults:
      backend: zenodo
      dummy: false
      enabled: true
      serve_aliases: [Production]
    models:
      - name: FAKE
        model_class: FAKE
        datasets: []
        enabled: false
        lifecycle: []
        serve_aliases: []
        prefect: {}
""")

YAML_ADDITIVE = textwrap.dedent("""
    version: "1"
    defaults:
      backend: zenodo
      dummy: false
      enabled: true
      serve_aliases: [Production]
    models:
      - name: FAKE_v2
        model_class: FAKE
        datasets: [FakeDS]
        backend: dataplane
        lifecycle:
          - name: Production
            metric: rmse
            threshold: 20.0
            direction: lower_is_better
        serve_aliases: [Production]
        prefect: {}
""")


def test_apply_yaml_overrides_lifecycle(monkeypatch, tmp_path):
    pg = _make_fake_registry(monkeypatch)
    f = tmp_path / "reg.yaml"
    f.write_text(YAML_LIFECYCLE_OVERRIDE)
    pg.apply_yaml_registry(f)
    _, cfg, _ = pg.MODEL_REGISTRY["FAKE"]
    assert cfg.get_inference_params()["lifecycle"][0]["threshold"] == 25.0


def test_apply_yaml_overrides_backend(monkeypatch, tmp_path):
    pg = _make_fake_registry(monkeypatch)
    f = tmp_path / "reg.yaml"
    f.write_text(YAML_BACKEND_OVERRIDE)
    pg.apply_yaml_registry(f)
    _, cfg, _ = pg.MODEL_REGISTRY["FAKE"]
    _, _, result = cfg.get_train_components(FakeDS, backend_name=None)
    assert result["backend"] == "minio"


def test_apply_yaml_disabled_removes_entry(monkeypatch, tmp_path):
    pg = _make_fake_registry(monkeypatch)
    f = tmp_path / "reg.yaml"
    f.write_text(YAML_DISABLED)
    pg.apply_yaml_registry(f)
    assert "FAKE" not in pg.MODEL_REGISTRY


def test_apply_yaml_additive_registers_new_key(monkeypatch, tmp_path):
    pg = _make_fake_registry(monkeypatch)
    f = tmp_path / "reg.yaml"
    f.write_text(YAML_ADDITIVE)
    pg.apply_yaml_registry(f)
    assert "FAKE_v2" in pg.MODEL_REGISTRY
    _, cfg, _ = pg.MODEL_REGISTRY["FAKE_v2"]
    assert cfg.get_inference_params()["lifecycle"][0]["threshold"] == 20.0


def test_apply_yaml_does_not_mutate_original_config(monkeypatch, tmp_path):
    pg = _make_fake_registry(monkeypatch)
    original = FakeCfg.get_inference_params()["lifecycle"][0]["threshold"]
    f = tmp_path / "reg.yaml"
    f.write_text(YAML_LIFECYCLE_OVERRIDE)
    pg.apply_yaml_registry(f)
    assert FakeCfg.get_inference_params()["lifecycle"][0]["threshold"] == original


def test_apply_yaml_no_file_is_noop(monkeypatch, tmp_path):
    pg = _make_fake_registry(monkeypatch)
    pg.apply_yaml_registry(tmp_path / "nonexistent.yaml")
    # FAKE should still be present, unchanged
    assert "FAKE" in pg.MODEL_REGISTRY
    _, cfg, _ = pg.MODEL_REGISTRY["FAKE"]
    assert cfg.get_inference_params()["lifecycle"][0]["threshold"] == 50.0


def test_apply_yaml_env_overlay(monkeypatch, tmp_path):
    pg = _make_fake_registry(monkeypatch)
    base = tmp_path / "reg.yaml"
    env = tmp_path / "prod.yaml"
    base.write_text(YAML_BACKEND_OVERRIDE)  # backend: minio base
    env.write_text(
        textwrap.dedent("""
        models:
          - name: FAKE
            lifecycle:
              - name: Production
                metric: rmse
                threshold: 10.0
                direction: lower_is_better
    """)
    )
    pg.apply_yaml_registry(base, env)
    _, cfg, _ = pg.MODEL_REGISTRY["FAKE"]
    assert cfg.get_inference_params()["lifecycle"][0]["threshold"] == 10.0
    _, _, result = cfg.get_train_components(FakeDS, backend_name=None)
    assert result["backend"] == "minio"


def test_apply_yaml_skips_unknown_model_class(monkeypatch, tmp_path, capsys):
    pg = _make_fake_registry(monkeypatch)
    f = tmp_path / "reg.yaml"
    f.write_text(
        textwrap.dedent("""
        version: "1"
        defaults:
          backend: zenodo
          dummy: false
          enabled: true
          serve_aliases: [Production]
        models:
          - name: GHOST
            model_class: NONEXISTENT
            datasets: []
            lifecycle: []
            serve_aliases: []
            prefect: {}
    """)
    )
    pg.apply_yaml_registry(f)
    assert "GHOST" not in pg.MODEL_REGISTRY
    captured = capsys.readouterr()
    assert "WARNING" in captured.out


def test_apply_yaml_dummy_from_yaml_applies(monkeypatch, tmp_path):
    pg = _make_fake_registry(monkeypatch)
    f = tmp_path / "reg.yaml"
    f.write_text(
        textwrap.dedent("""
        version: "1"
        defaults:
          backend: zenodo
          dummy: false
          enabled: true
          serve_aliases: [Production]
        models:
          - name: FAKE
            model_class: FAKE
            datasets: [FakeDS]
            backend: zenodo
            dummy: true
            lifecycle: []
            serve_aliases: [Production]
            prefect: {}
    """)
    )
    pg.apply_yaml_registry(f)
    _, cfg, _ = pg.MODEL_REGISTRY["FAKE"]
    _, _, result = cfg.get_train_components(FakeDS, backend_name=None)
    assert result["is_dummy"] is True
