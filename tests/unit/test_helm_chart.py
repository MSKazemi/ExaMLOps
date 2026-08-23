"""The Helm chart is a published artifact; hold it to what a published artifact must be.

`make helm-validate` graded this chart green from the day it was written, and
`docs/guides/enterprise-installation.md` published that grade — while the chart's own default
values composed image references like `examlops-agent:0.37.0`. An unqualified name resolves to
`docker.io/library/examlops-agent`, the Docker Official Images namespace, which nobody outside
Docker can publish to. So the defaults asked for something that can never exist, `helm lint`
reported `0 chart(s) failed`, and the failure would first appear in someone else's cluster as
ImagePullBackOff.

Two of these tests need `helm` and skip without it. The rest read Chart.yaml directly and run
everywhere, because the two defects that mattered most — a stale `appVersion` and a maintainer
who is not the author — are plain YAML.
"""

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


def _chart() -> dict:
    return yaml.safe_load((CHART / "Chart.yaml").read_text())


def _platform_version() -> str:
    pyproject = (REPO / "pyproject.toml").read_text()
    return next(
        line.split("=", 1)[1].strip().strip('"')
        for line in pyproject.splitlines()
        if line.startswith("version =")
    )


def _render(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [HELM, "template", "rel", str(CHART), *extra], capture_output=True, text=True
    )


def test_app_version_tracks_the_platform_version():
    """`tag: ""` defaults to appVersion, so a stale value pins every image to an old release.

    It said 0.37.0 while the platform was at 0.48.0 — eleven minor releases behind, and nothing
    anywhere compared the two.
    """
    version = _platform_version()
    assert _chart()["appVersion"] == version, (
        f"Chart appVersion {_chart()['appVersion']!r} != platform version {version!r} — "
        "the chart would deploy images tagged with the wrong release"
    )


def test_the_maintainer_is_the_author():
    """A chart published under someone's name lists that person, not the product."""
    maintainers = _chart()["maintainers"]
    emails = {m.get("email") for m in maintainers}
    assert "mohsen.seyedkazemi@gmail.com" in emails, maintainers


def test_the_chart_declares_a_home_that_exists_publicly():
    assert _chart()["home"].startswith("https://github.com/MSKazemi/"), _chart()["home"]


@needs_helm
def test_rendering_without_a_registry_is_refused():
    """The defect, pinned. Silently emitting a `library/` reference is the behaviour to prevent."""
    result = _render()
    assert result.returncode != 0, (
        "the chart rendered with no registry — it would emit library/ refs"
    )
    assert "global.imageRegistry is required" in result.stderr
    assert "ghcr.io" in result.stderr, "the error must show how to fix it, not just what is wrong"


@needs_helm
def test_every_rendered_image_carries_a_registry_host():
    """The other direction: with a registry set, nothing slips through unqualified."""
    result = _render("--set", "global.imageRegistry=ghcr.io/example/")
    assert result.returncode == 0, result.stderr

    images = [
        doc_image for doc in yaml.safe_load_all(result.stdout) if doc for doc_image in _images(doc)
    ]
    assert images, "the chart rendered no workloads at all"
    for image in images:
        host = image.split("/")[0]
        assert "." in host or ":" in host, (
            f"{image!r} has no registry host — Kubernetes resolves that to docker.io/library/"
        )


@needs_helm
def test_every_workload_is_actually_readiness_gated():
    """Each tier claims `maxUnavailable: 0` — "never drop below desired during upgrade".

    That claim is only true with a readinessProbe: without one Kubernetes marks a pod Ready the
    moment the container starts, so a rolling upgrade routes traffic to a replica that is not yet
    serving. The agent tier carried the comment and no probe.
    """
    result = _render("--set", "global.imageRegistry=ghcr.io/example/")
    assert result.returncode == 0, result.stderr

    deployments = [
        doc for doc in yaml.safe_load_all(result.stdout) if doc and doc.get("kind") == "Deployment"
    ]
    assert len(deployments) == 3, [d["metadata"]["name"] for d in deployments]

    for dep in deployments:
        name = dep["metadata"]["name"]
        rolling = dep["spec"].get("strategy", {}).get("rollingUpdate", {})
        for container in dep["spec"]["template"]["spec"]["containers"]:
            if rolling.get("maxUnavailable") == 0:
                assert "readinessProbe" in container, (
                    f"{name} promises maxUnavailable=0 but {container['name']} has no "
                    "readinessProbe, so every pod counts as ready the instant it starts"
                )
            assert "livenessProbe" in container, f"{name}/{container['name']} has no livenessProbe"


def _images(doc: dict) -> list[str]:
    spec = doc.get("spec", {}).get("template", {}).get("spec", {})
    return [c["image"] for c in spec.get("containers", []) if "image" in c]


def test_chart_version_moves_with_the_platform():
    """A frozen chart version makes every republish invisible to `helm repo update`.

    `helm repo index` keys entries on the chart version, so shipping changed contents
    under a version a consumer already has is not an upgrade -- it is a no-op they
    cannot detect. The chart lives in the same repository as the code it deploys, so
    the cheapest rule that cannot rot is lockstep with the platform version.
    """
    assert str(_chart()["version"]) == _platform_version(), (
        f"Chart version {_chart()['version']} != platform {_platform_version()}; bump it, "
        "or consumers of the published repo will never see this change."
    )
