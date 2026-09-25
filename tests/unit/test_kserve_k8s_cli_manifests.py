# tests/unit/test_kserve_k8s_cli_manifests.py
"""The Kubernetes serving commands (ADR 0015 d1, ADR 0142 d3): what an operator runs.

* ``exa serve manifest`` now carries the in-pod verify-before-load wiring the ``kserve`` substrate
  would apply, and ``enforce`` refuses a render it cannot verify;
* ``exa serve verifier-manifest`` renders the cluster's storage container;
* ``exa serve kuberay-manifest`` renders the Ray multi-model server as a ``RayService``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli.commands import serve  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402
from examlops.serving.substrates import k8s_schema  # noqa: E402

runner = CliRunner()
IMAGE = "ghcr.io/example/verified-storage:v1"
RAY_IMAGE = "ghcr.io/example/examlops-ray-serving:v1.2.3"


@pytest.fixture()
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    for var in ("EXAMLOPS_SERVING_VERIFY", "EXAMLOPS_KSERVE_VERIFIER_IMAGE"):
        monkeypatch.delenv(var, raising=False)
    init_db()
    d = tmp_path / "models"
    d.mkdir()
    (d / "jpcp.yaml").write_text(yaml.safe_dump({"name": "JPCP", "framework": "sklearn"}))
    (d / "mack.yaml").write_text(yaml.safe_dump({"name": "MACK", "framework": "sklearn"}))
    return d


def _manifest(reg: Path, out: Path) -> list[str]:
    return [
        "manifest",
        "JPCP",
        "--registry-dir",
        str(reg),
        "--version",
        "17",
        "--artifact-uri",
        "s3://mlflow-artifacts/1/models/m-a/artifacts",
        "--out",
        str(out),
    ]


def test_manifest_carries_the_verifier_when_configured(reg, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SERVING_VERIFY", "enforce")
    monkeypatch.setenv("EXAMLOPS_KSERVE_VERIFIER_IMAGE", IMAGE)
    out = tmp_path / "jpcp.yaml"
    result = runner.invoke(serve.app, _manifest(reg, out))
    assert result.exit_code == 0, result.output
    predictor = yaml.safe_load(out.read_text())["spec"]["predictor"]
    assert predictor["storageContainerName"] == "examlops-verified-storage"
    assert predictor["annotations"]["examlops.io/verify-mode"] == "enforce"


def test_manifest_in_enforce_without_a_verifier_refuses(reg, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SERVING_VERIFY", "enforce")
    out = tmp_path / "jpcp.yaml"
    result = runner.invoke(serve.app, _manifest(reg, out))
    assert result.exit_code != 0 and not out.exists()
    assert "EXAMLOPS_KSERVE_VERIFIER_IMAGE" in result.output


def test_manifest_by_default_is_unwired_and_warns(reg, tmp_path):
    out = tmp_path / "jpcp.yaml"
    result = runner.invoke(serve.app, _manifest(reg, out))
    assert result.exit_code == 0, result.output
    assert "storageContainerName" not in yaml.safe_load(out.read_text())["spec"]["predictor"]
    assert "not rendered" in result.output


def test_verifier_manifest_renders_a_valid_storage_container(reg, tmp_path):
    out = tmp_path / "csc.yaml"
    result = runner.invoke(serve.app, ["verifier-manifest", "--image", IMAGE, "--out", str(out)])
    assert result.exit_code == 0, result.output
    obj = yaml.safe_load(out.read_text())
    assert obj["kind"] == "ClusterStorageContainer" and k8s_schema.validate(obj) == []
    assert obj["spec"]["container"]["image"] == IMAGE


def test_verifier_manifest_without_an_image_fails(reg):
    result = runner.invoke(serve.app, ["verifier-manifest"])
    assert result.exit_code != 0 and "EXAMLOPS_KSERVE_VERIFIER_IMAGE" in result.output


def test_kuberay_manifest_renders_the_whole_registry(reg, tmp_path):
    out = tmp_path / "ray.yaml"
    result = runner.invoke(
        serve.app,
        ["kuberay-manifest", "--image", RAY_IMAGE, "--registry-dir", str(reg), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    obj = yaml.safe_load(out.read_text())
    assert obj["kind"] == "RayService" and k8s_schema.validate(obj) == []
    assert obj["metadata"]["annotations"]["examlops.io/models"] == "JPCP,MACK"


def test_kuberay_manifest_without_an_image_fails(reg, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_KUBERAY_IMAGE", raising=False)
    result = runner.invoke(serve.app, ["kuberay-manifest", "--registry-dir", str(reg)])
    assert result.exit_code != 0 and "EXAMLOPS_KUBERAY_IMAGE" in result.output


def test_kuberay_manifest_refuses_unbounded_workers(reg):
    result = runner.invoke(
        serve.app,
        ["kuberay-manifest", "--image", RAY_IMAGE, "--registry-dir", str(reg),
         "--max-workers", "5000"],
    )  # fmt: skip
    assert result.exit_code != 0 and "256" in result.output
