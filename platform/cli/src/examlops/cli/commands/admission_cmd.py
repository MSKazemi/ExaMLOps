"""``exa admission`` — durable fair-share admission-control queue (Phase 1 item 1.5)."""

from __future__ import annotations

import json

import typer

from examlops.cli import _output

app = typer.Typer(no_args_is_help=True, help="Admission-control queue (per-tenant fair-share)")


@app.command("submit")
def submit(
    kind: str = typer.Argument(..., help="Work kind, e.g. retrain | pipeline"),
    payload: str = typer.Option("{}", "--payload", "-p", help="JSON payload"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant for fair-share accounting"),
    project: str | None = typer.Option(None, "--project", help="Project attribution"),
    priority: int = typer.Option(0, "--priority", help="Higher runs first within a tenant"),
) -> None:
    """Enqueue a work item (durable). A worker claims it under the global + per-tenant caps.

    **This enqueues; it does not dispatch.** `examlops.admission` is a facade whose `dispatch` is
    injected by whatever embeds it, and the control plane runs its own admission accounting on this
    table rather than through the facade — so an item submitted here waits until something claims
    it. `exa admission stats` reports how long the oldest queued item has been waiting, which is
    what tells a busy queue from a stranded one.
    """
    from examlops import admission

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        _output.error(f"--payload must be valid JSON: {exc}")
        raise typer.Exit(1) from exc
    item_id = admission.submit(kind, data, tenant=tenant, project=project, priority=priority)
    if _output.json_mode:
        _output.print_json({"id": item_id, "kind": kind, "tenant": tenant})
        return
    _output.ok(
        f"Queued admission #{item_id} ({kind}, tenant={tenant}) — it waits for a worker to claim "
        f"it. Check it is moving with: exa admission stats"
    )


@app.command("stats")
def stats() -> None:
    """Show queue depth by state (queued/running/done/rejected/failed)."""
    from examlops import admission

    s = admission.stats()
    if _output.json_mode:
        _output.print_json(s)
        return
    _output.print_table(
        "Admission queue",
        ["State", "Count"],
        [[k, str(v)] for k, v in s.items()],
    )


@app.command("simulate")
def simulate(
    request: str = typer.Option(..., "--request", help="JobRequest JSON file to evaluate"),
    policy: str | None = typer.Option(
        None,
        "--policy",
        help="fair-share (default) | baseline-over-quota; else $EXAMLOPS_ADMISSION_POLICY",
    ),
    cluster_state: str | None = typer.Option(
        None,
        "--cluster-state",
        help="JSON file overriding the live state (what-if): total_gpus, free_gpus, "
        "gpus_in_use_by_tenant, running_by_tenant, largest_free_domain_gpus",
    ),
) -> None:
    """Show what the admission seam would decide for a job request. Read-only: nothing is queued,
    reserved or executed, and no audit event is written."""
    from pathlib import Path

    from examlops.admission_seam import JobRequest, JobRequestError
    from examlops.admission_seam.policy import ClusterState
    from examlops.admission_seam.service import current_state, decide

    try:
        req = JobRequest.from_dict(json.loads(Path(request).read_text(encoding="utf-8")))
        st = current_state()
        if cluster_state:
            over = json.loads(Path(cluster_state).read_text(encoding="utf-8"))
            by_tenant = over.get("running_by_tenant", st.running_by_tenant)
            st = ClusterState(
                running_total=sum(by_tenant.values()),
                running_by_tenant=by_tenant,
                gpus_in_use_by_tenant=over.get("gpus_in_use_by_tenant", st.gpus_in_use_by_tenant),
                total_gpus=over.get("total_gpus", st.total_gpus),
                free_gpus=over.get("free_gpus", st.free_gpus),
                largest_free_domain_gpus=over.get(
                    "largest_free_domain_gpus", st.largest_free_domain_gpus
                ),
                capabilities=st.capabilities,
            )
        decision, meta = decide(req, state=st, policy=policy, record=False)
    except (OSError, json.JSONDecodeError, JobRequestError, ValueError) as exc:
        _output.error(f"cannot simulate: {exc}")
        raise typer.Exit(1) from exc
    out = {"request": req.to_dict(), **decision.to_dict(), **meta}
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.print_table(
        "Admission simulation",
        ["Field", "Value"],
        [
            ["policy", meta["policy"]],
            ["verdict", decision.verdict],
            ["reason", decision.reason],
            ["ignored fields", ", ".join(meta["ignored_fields"]) or "-"],
            *[
                [f"gate {g['gate']}", f"{g['verdict']}: {g['reason']}"]
                for g in meta.get("gates", [])
            ],
        ],
    )


@app.command("topology")
def topology() -> None:
    """Show the typed resource graph admission reasons over (ADR 0116): scale-up domains and their
    free GPUs from the node inventory plus the site's declared topology file. Read-only."""
    from examlops.admission_seam import topology as topo

    try:
        path = topo.topology_path()
        graph = topo.current_graph()
    except (OSError, ValueError) as exc:
        _output.error(f"cannot build the resource graph: {exc}")
        raise typer.Exit(1) from exc
    out = {"topology_file": str(path) if path else None, **graph.summary()}
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.print_table(
        "Resource graph",
        ["Kind", "Count"],
        [[k, str(v)] for k, v in out["vertices"].items()],
    )
    _output.print_table(
        "Scale-up domains"
        + ("" if path else " (none declared: topology unknown, `required` is never promised)"),
        ["Domain", "Nodes", "Free GPUs"],
        [[d["id"], str(d["nodes"]), str(d["free_gpus"])] for d in out["scale_up_domains"]],
    )
    for problem in out["problems"]:
        _output.warning(problem)


@app.command("translate")
def translate(
    request: str = typer.Option(..., "--request", help="JobRequest JSON file to translate"),
    backend: str = typer.Option("slurm", "--backend", help="mock | slurm | flux"),
) -> None:
    """Show how one JobRequest maps onto an execution backend (ADR 0116): the native resources the
    adapter receives, and the fields that backend cannot enforce. Read-only; nothing is submitted."""
    from pathlib import Path

    from examlops.admission_seam import JobRequest, JobRequestError
    from examlops.admission_seam import translate as tr

    try:
        req = JobRequest.from_dict(json.loads(Path(request).read_text(encoding="utf-8")))
        out = tr.translate(req, backend)
    except (OSError, json.JSONDecodeError, JobRequestError, ValueError) as exc:
        _output.error(f"cannot translate: {exc}")
        raise typer.Exit(1) from exc
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.print_table(
        f"{out['backend']} native resources",
        ["Key", "Value"],
        [[k, str(v)] for k, v in out["native"].items()],
    )
    _output.detail("  not enforced by this backend: " + ", ".join(out["not_native"]))


@app.command("reconcile")
def reconcile(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be expired/released; change nothing"
    ),
) -> None:
    """Reclaim leaked quota (ADR 0116 decision 3): expire reservations whose TTL lapsed, and release
    committed job reservations whose scheduler job has ended. A job the scheduler cannot answer
    for keeps its quota. Run it periodically (cron / a Prefect schedule)."""
    from examlops import scheduler_jobs
    from examlops.admission_seam import completion
    from examlops.admission_seam import reservations as res

    adapters: dict[str, object] = {}

    def _state(scheduler: str, job_id: str) -> str | None:
        if scheduler not in adapters:
            adapters[scheduler] = scheduler_jobs.scheduler_adapter(scheduler)
        status = adapters[scheduler].get_job_status(job_id)  # type: ignore[attr-defined]
        return (status or {}).get("state")

    import contextlib
    import sys

    expired = res.expire(dry_run=dry_run)
    # The adapters announce their backend on stdout; keep that out of --json output.
    with contextlib.redirect_stdout(sys.stderr):
        jobs = completion.reconcile_job_reservations(_state, dry_run=dry_run)
    out = {"dry_run": dry_run, "expired": expired, **jobs}
    if _output.json_mode:
        _output.print_json(out)
        return
    verb = "would be" if dry_run else ""
    _output.print_table(
        "Admission reconcile" + (" (dry run)" if dry_run else ""),
        ["What", "Count"],
        [
            [f"lapsed reservations {verb} expired".replace("  ", " "), str(len(expired))],
            [f"job reservations {verb} released".replace("  ", " "), str(len(jobs["released"]))],
            ["job reservations still running", str(len(jobs["still_running"]))],
            ["job reservations unverified (kept)", str(len(jobs["unverified"]))],
        ],
    )
    for u in jobs["unverified"]:
        _output.warning(f"{u['holder']}: {u['error']}")


@app.command("reservations")
def reservations(
    state: str | None = typer.Option(
        None, "--state", help="reserved | committed | released | expired"
    ),
    project: str | None = typer.Option(None, "--project", help="Only this project"),
    expire_preview: bool = typer.Option(
        False, "--expire-preview", help="List reservations whose TTL lapsed (nothing is changed)"
    ),
    limit: int = typer.Option(100, "--limit", help="Most recent N"),
) -> None:
    """List two-phase quota reservations, or preview which leaked ones would expire. Read-only."""
    import time

    from examlops.data import quota_reservations as store

    if expire_preview:
        rows = store.expire_due(dry_run=True)
    else:
        rows = store.list_reservations(state=state, project=project, limit=limit)
    now = time.time()
    for r in rows:
        r["lapsed"] = r["state"] == "reserved" and r["expires_at"] <= now
    if _output.json_mode:
        _output.print_json({"reservations": rows, "expire_preview": expire_preview})
        return
    _output.print_table(
        "Quota reservations" + (" (expire preview)" if expire_preview else ""),
        ["ID", "Project", "Tenant", "GPUs", "GPU-h", "State", "Lapsed"],
        [
            [
                r["id"][:12],
                r["project"],
                r["tenant"],
                str(r["gpus"]),
                f"{r['gpu_hours']:.2f}",
                r["state"],
                "yes" if r["lapsed"] else "no",
            ]
            for r in rows
        ],
    )
