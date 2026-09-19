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

import hashlib
import os
import sys
from pathlib import Path

import typer

from examlops.cli import _output
from examlops.cli._help import make_ordered_group

# Help panels for `exa hpc` (the gpu-share sub-group is attached in main.py).
_PANELS: list[tuple[str, list[str]]] = [
    ("Discovery", ["detect", "nodes", "gpus", "capacity", "prometheus-sd"]),
    ("Registry & Approval", ["connect", "clusters", "approve", "reject"]),
    ("Placement & Jobs", ["place", "queue", "jobs", "preflight", "gpu-share"]),
]

app = typer.Typer(
    cls=make_ordered_group(_PANELS),
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
        _output.warning(
            f"No known scheduler detected on {target} (scheduler={caps.get('scheduler')})"
        )
    else:
        _output.ok(f"Detected {caps['scheduler']} on {target}")
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
    save: bool = typer.Option(
        False, "--save", help="Persist the inventory snapshot to platform.db"
    ),
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
        from examlops.data import init_db
        from examlops.data.hpc import record_node_snapshot

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


# ── registry + approval-gated connect (Phase 35b) ─────────────────────────────────


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def _key_fingerprint(key_path: str | None) -> str | None:
    """Best-effort SHA256 fingerprint of a public key (from a private-key path)."""
    if not key_path:
        return None
    pub = (
        Path(key_path)
        .expanduser()
        .with_suffix(Path(key_path).suffix + ".pub" if Path(key_path).suffix else ".pub")
    )
    try:
        if pub.exists():
            blob = pub.read_text().split()[1]
            return "SHA256:" + hashlib.sha256(blob.encode()).hexdigest()[:32]
    except Exception:  # noqa: BLE001 - fingerprinting is advisory only
        return None
    return None


_CONNECT_EXAMPLES = (
    "Examples:\n\n"
    "  exa hpc connect lxp-login --name lxp\n\n"
    "  exa hpc connect lxp-login --name lxp --user hpcuser --key ~/.ssh/id_ed25519\n\n"
    "  exa hpc clusters\n\n"
    "  exa hpc approve lxp        # sysadmin: authorize scheduling on the cluster\n\n"
    '  exa hpc reject lxp --reason "wrong account"'
)


@app.command(epilog=_CONNECT_EXAMPLES)
def connect(
    host: str = typer.Argument(..., help="Login-node host to probe and register"),
    name: str = typer.Option(None, "--name", "-n", help="Cluster name (default: host)"),
    user: str = typer.Option(None, "--user", "-u", help="SSH user"),
    key: str = typer.Option(None, "--key", "-k", help="SSH private-key path"),
    port: int = typer.Option(22, "--port", "-p", help="SSH port"),
    scheduler: str = typer.Option(None, "--scheduler", "-s", help="Force scheduler; default auto"),
):
    """Probe a host and register it as a PENDING cluster (requires approval to use)."""
    discovery, *_ = _load_discovery()
    from examlops.data.audit import write_audit_event
    from examlops.hpc_registry import register_pending

    cluster = name or host
    executor = _build_executor(host, user, key, port)
    try:
        caps = (
            discovery._PROBES[scheduler].capabilities(executor)
            if scheduler
            else discovery.probe_scheduler(executor)
        ).to_dict()
    except KeyError:
        _output.error(f"unknown scheduler probe: {scheduler!r}")
        return
    finally:
        _safe_close(executor)

    fingerprint = _key_fingerprint(key)
    register_pending(
        cluster,
        caps.get("scheduler") or "unknown",
        transport="ssh",
        host=host,
        ssh_user=user,
        ssh_port=port,
        ssh_key=key,
        key_fingerprint=fingerprint,
        capabilities=caps,
        requested_by=_actor(),
    )
    write_audit_event(
        "exa-hpc",
        _actor(),
        "cluster_connect_requested",
        cluster,
        {"host": host, "scheduler": caps.get("scheduler"), "fingerprint": fingerprint},
    )

    if _output.json_mode:
        _output.print_json(
            {"cluster": cluster, "state": "PENDING", "host": host, "capabilities": caps}
        )
        return
    _output.ok(f"Registered cluster {cluster} (state: PENDING)")
    _output.detail(
        f"  scheduler: {caps.get('scheduler')}  host: {host}  fingerprint: {fingerprint}"
    )
    _output.info(f"No jobs will run here until approved. Sysadmin: exa hpc approve {cluster}")


@app.command(epilog=_CONNECT_EXAMPLES)
def clusters():
    """List registered clusters and their approval state."""
    from examlops.hpc_registry import list_clusters

    rows = list_clusters()
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No clusters registered. Add one with 'exa hpc connect <host> --name <n>'.")
        return
    cols = ["Name", "Scheduler", "Transport", "Host", "State", "Approved by"]
    table = [
        [
            c["name"],
            c["scheduler"] or "—",
            c["transport"] or "—",
            c["host"] or "—",
            c["state"],
            c["approved_by"] or "—",
        ]
        for c in rows
    ]
    _output.print_table("HPC clusters", cols, table)


@app.command(epilog=_CONNECT_EXAMPLES)
def approve(
    name: str = typer.Argument(..., help="Cluster name to approve"),
):
    """Sysadmin: approve a cluster so exaMLOps may schedule jobs on it."""
    from examlops.data.audit import write_audit_event
    from examlops.data.hpc import set_cluster_state
    from examlops.hpc_registry import get_merged

    merged = get_merged(name)
    if merged is None:
        _output.error(f"Unknown cluster: {name}", hint="exa hpc clusters")
        return
    if not _output.confirm(
        f"Approve cluster '{name}' ({merged['scheduler']} @ {merged['host']}) for scheduling?"
    ):
        _output.info("Aborted — cluster left unchanged.")
        return
    set_cluster_state(name, "ACTIVE", approved_by=_actor())
    write_audit_event("exa-hpc", _actor(), "cluster_approved", name, {"host": merged.get("host")})
    _output.ok(f"Cluster {name} is now ACTIVE — jobs may be scheduled on it.")


@app.command(epilog=_CONNECT_EXAMPLES)
def reject(
    name: str = typer.Argument(..., help="Cluster name to reject"),
    reason: str = typer.Option(None, "--reason", "-r", help="Why the cluster is rejected"),
):
    """Sysadmin: reject a cluster (blocks scheduling; auditable)."""
    from examlops.data.audit import write_audit_event
    from examlops.data.hpc import set_cluster_state
    from examlops.hpc_registry import get_merged

    merged = get_merged(name)
    if merged is None:
        _output.error(f"Unknown cluster: {name}", hint="exa hpc clusters")
        return
    set_cluster_state(name, "REJECTED", approved_by=_actor(), reason=reason)
    write_audit_event("exa-hpc", _actor(), "cluster_rejected", name, {"reason": reason})
    _output.ok(f"Cluster {name} is now REJECTED — scheduling blocked.")


# ── placement, queue, preflight (Phase 35c) ───────────────────────────────────────


def _executor_for_cluster(name: str):
    """Build a transport for an ACTIVE cluster from its registry definition."""
    from examlops.hpc_registry import require_active

    merged = require_active(name)  # raises ClusterNotActiveError if not approved
    host = merged.get("host")
    if (merged.get("transport") or "ssh") == "ssh" and host:
        return _build_executor(
            host, merged.get("ssh_user"), merged.get("ssh_key"), int(merged.get("ssh_port") or 22)
        ), merged
    return _build_executor(None, None, None, 22), merged


_PLACE_EXAMPLES = (
    "Examples:\n\n"
    "  exa hpc place --gpus 4               # which ACTIVE cluster should run this?\n\n"
    "  exa hpc queue --cluster lxp          # live scheduler queue\n\n"
    "  exa hpc jobs --model JPCP            # tracked submissions from platform.db\n\n"
    "  exa hpc preflight lxp --gpus 4       # fail-fast pre-submit checks"
)


@app.command(epilog=_PLACE_EXAMPLES)
def place(
    gpus: int = typer.Option(0, "--gpus", "-g", help="GPUs the job needs"),
    cpus: int = typer.Option(0, "--cpus", help="CPUs the job needs"),
    nodes: int = typer.Option(1, "--nodes", "-N", help="Nodes the job needs"),
    provider: str | None = typer.Option(
        None, "--placement-provider", help="Placement scoring provider (default: least-loaded)"
    ),
):
    """Show which ACTIVE cluster placement would choose for a resource ask."""
    from examlops import sdk

    result = sdk.place(gpus=gpus, cpus=cpus, nodes=nodes, provider=provider)
    if _output.json_mode:
        _output.print_json(result.to_dict())
        return
    if result.cluster is None:
        _output.warning(result.reason)
    else:
        _output.ok(result.reason)
    if result.candidates:
        cols = ["Cluster", "Scheduler", "Fits", "Idle GPUs", "Idle nodes", "Score"]
        table = [
            [
                c["name"],
                c["scheduler"] or "—",
                "yes" if c["fits"] else "no",
                f"{c['idle_gpus']}/{c['total_gpus']}",
                f"{c['idle_nodes']}/{c['total_nodes']}",
                "—" if c["score"] == float("-inf") else c["score"],
            ]
            for c in result.candidates
        ]
        _output.print_table("Placement candidates", cols, table)


@app.command(epilog=_PLACE_EXAMPLES)
def queue(
    cluster: str = typer.Option(None, "--cluster", "-c", help="ACTIVE cluster to query"),
):
    """Show the live scheduler queue for an ACTIVE cluster (read-only)."""
    from examlops.hpc_registry import ClusterNotActiveError, default_cluster

    discovery, *_ = _load_discovery()
    name = cluster or default_cluster()
    if not name:
        _output.error(
            "No cluster given", hint="exa hpc queue --cluster <name>  (or set EXAMLOPS_HPC_CLUSTER)"
        )
        return
    try:
        executor, merged = _executor_for_cluster(name)
    except ClusterNotActiveError as exc:
        _output.error(str(exc))
        return
    try:
        jobs = discovery.queue_jobs(executor, merged.get("scheduler"))
    finally:
        _safe_close(executor)

    if _output.json_mode:
        _output.print_json({"cluster": name, "jobs": jobs})
        return
    if not jobs:
        _output.info(f"No jobs in the queue on '{name}' (or scheduler not queryable).")
        return
    cols = ["Job", "Name", "User", "State", "Nodes"]
    table = [
        [
            j["job_id"],
            j["name"],
            j["user"],
            j["state"],
            j["nodes"] if j["nodes"] is not None else "—",
        ]
        for j in jobs
    ]
    _output.print_table(f"Queue — {name} ({merged.get('scheduler')})", cols, table)


@app.command(epilog=_PLACE_EXAMPLES)
def jobs(
    model: str = typer.Option(None, "--model", "-m", help="Filter by model"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max rows"),
):
    """List tracked HPC submissions from platform.db (hpc_jobs)."""
    from examlops.data import init_db
    from examlops.data.hpc import get_hpc_jobs

    init_db()
    rows = get_hpc_jobs(model)[:limit]
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No HPC jobs tracked yet.")
        return
    cols = ["Job", "Scheduler", "Model", "State", "GPUs", "Submitted"]
    table = [
        [
            r["job_id"],
            r["scheduler"],
            r["model"],
            r["state"],
            r["gpus"] if r["gpus"] is not None else "—",
            (r["submit_time"] or "")[:16],
        ]
        for r in rows
    ]
    _output.print_table("Tracked HPC jobs", cols, table)


@app.command(epilog=_PLACE_EXAMPLES)
def preflight(
    cluster: str = typer.Argument(..., help="ACTIVE cluster to check"),
    gpus: int = typer.Option(0, "--gpus", "-g", help="GPUs the job will request"),
    nodes: int = typer.Option(1, "--nodes", "-N", help="Nodes the job will request"),
):
    """Fail-fast pre-submit checks against a cluster (exit 1 on any failure)."""
    from examlops.hpc_registry import ClusterNotActiveError

    discovery, *_ = _load_discovery()
    try:
        executor, merged = _executor_for_cluster(cluster)
    except ClusterNotActiveError as exc:
        _output.error(str(exc))
        raise typer.Exit(code=1) from exc
    try:
        checks = discovery.preflight(
            executor, merged.get("scheduler"), {"gpus": gpus, "nodes": nodes}
        )
    finally:
        _safe_close(executor)

    all_ok = all(c["ok"] for c in checks)
    if _output.json_mode:
        _output.print_json({"cluster": cluster, "ok": all_ok, "checks": checks})
    else:
        for c in checks:
            mark = "✓" if c["ok"] else "✗"
            (_output.detail if c["ok"] else _output.warning)(
                f"  {mark} {c['check']}: {c['detail']}"
            )
        (_output.ok if all_ok else _output.error)(
            f"Preflight {'passed' if all_ok else 'FAILED'} for '{cluster}'"
            if all_ok
            else f"Preflight FAILED for '{cluster}'"
        )
    if not all_ok:
        raise typer.Exit(code=1)


@app.command(epilog=_PLACE_EXAMPLES)
def capacity():
    """Per-cluster GPU capacity, utilization, GPU-hours used and cost (ACTIVE clusters)."""
    from examlops.data import init_db
    from examlops.data.hpc import get_hpc_jobs
    from examlops.hpc_capacity import capacity_report
    from examlops.hpc_registry import active_clusters_with_inventory

    init_db()
    rows = capacity_report(active_clusters_with_inventory(), get_hpc_jobs())
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No ACTIVE clusters. Approve one with 'exa hpc approve <name>'.")
        return
    cols = ["Cluster", "Scheduler", "GPUs (idle/total)", "Util %", "GPU-hours", "Cost $"]
    table = [
        [
            r["name"],
            r["scheduler"] or "—",
            f"{r['idle_gpus']}/{r['total_gpus']}",
            r["utilization_pct"],
            r["gpu_hours_used"],
            r["cost_usd"],
        ]
        for r in rows
    ]
    _output.print_table("HPC capacity", cols, table)
    _output.detail("Carbon/energy accounting: exa finops carbon")


@app.command("prometheus-sd")
def prometheus_sd(
    out: str = typer.Option(None, "--out", "-o", help="Write file_sd JSON here (else stdout)"),
    cluster: str = typer.Option(None, "--cluster", "-c", help="Limit to one cluster"),
):
    """Generate Prometheus file_sd scrape targets (node_exporter + DCGM) from the fleet registry (3.1)."""
    from examlops.prometheus_sd import generate, write_file_sd

    if out:
        n = write_file_sd(out, cluster)
        _output.ok(f"Wrote {n} scrape target(s) to {out}.")
    else:
        _output.print_json(generate(cluster))
