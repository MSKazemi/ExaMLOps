"""Agent scaling defaults and opt-in resources in the ExaMLOps Helm chart."""

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


def _render(*extra: str) -> list[dict]:
    result = subprocess.run(
        [
            HELM,
            "template",
            "rel",
            str(CHART),
            "--set",
            "global.imageRegistry=ghcr.io/example/",
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _agent_resource(documents: list[dict], kind: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == kind and document["metadata"]["name"].endswith("-agent")
    )


def test_agent_scaling_defaults_are_safe_and_control_plane_pdb_is_preserved():
    values = _values()
    agent = values["agent"]

    assert agent["replicaCount"] == 2
    assert agent["autoscaling"] == {
        "enabled": False,
        "minReplicas": 2,
        "maxReplicas": 10,
        "targetCPUUtilizationPercentage": 70,
    }
    assert agent["pdb"] == {"enabled": True, "minAvailable": 1}
    assert values["controlPlane"]["pdb"]["minAvailable"] == 1


@needs_helm
def test_default_render_keeps_two_replicas_and_adds_agent_pdb_without_hpa():
    documents = _render()
    deployment = _agent_resource(documents, "Deployment")
    pdb = _agent_resource(documents, "PodDisruptionBudget")

    assert deployment["spec"]["replicas"] == 2
    assert pdb["spec"]["minAvailable"] == 1
    assert pdb["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == "agent"
    assert not any(
        document.get("kind") == "HorizontalPodAutoscaler"
        and document["metadata"]["name"].endswith("-agent")
        for document in documents
    )


@needs_helm
def test_opt_in_agent_hpa_owns_replica_count_and_renders_cpu_target():
    documents = _render(
        "--set",
        "agent.autoscaling.enabled=true",
        "--set",
        "agent.autoscaling.minReplicas=3",
        "--set",
        "agent.autoscaling.maxReplicas=7",
        "--set",
        "agent.autoscaling.targetCPUUtilizationPercentage=65",
    )
    deployment = _agent_resource(documents, "Deployment")
    hpa = _agent_resource(documents, "HorizontalPodAutoscaler")

    assert "replicas" not in deployment["spec"]
    assert hpa["spec"]["scaleTargetRef"] == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": deployment["metadata"]["name"],
    }
    assert hpa["spec"]["minReplicas"] == 3
    assert hpa["spec"]["maxReplicas"] == 7
    assert hpa["spec"]["metrics"][0]["resource"]["target"] == {
        "type": "Utilization",
        "averageUtilization": 65,
    }
