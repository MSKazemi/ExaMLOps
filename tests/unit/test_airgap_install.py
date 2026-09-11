"""Air-gapped installs (`docs/guides/air-gapped-install.md`) rest on a few structural facts.

The guide mirrors a release into a registry the site controls and verifies it there with no
network. It was tested end to end against v0.54.0, but each piece that made that work can regress
without any build failing — the first to notice would be a site with no internet trying to pull:

* the release publishes `images-X.Y.Z.txt` (`name:tag@digest`, inside the signed `SHA256SUMS`),
  the list the guide mirrors from;
* every image the chart renders is built from `global.imageRegistry`, so one value moves the whole
  chart onto the mirror;
* every upstream image in the Compose bundle is a digest-pinned Docker Hub image — the only kind a
  Docker `registry-mirrors` entry serves;
* the guide's verification loop names every image the release publishes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "guides" / "air-gapped-install.md"
RELEASE = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"
COMPOSE = ROOT / "platform" / "infra" / "docker-compose" / "install" / "docker-compose.yml"

PINNED = re.compile(r"^(?P<repo>[a-z0-9._/-]+):[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def _runs(job: str) -> list[str]:
    return [s.get("run", "") for s in RELEASE["jobs"][job]["steps"]]


def _released_images() -> list[str]:
    return [i["name"] for i in RELEASE["jobs"]["images"]["strategy"]["matrix"]["include"]]


def test_the_release_publishes_the_signed_image_list_the_guide_mirrors_from():
    assert any(
        'echo "${IMAGE}:${VERSION}@${DIGEST}" > "refs/${NAME}.txt"' in r for r in _runs("images")
    ), "each image job must record name:tag@digest"
    run = next(r for r in _runs("github-release") if "images-${VERSION}.txt" in r)
    listed = run.index('cat artifacts/image-*/refs/*.txt | sort > "assets/images-${VERSION}.txt"')
    # Written before the checksums, so the signature on SHA256SUMS covers the digests.
    assert listed < run.index("sha256sum -- * > SHA256SUMS")


def test_every_chart_image_is_built_from_global_image_registry():
    image_lines = [
        (path.name, line.strip())
        for path in sorted((CHART / "templates").glob("*.yaml"))
        for line in path.read_text().splitlines()
        if re.match(r"^\s*(-\s*)?image:", line)
    ]
    assert image_lines, "the chart renders no images?"
    stray = [(name, line) for name, line in image_lines if 'include "examlops.image"' not in line]
    assert not stray, f"image not built by examlops.image (a mirror would not reach it): {stray}"
    helper = (CHART / "templates" / "_helpers.tpl").read_text()
    assert "$reg := .root.Values.global.imageRegistry" in helper
    assert 'printf "%s%s:%s" $reg' in helper


def test_every_upstream_compose_image_is_a_pinned_docker_hub_image():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    upstream = {
        name: svc["image"]
        for name, svc in services.items()
        if not svc["image"].startswith("${EXAMLOPS_REGISTRY")
    }
    assert upstream
    for name, image in upstream.items():
        match = PINNED.match(image)
        assert match, f"{name}: {image} is not pinned by digest"
        first = match["repo"].split("/")[0]
        # Docker's rule: a first component with a dot or a colon, or `localhost`, is a registry.
        assert "/" not in match["repo"] or not (
            "." in first or ":" in first or first == "localhost"
        ), f"{name}: {image} is not on Docker Hub, so registry-mirrors will not serve it"


def test_the_guides_verification_loop_names_every_released_image():
    loop = re.search(r"for image in ([a-z -]+); do", GUIDE.read_text())
    assert loop, "the guide's cosign verify loop is gone"
    assert sorted(loop[1].split()) == sorted(_released_images())


def test_the_guide_is_published_and_linked_from_the_install_guides():
    assert "guides/air-gapped-install.md" in (ROOT / "mkdocs.yml").read_text()
    for page in ("install-compose-bundle.md", "enterprise-installation.md"):
        assert "air-gapped-install.md" in (ROOT / "docs" / "guides" / page).read_text(), page
