from __future__ import annotations

import typer

from examlops import control_plane_api
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


def _checked_address(key: str, svc: dict, cfg) -> str:
    """The address the health check actually used.

    The control plane reports it per service (`url`). Older deployments do not, and rather than
    substitute the host port map — the thing that was wrong in the first place — say so: an
    unknown address is `?`, not a guess. The control plane is the one service `exa` probes
    itself, so its address is the configured one.
    """
    if key == "control_plane":
        return str(cfg.control_plane_url)
    url = svc.get("url")
    if not url or url == "self":
        return "?"
    return str(url)


def _hardware_profile_summary() -> dict | None:
    """Profiles in use and the status each resolved to (ADR 0157 Phase 4), or ``None``.

    Read from the local platform datastore, not the control plane. ``None`` means the read
    failed — distinct from an empty report, which means nothing is sized by a profile. A status
    line must never turn an unreadable ledger into the silence that means "all fine".
    """
    try:
        from examlops.hardware_profiles import in_use_report  # noqa: PLC0415

        report = in_use_report()
    except Exception:  # noqa: BLE001 - a status page must render with a broken datastore
        return None
    return {
        "in_use": len(report["entries"]),
        "counts": report["counts"],
        "attention": report["attention"],
        "truncated": report.get("truncated", False),
    }


def _render_hardware_profiles() -> None:
    summary = _hardware_profile_summary()
    if summary is None:
        _output.warning("hardware profile usage unknown — the platform datastore could not be read")
        return
    if not summary["in_use"]:
        return  # nothing sized by a profile: no line, as for any unused feature
    attention = summary["attention"]
    if not attention:
        _output.ok(f"{summary['in_use']} hardware profile use(s), none degraded or unresolvable")
        return
    _output.print_table(
        "Hardware Profiles Needing Attention",
        ["Consumer", "Ref", "Profile", "Status", "Reason"],
        [
            [
                e["consumer"],
                e["consumer_ref"],
                f"{e['name']} v{e['version']}",
                e["status"],
                e["reason"],
            ]
            for e in attention
        ],
    )
    _output.warning("run: exa hardware profile in-use")


def _render_status(cfg, watch: bool) -> None:
    from examlops import sdk

    with _output.spinner("Checking platform health…"):
        snapshot = sdk.status()
    # Render through the SDK snapshot; `.raw` preserves the exact payload the renderer expects.
    data = snapshot.raw if snapshot.reachable else None

    if _output.json_mode:
        if data:
            # The control plane's payload does not carry production models; the snapshot resolves
            # them from the registry. Emitting `raw` alone made `--json` and the table disagree
            # about what the command had found.
            _output.print_json(
                {
                    **data,
                    "production_models": snapshot.production_models,
                    "suspend": _suspend_report(),
                    "hardware_profiles": _hardware_profile_summary(),
                }
            )
        else:
            _output.print_json({"error": "control_plane_unreachable", "suspend": _suspend_report()})
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
    n_unreported = 0
    for key in _SERVICE_ORDER:
        svc = services.get(key)
        label = _SERVICE_LABELS.get(key, key)
        checked = _checked_address(key, svc or {}, cfg)
        if svc is None:
            # An older control plane that does not report this service has not reported it *down*.
            # Rendering the absence as `✗ unreachable` sends a reader to debug a service that may
            # be fine, and counts it into the "run: exa doctor" warning.
            n_unreported += 1
            rows.append([label, "[dim]— not reported[/dim]", "[dim]—[/dim]"])
            continue
        svc_ok = svc.get("ok", False)
        if not svc_ok:
            n_down += 1
            cell = "[red]✗ unreachable[/red]"
        elif key == "ray_serve":
            loaded = svc.get("models", [])
            cell = f"[green]✓ ok[/green]  [dim]{len(loaded)} model(s) loaded[/dim]"
        elif key == "prefect":
            runs = svc.get("active_runs", None)
            extra = f"  [dim]{runs} active run(s)[/dim]" if runs is not None else ""
            cell = f"[green]✓ ok[/green]{extra}"
        else:
            cell = "[green]✓ ok[/green]"
        rows.append([label, cell, f"[dim]{checked}[/dim]"])

    _output.console.print()
    _output.print_table("ExaMLOps Service Health", ["Service", "Status", "Checked"], rows)

    if n_down:
        _output.warning(
            f"{n_down} service{'s' if n_down != 1 else ''} unreachable — run: exa doctor"
        )
    if n_unreported:
        _output.warning(
            f"{n_unreported} service{'s' if n_unreported != 1 else ''} not reported by the control "
            "plane — its /status is older than this client, so their health is unknown"
        )

    # ── Pending approvals ──────────────────────────────────────────────────
    pending_count = data.get("pending_approvals", 0)
    if pending_count is None:
        # Not the same as zero. The control plane could not read the approval store, and the line
        # below prints nothing for a falsy count — so treating unknown as 0 renders a broken queue
        # as the silence that means "nothing waiting".
        _output.warning(
            "pending approvals unknown — the control plane could not read the approval store; "
            "run: exa doctor"
        )
    elif pending_count:
        _output.warning(
            f"{pending_count} pending approval{'s' if pending_count != 1 else ''} — run: exa approvals list"
        )
        try:
            pending = control_plane_api.list_approvals(
                status="pending", base=cfg.control_plane_url, token=cfg.control_plane_token
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
    # Three outcomes, and the old code could express only one of them. It read a key the control
    # plane has never returned, so the list was always empty, and an empty list printed *nothing* —
    # which reads as "no models are in production" to anyone looking at the screen.
    models_list = snapshot.production_models
    if models_list is None:
        _output.warning(
            "production models unknown — the model registry could not be read; run: exa doctor"
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
        _output.print_table("Production Models", ["Model", "Production", "Staging"], rows_m)
    elif not models_list:
        _output.ok("No model carries a Production or Staging alias")
    else:
        _output.print_table(
            "Production Models",
            ["Model", "Production Version", "Staging Version"],
            [
                [
                    (m if isinstance(m, str) else (m.get("name") or "—")),
                    (m.get("production_version") or "—") if isinstance(m, dict) else "—",
                    (m.get("staging_version") or "—") if isinstance(m, dict) else "—",
                ]
                for m in models_list
            ],
        )

    _render_suspend()

    # ── Hardware profiles in use (ADR 0157 Phase 4) ───────────────────────────
    _render_hardware_profiles()

    _output.console.print()


def _suspend_report() -> dict:
    """ADR 0109: the suspend seam's capability and restore timing split, read locally.

    Local on purpose — it is this platform's own record (``suspend_snapshots``), not something the
    control plane owns — and fail-open, so the seam can never take ``exa status`` down.
    """
    try:
        from examlops.suspend.report import seam_report

        return seam_report()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"suspend seam unavailable: {exc}"}


def _secs(value) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def _render_suspend() -> None:
    report = _suspend_report()
    if "error" in report:
        _output.warning(str(report["error"]))
        return
    rows = []
    for b in report.get("backends", []):
        if "error" in b:
            rows.append([b.get("backend"), "[red]error[/red]", str(b["error"])[:40], "", "", ""])
            continue
        cap, timing, pre = b["capability"], b.get("timing") or {}, b["preemption"]
        name = cap["backend"] + (" *" if cap["backend"] == report.get("selected_backend") else "")
        rows.append(
            [
                name,
                cap["granularity"],
                ",".join(cap["tiers"]) or "—",
                "[green]yes[/green]" if pre["can_promise"] else "[yellow]no[/yellow]",
                str(timing.get("restores", "?")),
                f"{_secs(timing.get('state_transfer_s_median'))} / "
                f"{_secs(timing.get('communicator_rebuild_s_median'))}",
            ]
        )
    _output.print_table(
        "Suspend/Resume Seam (ADR 0109; * = selected)",
        ["Backend", "Granularity", "Tiers", "Preemption", "Restores", "Transfer / Rebuild s"],
        rows,
    )
