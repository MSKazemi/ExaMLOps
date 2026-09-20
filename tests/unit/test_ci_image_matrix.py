"""Guard: the images CI builds must be the images the deploy runs.

``build:images`` publishes one image per service and ``lxp_release.sh`` then pulls them by
name. Nothing else connects those two lists. A service added to ``docker-compose.yml`` and
forgotten in the CI matrix does not fail anything at build time — it fails on the node, at
``docker compose pull``, in the middle of a production deploy, which is the worst available
moment to discover it.

The second half is the same claim from the other side: an image can only be pulled if the
compose file names it with the registry prefix. A service whose ``image:`` is still the bare
local name silently keeps building on the node while every other service pulls, so the deploy
is half one thing and half the other and reports success.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".gitlab-ci.yml"
_COMPOSE = _ROOT / "platform" / "infra" / "docker-compose" / "docker-compose.yml"

# Services that are deliberately NOT built in CI. Each needs a reason, because the whole
# point of the guard is that "not in the matrix" should be a decision rather than an
# oversight.
_EXCLUDED = {
    # Its build context is the PARENT of this repository — it needs the sibling dataplane-bus
    # checkout, which a CI clone of this repo does not have. Built on the node, as before.
    "dataplane-bus-bridge",
}


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def _built_services() -> set[str]:
    return {name for name, body in _compose()["services"].items() if body.get("build")}


def _matrix_services() -> set[str]:
    job = yaml.safe_load(_CI.read_text())["build:images"]
    entries = job["parallel"]["matrix"]
    return {svc for entry in entries for svc in entry["SERVICE"]}


def test_there_is_something_to_compare():
    """Both assertions below pass trivially if either list comes back empty."""
    assert len(_built_services()) >= 10, sorted(_built_services())
    assert len(_matrix_services()) >= 10, sorted(_matrix_services())


def test_every_locally_built_service_is_built_in_ci_or_excluded_on_purpose():
    missing = sorted(_built_services() - _matrix_services() - _EXCLUDED)
    assert not missing, (
        "these compose services are built from this repo but have no entry in the "
        f"build:images matrix, so registry-mode deploys will fail to pull them: {missing}. "
        "Add them to the matrix, or to _EXCLUDED here with the reason they cannot be built "
        "in CI."
    )


def test_the_matrix_does_not_name_a_service_that_cannot_be_built():
    phantom = sorted(_matrix_services() - _built_services())
    assert not phantom, (
        f"build:images tries to build {phantom}, which either does not exist in the compose "
        "file or has no build: section. build_image.sh resolves context and Dockerfile from "
        "compose, so these jobs fail every run."
    )


def test_an_excluded_service_is_one_that_really_exists():
    """An exclusion for a service that was renamed away is a comment, not a decision."""
    stale = sorted(_EXCLUDED - set(_compose()["services"]))
    assert not stale, (
        f"_EXCLUDED names {stale}, which is not in the compose file any more — the exemption "
        "outlived the service it was written for."
    )


def test_every_built_service_image_is_registry_addressable():
    """A bare `image: examlops-x` cannot be pushed or pulled; it can only be built locally."""
    services = _compose()["services"]
    unparameterised = sorted(
        name
        for name in _built_services()
        if "${EXAMLOPS_IMAGE_PREFIX" not in str(services[name].get("image", ""))
    )
    assert not unparameterised, (
        "these services are built from this repo but their compose `image:` is not "
        f"parameterised by EXAMLOPS_IMAGE_PREFIX: {unparameterised}. In registry mode the "
        "rest of the stack pulls a tested image while these rebuild on the node, and the "
        "deploy reports success either way. Use "
        "`image: ${EXAMLOPS_IMAGE_PREFIX:-examlops}-<name>:${EXAMLOPS_IMAGE_TAG:-latest}`."
    )


def test_the_local_default_is_unchanged():
    """Developers who set neither variable must get exactly the old image names."""
    services = _compose()["services"]
    for name in sorted(_built_services()):
        image = services[name]["image"]
        assert image.startswith("${EXAMLOPS_IMAGE_PREFIX:-examlops}-"), image
        assert image.endswith(":${EXAMLOPS_IMAGE_TAG:-latest}"), image
