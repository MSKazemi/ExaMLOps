# tests/unit/test_serve_manifest_cli.py
"""USAR I0 — `exa serve manifest` renders what would run, or says why it cannot (ADR 0142 d3/d4).

The command used to print a manifest naming ``mlflow://models/<name>@<alias>`` without asking the
registry anything. It now resolves the alias to a concrete version first; with no registry it
renders only from an explicit ``--version`` + ``--artifact-uri``, and otherwise refuses with a hint.
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

runner = CliRunner()


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-key")
    # An address nothing listens on: resolution through the registry must fail fast, not hang.
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:9")
    monkeypatch.setenv("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")
    monkeypatch.setenv("MLFLOW_HTTP_REQUEST_TIMEOUT", "2")
    init_db()
    reg = tmp_path / "models"
    reg.mkdir()
    (reg / "jpcp.yaml").write_text(
        yaml.safe_dump({"name": "JPCP", "framework": "sklearn", "project": "research"})
    )
    return reg


def test_offline_render_names_the_exact_version_and_uri(registry, tmp_path):
    out = tmp_path / "jpcp.yaml"
    result = runner.invoke(
        serve.app,
        [
            "manifest",
            "JPCP",
            "--registry-dir",
            str(registry),
            "--version",
            "17",
            "--artifact-uri",
            "s3://mlflow-artifacts/1/models/m-a/artifacts",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    m = yaml.safe_load(out.read_text())
    assert m["apiVersion"] == "serving.kserve.io/v1beta1"
    assert (
        m["spec"]["predictor"]["model"]["storageUri"]
        == "s3://mlflow-artifacts/1/models/m-a/artifacts"
    )
    assert m["metadata"]["labels"]["examlops.io/version"] == "17"
    assert m["metadata"]["labels"]["examlops.io/project"] == "research"
    assert "mlflow://" not in out.read_text()


def test_an_unreachable_registry_is_refused_with_the_offline_hint(registry):
    result = runner.invoke(serve.app, ["manifest", "JPCP", "--registry-dir", str(registry)])
    assert result.exit_code != 0
    assert "--artifact-uri" in result.output


def test_a_registry_reference_as_artifact_uri_is_refused(registry):
    result = runner.invoke(
        serve.app,
        [
            "manifest",
            "JPCP",
            "--registry-dir",
            str(registry),
            "--version",
            "17",
            "--artifact-uri",
            "models:/jpcp/17",
        ],
    )
    assert result.exit_code != 0
    assert "registry reference" in result.output


def test_a_canary_cannot_be_rendered_offline(registry):
    result = runner.invoke(
        serve.app,
        [
            "manifest",
            "JPCP",
            "--registry-dir",
            str(registry),
            "--version",
            "17",
            "--artifact-uri",
            "s3://b/k",
            "--canary",
            "10",
        ],
    )
    assert result.exit_code != 0
    assert "cannot be combined" in result.output
