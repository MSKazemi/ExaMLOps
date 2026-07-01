from __future__ import annotations

from langchain_core.tools import tool

from skipper.confirm import confirmed_write
from skipper.tools import _http


@tool
def list_services() -> str:
    """List platform Docker services and their status (via the dashboard)."""
    data, err = _http.dashboard().request("dashboard", "GET", "/api/containers")
    if err:
        return err
    return (
        "\n".join(f"- {c.get('name')}: {c.get('status', '?')}" for c in data)
        if data
        else "No services."
    )


@tool
def service_logs(service: str, lines: int = 100) -> str:
    """Tail logs for a platform service (via the dashboard).

    Args:
        service: Service/container name (e.g. 'mlflow').
        lines: Number of trailing log lines.
    """
    data, err = _http.dashboard().request(
        "dashboard", "GET", f"/api/containers/{service}/logs", params={"lines": lines}
    )
    if err:
        return err
    return f"logs for {service}:\n{data}"


@tool
@confirmed_write(lambda service: f"Restart service '{service}'")
def restart_service(service: str) -> str:
    """Restart a platform service (via the dashboard).

    Args:
        service: Service/container name.
    """
    data, err = _http.dashboard().request("dashboard", "POST", f"/api/containers/{service}/restart")
    return err or f"Restarted {service}: {data}"


@tool
@confirmed_write(lambda service: f"Start service '{service}'")
def start_service(service: str) -> str:
    """Start a platform service (via the dashboard).

    Args:
        service: Service/container name.
    """
    data, err = _http.dashboard().request("dashboard", "POST", f"/api/containers/{service}/start")
    return err or f"Started {service}: {data}"


@tool
@confirmed_write(lambda service: f"Stop service '{service}'")
def stop_service(service: str) -> str:
    """Stop a platform service (via the dashboard).

    Args:
        service: Service/container name.
    """
    data, err = _http.dashboard().request("dashboard", "POST", f"/api/containers/{service}/stop")
    return err or f"Stopped {service}: {data}"


TOOLS = [list_services, service_logs, restart_service, start_service, stop_service]
