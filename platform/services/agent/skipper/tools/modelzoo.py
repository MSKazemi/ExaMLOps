from __future__ import annotations

from langchain_core.tools import tool

from skipper import config
from skipper.confirm import confirmed_write
from skipper.tools import _http


@tool
def modelzoo_status() -> str:
    """Show per-model ModelZoo freshness (fresh/stale) from the control plane."""
    data, err = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/v1/modelzoo/status"
    )
    if err:
        return err
    return f"ModelZoo status: {data}"


@tool
def modelzoo_events(limit: int = 20) -> str:
    """Show recent ModelZoo push events.

    Args:
        limit: Max events to return.
    """
    data, err = _http.request_json(
        "control_plane",
        "GET",
        f"{config.CONTROL_PLANE_URL}/v1/modelzoo/events",
        params={"limit": limit},
    )
    if err:
        return err
    return f"ModelZoo events: {data}"


@tool
def modelzoo_get_config() -> str:
    """Show ModelZoo integration runtime config (auto_retrain, poll interval, watch branch)."""
    data, err = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/v1/modelzoo/config"
    )
    if err:
        return err
    return f"ModelZoo config: {data}"


@tool
@confirmed_write(lambda: "Run a manual ModelZoo poll cycle against GitLab")
def modelzoo_sync() -> str:
    """Trigger a manual ModelZoo poll cycle against GitLab."""
    data, err = _http.request_json(
        "control_plane", "POST", f"{config.CONTROL_PLANE_URL}/v1/modelzoo/sync"
    )
    if err:
        return err
    return f"Sync result: {data}"


@tool
@confirmed_write(
    lambda auto_retrain=None, poll_interval_seconds=None, watch_branch=None: (
        f"Update ModelZoo config: auto_retrain={auto_retrain}, "
        f"poll_interval_seconds={poll_interval_seconds}, watch_branch={watch_branch}"
    )
)
def modelzoo_set_config(
    auto_retrain: bool | None = None,
    poll_interval_seconds: int | None = None,
    watch_branch: str | None = None,
) -> str:
    """Update ModelZoo integration runtime config. Only non-null fields are changed.

    Args:
        auto_retrain: Trigger retrain automatically on push.
        poll_interval_seconds: Background poller interval (0 disables).
        watch_branch: Branch the poller/webhooks watch.
    """
    body = {
        k: v
        for k, v in {
            "auto_retrain": auto_retrain,
            "poll_interval_seconds": poll_interval_seconds,
            "watch_branch": watch_branch,
        }.items()
        if v is not None
    }
    data, err = _http.request_json(
        "control_plane", "PUT", f"{config.CONTROL_PLANE_URL}/v1/modelzoo/config", json=body
    )
    if err:
        return err
    return f"Updated config: {data}"


TOOLS = [modelzoo_status, modelzoo_events, modelzoo_get_config, modelzoo_sync, modelzoo_set_config]
