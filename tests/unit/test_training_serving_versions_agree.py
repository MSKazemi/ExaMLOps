"""A model must be served by the library versions it was trained with.

Training runs from the uv workspace (`uv.lock`); serving runs in the Ray Serve image built from
`serving/ray_serving/requirements.txt`; the tracking server is its own image. Nothing tied them
together, and Dependabot updates each manifest on its own: PR #16 (2026-09-10) moved the lock to
mlflow 3.15.2 and xgboost 3.4.1 while the serving image kept 3.11.1 and 3.2.0 — a model trained
after that merge would be pickled by one xgboost and loaded by an older one, with every unit test
green. `.github/dependabot.yml` now leaves both packages to a deliberate, all-at-once upgrade;
this is what makes a half-done upgrade fail instead of ship.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVING_REQS = ROOT / "serving" / "ray_serving" / "requirements.txt"
COMPOSE = ROOT / "platform" / "infra" / "docker-compose"


def _locked(name: str) -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {p["version"] for p in lock["package"] if p["name"] == name}
    assert len(versions) == 1, f"uv.lock holds {sorted(versions) or 'no'} version(s) of {name}"
    return versions.pop()


def _exact_pins(name: str, path: Path) -> list[str]:
    return re.findall(rf"(?<![\w-]){re.escape(name)}==([\w.]+)", path.read_text())


def test_mlflow_is_one_version_from_training_to_serving() -> None:
    found: dict[str, str] = {"uv.lock": _locked("mlflow")}
    for rel in ("pyproject.toml", "pipelines/pyproject.toml", "serving/pyproject.toml"):
        for version in _exact_pins("mlflow", ROOT / rel):
            found[rel] = version
    for path in (SERVING_REQS, COMPOSE / "Dockerfile.jupyterlab"):
        for version in _exact_pins("mlflow", path):
            found[str(path.relative_to(ROOT))] = version
    server = re.search(r"mlflow/mlflow:v([\w.]+)", (COMPOSE / "Dockerfile.mlflow").read_text())
    assert server, "Dockerfile.mlflow no longer names an mlflow image tag — the scan is broken"
    found["Dockerfile.mlflow (tracking server)"] = server.group(1)
    assert len(found) >= 6, f"expected mlflow pinned in every layer, found only {sorted(found)}"
    assert len(set(found.values())) == 1, f"mlflow versions disagree: {found}"


def test_ray_is_one_version_for_every_client_and_the_cluster() -> None:
    """Ray Client refuses to talk to a cluster of another Ray version, and the workspace's
    `fastapi<0.137` cap exists because of ray 2.55's `@serve.ingress` specifically. PR #6 moved
    the serving image alone to ray 2.58."""
    found: dict[str, str] = {"uv.lock": _locked("ray")}
    for path in (
        ROOT / "pyproject.toml",
        ROOT / "serving" / "pyproject.toml",
        SERVING_REQS,
        COMPOSE / "Dockerfile.jupyterlab",
    ):
        for version in re.findall(r"(?<![\w-])ray\[[\w,]+\]==([\w.]+)", path.read_text()):
            found[str(path.relative_to(ROOT))] = version
    assert len(found) >= 5, f"expected ray pinned in every layer, found only {sorted(found)}"
    assert len(set(found.values())) == 1, f"ray versions disagree: {found}"


def test_xgboost_serves_the_version_it_was_trained_with() -> None:
    served = _exact_pins("xgboost", SERVING_REQS)
    assert served, "serving/ray_serving/requirements.txt no longer pins xgboost exactly"
    trained = _locked("xgboost")
    assert served == [trained], (
        f"training locks xgboost {trained} but serving pins {served}: a model saved by the newer "
        "one is not guaranteed to load in the older one"
    )
