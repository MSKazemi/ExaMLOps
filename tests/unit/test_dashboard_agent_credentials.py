"""Deployment contract for distinct dashboard and CLI agent credentials."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "platform" / "infra" / "docker-compose" / "docker-compose.yml"
CHART = REPO / "platform" / "infra" / "helm" / "examlops"
HELM = shutil.which("helm") or shutil.which("helm", path=str(Path.home() / ".local" / "bin"))


def test_compose_passes_separate_dashboard_credential_and_principal_map():
    services = yaml.safe_load(COMPOSE.read_text())["services"]

    assert services["agent"]["environment"]["AGENT_API_KEYS_JSON"] == ("${AGENT_API_KEYS_JSON:-}")
    assert services["dashboard"]["environment"]["DASHBOARD_AGENT_API_KEY"] == (
        "${DASHBOARD_AGENT_API_KEY:-}"
    )
    # Compatibility remains explicit in both services during credential migration.
    assert services["agent"]["environment"]["AGENT_API_KEY"] == "${AGENT_API_KEY:-}"
    assert services["dashboard"]["environment"]["AGENT_API_KEY"] == "${AGENT_API_KEY:-}"


@pytest.mark.skipif(HELM is None, reason="helm is not installed")
def test_helm_sources_new_credentials_from_the_existing_secret():
    result = subprocess.run(
        [
            HELM,
            "template",
            "rel",
            str(CHART),
            "--set",
            "global.imageRegistry=ghcr.io/example/",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    deployments = {
        doc["metadata"]["name"].rsplit("-", 1)[-1]: doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc and doc.get("kind") == "Deployment"
    }

    agent_env = {
        item["name"]: item
        for item in deployments["agent"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    dashboard_env = {
        item["name"]: item
        for item in deployments["dashboard"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    agent_ref = agent_env["AGENT_API_KEYS_JSON"]["valueFrom"]["secretKeyRef"]
    dashboard_ref = dashboard_env["DASHBOARD_AGENT_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert agent_ref == {
        "name": "examlops-secrets",
        "key": "AGENT_API_KEYS_JSON",
        "optional": True,
    }
    assert dashboard_ref == {
        "name": "examlops-secrets",
        "key": "DASHBOARD_AGENT_API_KEY",
        "optional": True,
    }
