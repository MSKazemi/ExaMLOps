from __future__ import annotations

from langchain_core.tools import tool

from skipper import config
from skipper.confirm import confirmed_write
from skipper.tools import _http


@tool
def list_pending_approvals() -> str:
    """List pending model-change approvals awaiting sysadmin action."""
    data, err = _http.request_json(
        "control_plane", "GET", f"{config.CONTROL_PLANE_URL}/v1/approvals"
    )
    if err:
        return err
    if not data:
        return "No pending approvals."
    return "\n".join(f"- {a.get('model_id')}: {a.get('status')}" for a in data)


@tool
@confirmed_write(lambda model_id: f"Approve model change for {model_id} (fires a training run)")
def approve_model(model_id: str) -> str:
    """Approve the most recent pending change for a model, triggering a Prefect run.

    Args:
        model_id: Model identifier (e.g. 'jpcp').
    """
    data, err = _http.request_json(
        "control_plane", "POST", f"{config.CONTROL_PLANE_URL}/v1/approvals/{model_id}/approve"
    )
    if err:
        return err
    return f"Approved {model_id}: {data}"


@tool
@confirmed_write(
    lambda model_id, reason="": f"Reject model change for {model_id} (reason: {reason or 'none'})"
)
def reject_model(model_id: str, reason: str = "") -> str:
    """Reject the most recent pending change for a model without training.

    Args:
        model_id: Model identifier (e.g. 'jpcp').
        reason: Optional rejection reason.
    """
    data, err = _http.request_json(
        "control_plane",
        "POST",
        f"{config.CONTROL_PLANE_URL}/v1/approvals/{model_id}/reject",
        json={"reason": reason or None},
    )
    if err:
        return err
    return f"Rejected {model_id}: {data}"


TOOLS = [list_pending_approvals, approve_model, reject_model]
