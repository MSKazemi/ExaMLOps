"""Render contracts for least-privilege per-tier Helm Secrets."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "platform" / "infra" / "helm" / "examlops"
HELM = shutil.which("helm") or shutil.which("helm", path=str(Path.home() / ".local" / "bin"))

needs_helm = pytest.mark.skipif(HELM is None, reason="helm is not installed")


def _values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text())


def _render(*settings: str) -> list[dict]:
    command = [
        HELM,
        "template",
        "rel",
        str(CHART),
        "--set",
        "global.imageRegistry=ghcr.io/example/",
    ]
    for setting in settings:
        command.extend(("--set", setting))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _deployment(documents: list[dict], component: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document["metadata"]["name"].endswith(f"-{component}")
    )


def _secret_names(deployment: dict) -> list[str]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    names = [
        source["secretRef"]["name"]
        for source in container.get("envFrom", [])
        if "secretRef" in source
    ]
    names.extend(
        item["valueFrom"]["secretKeyRef"]["name"]
        for item in container.get("env", [])
        if "secretKeyRef" in item.get("valueFrom", {})
    )
    return names


def test_per_tier_secret_values_default_to_legacy_fallback():
    values = _values()
    assert values["existingSecret"] == "examlops-secrets"
    assert values["controlPlane"]["existingSecret"] == ""
    assert values["dashboard"]["existingSecret"] == ""
    assert values["agent"]["existingSecret"] == ""


@needs_helm
def test_empty_per_tier_values_render_the_legacy_global_secret_everywhere():
    documents = _render()

    for component in ("control-plane", "dashboard", "agent"):
        names = _secret_names(_deployment(documents, component))
        assert names
        assert set(names) == {"examlops-secrets"}


@needs_helm
def test_per_tier_overrides_cover_env_from_and_explicit_key_refs():
    expected = {
        "control-plane": "control-plane-credentials",
        "dashboard": "dashboard-credentials",
        "agent": "agent-credentials",
    }
    documents = _render(
        "controlPlane.existingSecret=control-plane-credentials",
        "dashboard.existingSecret=dashboard-credentials",
        "agent.existingSecret=agent-credentials",
    )

    for component, secret_name in expected.items():
        names = _secret_names(_deployment(documents, component))
        assert names
        assert set(names) == {secret_name}
