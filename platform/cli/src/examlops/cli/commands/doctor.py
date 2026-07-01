from __future__ import annotations

import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

from rich.console import Console
from rich.table import Table

from examlops.cli import _output
from examlops.cli._config import CONFIG_PATH, load_config

_EXAMPLES = "Examples:\n\n  exa doctor\n\n  exa --json doctor"

console = Console()


def _ping(url: str, timeout: float = 4.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # 401/403/404 still means the service is alive
        if e.code < 500:
            return True, f"HTTP {e.code}"
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "refused" in reason.lower():
            return False, "connection refused"
        if "timed out" in reason.lower():
            return False, "timeout"
        return False, reason[:60]
    except Exception as e:
        return False, str(e)[:60]


def _db_check(path: str) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(path)
        conn.execute("SELECT 1")
        conn.close()
        return True, path
    except Exception as e:
        return False, str(e)[:80]


def doctor() -> None:
    """Diagnose your ExaMLOps setup: config, connectivity, and DB health."""
    cfg = load_config()
    issues: list[str] = []

    rows: list[tuple[str, bool, str, str]] = []

    def _row(label: str, ok: bool, detail: str, fix: str = "") -> None:
        rows.append((label, ok, detail, fix))
        if not ok:
            issues.append(fix or detail)

    # ── Config file ────────────────────────────────────────────────────────
    if CONFIG_PATH.exists():
        _row("Config file", True, str(CONFIG_PATH))
    else:
        _row(
            "Config file",
            False,
            "not found",
            f"Create {CONFIG_PATH} or set env vars. Run: exa config show",
        )

    # ── API token ──────────────────────────────────────────────────────────
    if cfg.control_plane_token:
        _row("API token", True, "configured (CONTROL_PLANE_TOKEN or config)")
    else:
        _row(
            "API token",
            False,
            "not set — write endpoints will return 503",
            "Set CONTROL_PLANE_TOKEN env var or: exa config set control_plane_token <token>",
        )

    # ── Services ───────────────────────────────────────────────────────────
    _SERVICE_CHECKS = [
        (
            "Control Plane",
            cfg.control_plane_url + "/health",
            "exa stack up  (or check make control-plane-up)",
        ),
        ("MLflow", cfg.mlflow_url + "/health", "exa stack up --service mlflow"),
        ("Prefect", cfg.prefect_url + "/api/health", "exa stack up --service prefect"),
        ("Ray Serve", cfg.ray_serve_url + "/health", "exa stack up --service ray-serving"),
        ("Dashboard", cfg.dashboard_url + "/health", "exa stack up --service dashboard"),
    ]
    for label, url, fix in _SERVICE_CHECKS:
        ok_val, detail = _ping(url)
        _row(label, ok_val, f"{url}  [{detail}]", f"{label} unreachable → {fix}")

    # ── Platform DB ────────────────────────────────────────────────────────
    db_path = os.getenv("PLATFORM_DB", str(Path(__file__).parents[6] / "platform.db"))
    db_ok, db_detail = _db_check(db_path)
    _row(
        "Platform DB",
        db_ok,
        db_detail,
        "Set PLATFORM_DB env var to the correct path" if not db_ok else "",
    )

    # ── Python version ─────────────────────────────────────────────────────
    ver = sys.version_info
    py_ok = ver >= (3, 12)
    _row(
        "Python",
        py_ok,
        f"{ver.major}.{ver.minor}.{ver.micro} ({sys.executable})",
        "Python 3.12+ required" if not py_ok else "",
    )

    # ── Docker ─────────────────────────────────────────────────────────────
    import shutil

    docker_ok = shutil.which("docker") is not None
    _row(
        "Docker",
        docker_ok,
        shutil.which("docker") or "not found",
        "Install Docker: https://docs.docker.com/get-docker/" if not docker_ok else "",
    )

    if _output.json_mode:
        _output.print_json(
            {
                "checks": [{"name": r[0], "ok": r[1], "detail": r[2], "fix": r[3]} for r in rows],
                "issues": len(issues),
            }
        )
        return

    table = Table(title="ExaMLOps Doctor", show_header=True, header_style="bold cyan")
    table.add_column("Check", style="bold")
    table.add_column("", width=2)
    table.add_column("Detail")

    for label, ok_val, detail, _ in rows:
        icon = "[green]✓[/green]" if ok_val else "[red]✗[/red]"
        detail_style = "" if ok_val else "[dim red]"
        detail_end = "[/dim red]" if not ok_val else ""
        table.add_row(label, icon, f"{detail_style}{detail}{detail_end}")

    console.print()
    console.print(table)

    if not issues:
        console.print("\n[green]✓ All checks passed — your setup looks healthy![/green]\n")
    else:
        console.print(
            f"\n[yellow]⚠ {len(issues)} issue{'s' if len(issues) != 1 else ''} found:[/yellow]"
        )
        for issue in issues:
            if issue:
                console.print(f"  [dim]→ {issue}[/dim]")
        console.print()
