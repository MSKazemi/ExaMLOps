"""Deployment contracts for the dashboard Copilot transport."""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "platform" / "infra" / "docker-compose" / "docker-compose.yml"


def test_compose_runs_agent_and_wires_dashboard_to_service_dns():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    assert "agent" in services
    assert services["agent"]["healthcheck"]["test"]
    assert services["dashboard"]["environment"]["AGENT_URL"] == "http://agent:18004"
    assert "agent" in services["dashboard"]["depends_on"]


def test_compose_agent_port_is_bound_to_loopback_only():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    assert services["agent"]["ports"] == ["127.0.0.1:18004:18004"]
