"""Compose security gate (enterprise-readiness Phase 0, items 0.6 + 0.8).

Locks in the dev-compose hardening so an insecure pattern can't silently regress:
  * the dashboard talks to Docker through the SCOPED socket-proxy, never the raw rw socket
    (a raw /var/run/docker.sock mount is root-equivalent host takeover);
  * the socket-proxy denies the dangerous API sections (EXEC/IMAGES/VOLUMES/…);
  * no service ships a usable placeholder secret literal (e.g. CONTROL_PLANE_TOKEN=changeme);
  * the dashboard's own secrets stay required (`:?`), i.e. fail-closed if unset.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_COMPOSE = (
    Path(__file__).parents[2] / "platform" / "infra" / "docker-compose" / "docker-compose.yml"
)


def _services() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())["services"]


def test_dashboard_does_not_mount_raw_docker_socket():
    dash = _services()["dashboard"]
    for vol in dash.get("volumes", []):
        assert "/var/run/docker.sock" not in vol, (
            "dashboard mounts the raw Docker socket — route it through docker-socket-proxy (item 0.6)"
        )
    # It must instead point at the proxy.
    assert dash["environment"].get("DOCKER_HOST") == "tcp://docker-socket-proxy:2375"


def test_docker_socket_proxy_denies_dangerous_sections():
    proxy = _services().get("docker-socket-proxy")
    assert proxy is not None, "no docker-socket-proxy service (item 0.6)"
    env = proxy["environment"]
    # Only containers + POST are allowed; everything dangerous is explicitly denied.
    assert env.get("CONTAINERS") in ("1", 1)
    for denied in ("EXEC", "IMAGES", "VOLUMES", "NETWORKS", "SWARM", "SECRETS", "AUTH"):
        assert str(env.get(denied, "0")) == "0", f"{denied} must be denied on the socket proxy"
    # The socket itself is mounted read-only into the proxy.
    assert any(v.endswith(":ro") and "docker.sock" in v for v in proxy["volumes"])


def test_no_placeholder_secret_literals():
    raw = _COMPOSE.read_text()
    assert "CONTROL_PLANE_TOKEN:-changeme" not in raw, "placeholder token default (item 0.8/QW6)"
    # The control-plane token must have no insecure inline default.
    cp_env = _services()["control-plane"]["environment"]
    assert "changeme" not in str(cp_env.get("CONTROL_PLANE_TOKEN", ""))


def test_dashboard_secrets_are_required_fail_closed():
    env = _services()["dashboard"]["environment"]
    for key in ("DASHBOARD_JWT_SECRET", "DASHBOARD_SECRET_KEY", "DASHBOARD_ADMIN_PASSWORD"):
        assert ":?" in str(env[key]), f"{key} must be required (fail-closed) not defaulted"
