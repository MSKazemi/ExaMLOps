from __future__ import annotations

from examlops.cli import _client, _output
from examlops.cli._config import load_config

_EXAMPLES = (
    "Examples:\n\n"
    "  exa status\n\n"
    "  exa --json status"
)


def status():
    """Platform snapshot: service health, pending approvals, production models."""
    cfg = load_config()

    cp_ok, cp_pending = False, 0
    try:
        h = _client.get(f"{cfg.control_plane_url}/health")
        cp_ok = h.get("status") == "ok"
        cp_pending = h.get("pending_approvals", 0)
    except _client.ClientError:
        pass

    ray_models: list[str] = []
    ray_reachable = False
    try:
        ms = _client.get(f"{cfg.ray_serve_url}/models")
        ray_models = [m.get("name", m) if isinstance(m, dict) else m for m in ms]
        ray_reachable = True
    except _client.ClientError:
        pass

    pending: list[dict] = []
    try:
        pending = _client.get(f"{cfg.control_plane_url}/approvals?status=pending")
    except _client.ClientError:
        pass

    if _output.json_mode:
        _output.print_json({
            "control_plane": {"ok": cp_ok, "pending_approvals": cp_pending},
            "ray_serve": {"models": ray_models},
            "pending_approvals": pending,
        })
        return

    _output.print_table(
        "Service Health",
        ["Service", "Status"],
        [
            ["Control Plane", "✓ ok" if cp_ok else "✗ unreachable"],
            ["Ray Serve", f"✓ {len(ray_models)} model(s)" if ray_reachable else "✗ unreachable"],
        ],
    )
    if cp_pending:
        _output.print_table(
            f"Pending Approvals ({cp_pending})",
            ["Model", "Commit", "Message"],
            [[p["model_id"], (p.get("commit_sha") or "")[:8], p.get("commit_msg") or ""] for p in pending],
        )
    else:
        _output.ok("No pending approvals")
