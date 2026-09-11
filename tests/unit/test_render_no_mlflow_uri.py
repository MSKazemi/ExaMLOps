# tests/unit/test_render_no_mlflow_uri.py
"""USAR I0 — a manifest names exactly what runs (spec-usar-1 R-SUB-18/19, ADR 0142 d3).

The renderer used to write ``storageUri: mlflow://models/<name>@<alias>``: a scheme no KServe
storage initializer reads, naming an alias that moves while the object in the cluster does not.
Resolution now happens before rendering, and anything that is not loadable storage is refused.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.platform_db import init_db  # noqa: E402
from examlops.serving.substrates import kserve, resolve  # noqa: E402
from examlops.serving.substrates.resolve import RenderError  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-key")
    monkeypatch.delenv("EXAMLOPS_MLFLOW_ARTIFACTS_DESTINATION", raising=False)
    init_db()


class _Client:
    """The two MLflow registry calls resolution makes."""

    def __init__(self, uri: str, version: str = "17") -> None:
        self.uri, self.version, self.calls = uri, version, []

    def get_model_version_by_alias(self, name, alias):
        self.calls.append(("alias", name, alias))
        return SimpleNamespace(version=self.version)

    def get_model_version_download_uri(self, name, version):
        self.calls.append(("uri", name, version))
        return self.uri


def test_an_alias_resolves_to_a_concrete_version_and_its_storage():
    client = _Client("s3://mlflow-artifacts/1/models/m-abc/artifacts")
    ref = resolve.resolve_ref("JPCP", alias="Production", client=client)
    assert (ref.model, ref.version, ref.alias) == ("jpcp", "17", "Production")
    assert ref.artifact_uri == "s3://mlflow-artifacts/1/models/m-abc/artifacts"
    assert client.calls == [("alias", "jpcp", "Production"), ("uri", "jpcp", "17")]


@pytest.mark.parametrize(
    "uri",
    [
        "mlflow://models/jpcp@Production",
        "models:/jpcp/17",
        "runs:/abc/model",
        "file:///tmp/m",
        "/tmp/m",
    ],
)
def test_registry_and_local_references_are_refused(uri):
    with pytest.raises(RenderError):
        resolve.resolve_ref("JPCP", alias="Production", client=_Client(uri))


def test_a_proxied_artifact_uri_is_refused_without_its_destination():
    with pytest.raises(RenderError, match="EXAMLOPS_MLFLOW_ARTIFACTS_DESTINATION"):
        resolve.resolve_ref("JPCP", client=_Client("mlflow-artifacts:/1/models/m-abc/artifacts"))


def test_a_proxied_artifact_uri_maps_onto_the_configured_destination(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MLFLOW_ARTIFACTS_DESTINATION", "s3://mlflow-artifacts/")
    ref = resolve.resolve_ref("JPCP", client=_Client("mlflow-artifacts:/1/models/m-abc/artifacts"))
    assert ref.artifact_uri == "s3://mlflow-artifacts/1/models/m-abc/artifacts"


def test_offline_rendering_needs_both_a_uri_and_a_version():
    with pytest.raises(RenderError, match="--version"):
        resolve.resolve_ref("JPCP", artifact_uri="s3://b/k")
    ref = resolve.resolve_ref("JPCP", version="17", artifact_uri="s3://b/k", alias=None)
    assert (ref.version, ref.artifact_uri, ref.alias) == ("17", "s3://b/k", None)


def test_an_unsigned_version_says_so_and_a_signed_one_carries_its_digest(tmp_path):
    from examlops import supplychain

    assert resolve.resolve_ref("JPCP", version="17", artifact_uri="s3://b/k").digest == "unsigned"
    art = tmp_path / "model.pkl"
    art.write_bytes(b"weights")
    sig = supplychain.sign_model("JPCP", "17", [art])
    ref = resolve.resolve_ref("JPCP", version="17", artifact_uri="s3://b/k")
    assert ref.digest == f"sha256:{sig.digest}"


def test_no_render_contains_a_registry_uri():
    ref = resolve.resolve_ref(
        "JPCP", version="17", artifact_uri="s3://mlflow-artifacts/1/x", alias="Production"
    )
    for yaml_ in (
        {"name": "JPCP", "framework": "sklearn"},
        {"name": "ChatModel", "task_type": "text_generation", "engine": {"engine": "vllm"}},
    ):
        text = repr(kserve.render(yaml_, ref))
        assert "mlflow://" not in text and "models:/" not in text
        assert "s3://mlflow-artifacts/1/x" in text
