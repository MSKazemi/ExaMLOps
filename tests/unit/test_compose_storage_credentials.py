"""No service holds the object store's root credential unless it administers the store (P3.4).

Every service that touched MinIO used to get ``MINIO_ROOT_USER`` / ``MINIO_ROOT_PASSWORD`` —
mlflow, the dashboard, the backup sidecar, and through JupyterHub every notebook, which runs
arbitrary user code. With the root credential, whoever controls any one of them can rewrite every
artifact, create users, or delete the store. Each now reads its own least-privilege key, which
``minio-init`` provisions; the root credential remains only as the fallback default while an
operator has not set that key. This guard keeps a new service from quietly taking root again.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "platform" / "infra" / "docker-compose"
COMPOSE = COMPOSE_DIR / "docker-compose.yml"

# The two services that administer the store and legitimately hold root.
ROOT_HOLDERS = {"minio", "minio-init"}
# Variables that carry an object-store credential.
_CRED_VARS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
}
_OWN_KEY = re.compile(
    r"^\$\{(MINIO_[A-Z]+_(?:ACCESS|SECRET)_KEY):-\$\{MINIO_ROOT_(?:USER|PASSWORD)"
)


def _services() -> dict[str, dict]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def _env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def _credentials() -> dict[str, dict[str, str]]:
    return {
        name: {k: v for k, v in _env(svc).items() if k in _CRED_VARS or "MINIO_ROOT" in v}
        for name, svc in _services().items()
    }


def test_only_the_stores_administrators_hold_root():
    offenders = {
        name: sorted(k for k, v in creds.items() if "MINIO_ROOT" in v and not _OWN_KEY.match(v))
        for name, creds in _credentials().items()
        if name not in ROOT_HOLDERS
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, (
        f"these services get the MinIO root credential directly: {offenders}. Give the service "
        "its own MINIO_<SERVICE>_ACCESS_KEY/SECRET_KEY (with root only as the `:-` fallback) and "
        "provision it in minio-init."
    )


def test_every_service_key_is_provisioned_by_minio_init():
    init = _services()["minio-init"]
    script = "\n".join(init["entrypoint"])
    init_env = _env(init)
    missing = []
    for name, creds in _credentials().items():
        for value in creds.values():
            match = _OWN_KEY.match(value)
            if not match:
                continue
            var = match.group(1)
            if var not in init_env or f"${{{var}}}" not in script:
                missing.append(f"{name}: {var}")
    assert not missing, f"per-service keys minio-init never creates: {missing}"


def test_the_known_services_use_their_own_keys():
    creds = _credentials()
    assert creds["mlflow"]["AWS_ACCESS_KEY_ID"].startswith("${MINIO_MLFLOW_ACCESS_KEY:-")
    assert creds["backup"]["AWS_ACCESS_KEY_ID"].startswith("${MINIO_BACKUP_ACCESS_KEY:-")
    assert creds["dashboard"]["MINIO_ACCESS_KEY"].startswith("${MINIO_DASHBOARD_ACCESS_KEY:-")
    assert creds["ray-serving"]["AWS_ACCESS_KEY_ID"].startswith("${MINIO_SERVING_ACCESS_KEY:-")


def test_notebooks_get_the_notebook_credential_not_root():
    """A notebook runs arbitrary user code; the hub must hand it the scoped key."""
    config = (COMPOSE_DIR / "jupyterhub_config.py").read_text(encoding="utf-8")
    spawner_env = config.split("c.DockerSpawner.environment", 1)[1].split("}", 1)[0]
    assert "MINIO_NOTEBOOK_ACCESS_KEY" in spawner_env
    assert "MINIO_ROOT" not in spawner_env
