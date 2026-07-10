"""
``exa hpc`` — HPC fleet discovery (read-only).

Auto-detect the scheduler (Flux / Slurm / unmanaged) on a candidate login node and
enumerate its compute resources — nodes, CPUs, memory, GPUs, and online/allocated/down
state — via side-effect-free commands (``flux resource``, ``sinfo``, ``scontrol``,
``nvidia-smi``). Nothing here submits, cancels, or connects a workload: discovery only
*proposes* a configuration. Turning a discovered cluster into one exaMLOps will schedule on
is the registry + sysadmin-approval step (``exa hpc connect`` / ``approve``, Phase 35b).

The heavy lifting lives in ``platform/infra/slurm-adapter/discovery.py`` (pluggable probes);
this module is the thin CLI/persistence shell over it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from examlops.cli import _output

app = typer.Typer(
    help="HPC fleet discovery — auto-detect scheduler + enumerate nodes/GPUs (read-only).",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa hpc detect lxp-login          # probe a host, suggest scheduler config\n\n"
    "  exa hpc detect                    # probe the local machine (or env transport)\n\n"
    "  exa hpc nodes --host lxp-login    # list nodes + state\n\n"
    "  exa hpc nodes --save --cluster lxp   # persist inventory snapshot to platform.db\n\n"
    "  exa hpc gpus --host lxp-gpu01     # GPU devices via nvidia-smi\n\n"
    "  exa --json hpc detect lxp-login"
)


def _adapter_dir() -> Path:
    """Locate ``platform/infra/slurm-adapter`` from the installed CLI package."""
    root = Path(__file__).resolve().parents[6]
    return root / "platform" / "infra" / "slurm-adapter"


def _load_discovery():
    """Put the flat slurm-adapter modules on sys.path and import discovery + executors."""
    adapter = str(_adapter_dir())
    if adapter not in sys.path:
        sys.path.insert(0, adapter)
    import discovery  # type: ignore  # noqa: PLC0415
    from executor import LocalExecutor, SSHExecutor  # type: ignore  # noqa: PLC0415

    return discovery, LocalExecutor, SSHExecutor


def _build_executor(host, user, key, port):
    """Build a transport: SSH when a host is given, else local subprocess."""
    _discovery, LocalExecutor, SSHExecutor = _load_discovery()
    if host and host.lower() not in ("local", "localhost"):
        return SSHExecutor(host=host, user=user, key_path=key, port=port)
    return LocalExecutor()


def _suggested_config(caps: dict, host: str | None) -> dict:
    """Turn discovered capabilities into a proposed clusters.yaml / env snippet."""
    scheduler = caps.get("scheduler")
    return {
        "scheduler": scheduler,
        "transport": "ssh" if host else "local",
        "host": host,
        "env": {
            "EXAMLOPS_HPC_SCHEDULER": scheduler if scheduler in ("flux", "slurm") else "mock",
            **({"EXAMLOPS_HPC_TRANSPORT": "ssh", "EXAMLOPS_HPC_SSH_HOST": host} if host else {}),
        },
    }


@app.command(epilog=_EXAMPLES)
def detect(
    host: str = typer.Argument(None, help="Login-node host to probe over SSH (omit = local)"),
    user: str = typer.Option(None, "--user", "-u", help="SSH user"),
    key: str = typer.Option(None, "--key", "-k", help="SSH private-key path"),
    port: int = typer.Option(22, "--port", "-p", help="SSH port"),
):
    """Auto-detect the scheduler on a host and suggest a configuration (read-only)."""
    discovery, *_ = _load_discovery()
    executor = _build_executor(host, user, key, port)
    try:
        caps = discovery.probe_scheduler(executor).to_dict()
    finally:
        _safe_close(executor)

    suggested = _suggested_config(caps, host)
    if _output.json_mode:
        _output.print_json({"host": host or "local", "capabilities": caps, "suggested": suggested})
        return

    target = host or "local"
    if not caps.get("available"):
        _output.warning(f"No known scheduler detected on {target} (scheduler={caps.get('scheduler')})")
    else:
        _output.ok(f"Detected [bold]{caps['scheduler']}[/bold] on {target}")
    _output.print_record(
        {
            "Scheduler": caps.get("scheduler"),
            "Version": caps.get("version") or "—",
            "Accounting": "yes" if caps.get("has_accounting") else "no",
            "GPUs present": "yes" if caps.get("has_gpu") else "no",
            "Nodes": caps.get("total_nodes"),
            "CPUs (total)": caps.get("total_cpus"),
            "GPUs (total)": caps.get("total_gpus"),
        }
    )
    for note in caps.get("notes", []):
        _output.detail(f"note: {note}")
    _output.info("Suggested config (nothing connected — approve via 'exa hpc connect' later):")
    env = suggested["env"]
    for k, v in env.items():
        _output.detail(f"  {k}={v}")


@app.command(epilog=_EXAMPLES)
def nodes(
    host: str = typer.Option(None, "--host", "-H", help="Login-node host (omit = local)"),
    user: str = typer.Option(None, "--user", "-u", help="SSH user"),
    key: str = typer.Option(None, "--key", "-k", help="SSH private-key path"),
    port: int = typer.Option(22, "--port", "-p", help="SSH port"),
    scheduler: str = typer.Option(
        None, "--scheduler", "-s", help="Force a probe (flux|slurm|nvidia-smi); default auto"
    ),
    save: bool = typer.Option(False, "--save", help="Persist the inventory snapshot to platform.db"),
    cluster: str = typer.Option("default", "--cluster", "-c", help="Cluster name for --save"),
):
    """List compute nodes with CPUs/memory/GPUs and normalized state (read-only)."""
    discovery, *_ = _load_discovery()
    executor = _build_executor(host, user, key, port)
    try:
        inv = discovery.discover_inventory(executor, scheduler=scheduler)
    except ValueError as exc:
        _output.error(str(exc))
        return
    finally:
        _safe_close(executor)

    node_rows = inv["nodes"]
    sched = inv["capabilities"].get("scheduler")

    if save:
        from examlops.platform_db import init_db, record_node_snapshot

        init_db()
        n = record_node_snapshot(cluster, sched or "unknown", node_rows)
        _output.ok(f"Saved snapshot: {n} nodes for cluster '{cluster}' ({sched})")

    if _output.json_mode:
        _output.print_json(inv)
        return

    if not node_rows:
        _output.warning(f"No nodes discovered (scheduler={sched}). Is the host reachable?")
        return
    cols = ["Node", "State", "CPUs", "Mem (MB)", "GPUs", "GPU model", "Partition"]
    table = [
        [
            n["name"],
            n["state"],
            n["cpus"] if n["cpus"] is not None else "—",
            n["memory_mb"] if n["memory_mb"] is not None else "—",
            n["gpus"],
            n["gpu_model"] or "—",
            n["partition"] or "—",
        ]
        for n in node_rows
    ]
    _output.print_table(f"HPC nodes ({sched})", cols, table)


@app.command(epilog=_EXAMPLES)
def gpus(
    host: str = typer.Option(None, "--host", "-H", help="Host to probe (omit = local)"),
    user: str = typer.Option(None, "--user", "-u", help="SSH user"),
    key: str = typer.Option(None, "--key", "-k", help="SSH private-key path"),
    port: int = typer.Option(22, "--port", "-p", help="SSH port"),
    scheduler: str = typer.Option(None, "--scheduler", "-s", help="Force a probe; default auto"),
):
    """List GPU devices — model, memory, utilization, online status (read-only)."""
    discovery, *_ = _load_discovery()
    executor = _build_executor(host, user, key, port)
    try:
        inv = discovery.discover_inventory(executor, scheduler=scheduler)
    except ValueError as exc:
        _output.error(str(exc))
        return
    finally:
        _safe_close(executor)

    gpu_rows = inv["gpus"]
    if _output.json_mode:
        _output.print_json({"capabilities": inv["capabilities"], "gpus": gpu_rows})
        return

    if not gpu_rows:
        _output.info("No GPUs discovered (0 enrolled, or nvidia-smi unavailable on this host).")
        return
    cols = ["Node", "Idx", "Model", "Mem (MB)", "Used (MB)", "Util %", "State"]
    table = [
        [
            g["node"],
            g["index"] if g["index"] is not None else "—",
            g["model"] or "—",
            g["memory_mb"] if g["memory_mb"] is not None else "—",
            g["used_memory_mb"] if g["used_memory_mb"] is not None else "—",
            g["utilization_pct"] if g["utilization_pct"] is not None else "—",
            g["state"],
        ]
        for g in gpu_rows
    ]
    _output.print_table("GPU devices", cols, table)


def _safe_close(executor) -> None:
    try:
        executor.close()
    except Exception:  # noqa: BLE001 - best-effort teardown
        pass
