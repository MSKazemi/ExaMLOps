from __future__ import annotations

import typer

from examlops.cli import _client, _output
from examlops.cli._config import load_config

_EXAMPLES = "Examples:\n\n  exa status\n\n  exa --json status"

_SERVICE_ORDER = ["control_plane", "mlflow", "prefect", "ray_serve", "dashboard"]
_SERVICE_LABELS = {
    "control_plane": "Control Plane",
    "mlflow": "MLflow",
    "prefect": "Prefect",
    "ray_serve": "Ray Serve",
    "dashboard": "Dashboard",
}
_SERVICE_URLS = {
    "control_plane": ":18002",
    "mlflow": ":15000",
    "prefect": ":14200",
    "ray_serve": ":18001",
    "dashboard": ":18099",
}


def status(
    watch: bool = typer.Option(
        False, "--watch", "-w", help="Live auto-refreshing view (Ctrl-C to exit)"
    ),
    interval: int = typer.Option(5, "--interval", help="Refresh interval in seconds for --watch"),
) -> None:
    """Platform snapshot: service health, pending approvals, production models."""
    cfg = load_config()

    if watch and not _output.json_mode:
        _output.watch_loop(lambda: _render_status(cfg, watch=True), interval)
        return
    _render_status(cfg, watch=False)


def _render_status(cfg, watch: bool) -> None:
    with _output.spinner("Checking platform health…"):
        try:
            data = _client.get(f"{cfg.control_plane_url}/status", token=cfg.control_plane_token)
        except _client.ClientError:
            data = None

    if _output.json_mode:
        if data:
            _output.print_json(data)
        else:
            _output.print_json({"error": "control_plane_unreachable"})
        return

    if data is None:
        # In watch mode, keep the loop alive instead of exiting the process.
        if watch:
            _output.warning("Control Plane unreachable — retrying on next refresh.")
            return
        _output.error(
            "Control Plane unreachable. Is the stack running?",
            hint="Try: exa stack status  or  exa stack up",
        )
        return

    services = data.get("services", {})
    rows = []
    n_down = 0
    for key in _SERVICE_ORDER:
        svc = services.get(key, {})
        svc_ok = svc.get("ok", False)
        label = _SERVICE_LABELS.get(key, key)
        port = _SERVICE_URLS.get(key, "")
        if not svc_ok:
            n_down += 1
            cell = f"[red]✗ unreachable[/red]  [dim]{port}[/dim]"
        elif key == "ray_serve":
            loaded = svc.get("models", [])
            cell = f"[green]✓ ok[/green]  [dim]{len(loaded)} model(s) loaded  {port}[/dim]"
        elif key == "control_plane":
            cell = f"[green]✓ ok[/green]  [dim]{port}[/dim]"
        elif key == "prefect":
            runs = svc.get("active_runs", None)
            extra = f"  {runs} active run(s)" if runs is not None else ""
            cell = f"[green]✓ ok[/green]  [dim]{extra}  {port}[/dim]"
        else:
            cell = f"[green]✓ ok[/green]  [dim]{port}[/dim]"
        rows.append([label, cell])

    _output.console.print()
    _output.print_table("ExaMLOps Service Health", ["Service", "Status"], rows)

    if n_down:
        _output.warning(
            f"{n_down} service{'s' if n_down != 1 else ''} unreachable — run: exa doctor"
        )

    # ── Pending approvals ──────────────────────────────────────────────────
    pending_count = data.get("pending_approvals", 0)
    if pending_count:
        _output.warning(
            f"{pending_count} pending approval{'s' if pending_count != 1 else ''} — run: exa approvals list"
        )
        try:
            pending = _client.get(
                f"{cfg.control_plane_url}/approvals?status=pending",
                token=cfg.control_plane_token,
            )
            _output.print_table(
                "Pending Approvals",
                ["Model", "Commit", "Message", "Requested"],
                [
                    [
                        p["model_id"],
                        (p.get("commit_sha") or "")[:8],
                        (p.get("commit_msg") or "")[:50],
                        (p.get("requested_at") or "")[:16],
                    ]
                    for p in pending
                ],
            )
        except _client.ClientError:
            pass
    else:
        _output.ok("No pending approvals")

    # ── Production models ──────────────────────────────────────────────────
    models_list = data.get("production_models") or data.get("models") or []
    if models_list and isinstance(models_list, list) and models_list:
        _output.print_table(
            "Production Models",
            ["Model", "Production Version", "Staging Version"],
            [
                [
                    (m if isinstance(m, str) else (m.get("name") or "—")),
                    m.get("production_version", "—") if isinstance(m, dict) else "—",
                    m.get("staging_version", "—") if isinstance(m, dict) else "—",
                ]
                for m in models_list
            ],
        )
    elif isinstance(models_list, dict):
        # Some responses return a dict {name: {aliases: ...}}
        rows_m = []
        for name, meta in models_list.items():
            if isinstance(meta, dict):
                aliases = meta.get("aliases", {})
                rows_m.append([name, aliases.get("Production", "—"), aliases.get("Staging", "—")])
            else:
                rows_m.append([name, "—", "—"])
        if rows_m:
            _output.print_table("Production Models", ["Model", "Production", "Staging"], rows_m)

    _output.console.print()
