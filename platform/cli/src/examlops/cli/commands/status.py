from __future__ import annotations

from examlops.cli import _client, _output
from examlops.cli._config import load_config

_EXAMPLES = (
    "Examples:\n\n"
    "  exa status\n\n"
    "  exa --json status"
)

_SERVICE_ORDER = ["control_plane", "mlflow", "prefect", "ray_serve", "dashboard"]
_SERVICE_LABELS = {
    "control_plane": "Control Plane",
    "mlflow":        "MLflow",
    "prefect":       "Prefect",
    "ray_serve":     "Ray Serve",
    "dashboard":     "Dashboard",
}


def status():
    """Platform snapshot: service health, pending approvals, production models."""
    cfg = load_config()

    try:
        data = _client.get(f"{cfg.control_plane_url}/status")
    except _client.ClientError:
        if _output.json_mode:
            _output.print_json({"error": "control_plane_unreachable"})
        else:
            _output.print_table(
                "Service Health",
                ["Service", "Status"],
                [["Control Plane", "✗ unreachable"]],
            )
        return

    if _output.json_mode:
        _output.print_json(data)
        return

    services = data.get("services", {})
    rows = []
    for key in _SERVICE_ORDER:
        svc = services.get(key, {})
        ok = svc.get("ok", False)
        label = _SERVICE_LABELS.get(key, key)
        if key == "ray_serve" and ok:
            models = svc.get("models", [])
            cell = f"✓ {len(models)} model(s)"
        else:
            cell = "✓ ok" if ok else "✗ unreachable"
        rows.append([label, cell])

    _output.print_table("Service Health", ["Service", "Status"], rows)

    pending_count = data.get("pending_approvals", 0)
    if pending_count:
        pending = []
        try:
            pending = _client.get(f"{cfg.control_plane_url}/approvals?status=pending")
        except _client.ClientError:
            pass
        _output.print_table(
            f"Pending Approvals ({pending_count})",
            ["Model", "Commit", "Message"],
            [[p["model_id"], (p.get("commit_sha") or "")[:8], p.get("commit_msg") or ""] for p in pending],
        )
    else:
        _output.ok("No pending approvals")
