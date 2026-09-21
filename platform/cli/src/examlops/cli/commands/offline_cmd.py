"""`exa offline` — offline (batch) inference over a pinned dataset (ADR 0149).

Runs a registered *predictive* model version over a Parquet input — a dataplane snapshot revision or
a local path — through the same load and OIP v2 code path as online serving, and writes the
predictions as a content-addressed dataset with a manifest. Resumable and idempotent: run the same
command (same ``--key``) again after a crash and only the batches that did not commit are redone.
The job is an operation: ``exa ops status <job-id>`` reads it too.

Exit codes of ``exa offline run``: 0 completed, 1 refused or failed (or, with ``--fail-on-errors``,
completed with row errors), 130 stopped by a cancel request. The logic is in :mod:`examlops.offline`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from examlops import offline
from examlops.cli import _output

app = typer.Typer(
    help="Offline (batch) inference — run a registered model over a pinned dataset",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

CANCELLED_EXIT = 130

_KINDS = "predictive | generative | agentic (only predictive runs offline today)"
_EX_RUN = (
    "Examples:\n\n"
    "  # Score a local Parquet dataset with a registered model version\n"
    "  exa offline run --model JPCP --version 7 --input ./jobs.parquet --output ./scores "
    "--key score-2026-09\n\n"
    "  # Score a pinned dataplane snapshot, publish the result as a new snapshot\n"
    "  exa offline run --model JPCP --alias Production --input-source pm100 "
    "--input-table jobs --input-revision <rev> --output-source jpcp-scores --key nightly-0921\n\n"
    "  # A full spec from a file; re-running the same key resumes or replays\n"
    "  exa -o json offline run --spec offline.json\n\n"
    "Exit codes: 0 completed, 1 refused/failed, 130 cancelled."
)
_EX_STATUS = "Examples:\n\n  exa offline status off-3f9a…\n\n  exa -o json offline status <job-id>"
_EX_LIST = "Examples:\n\n  exa offline list\n\n  exa offline list --state running"
_EX_CANCEL = "Examples:\n\n  exa offline cancel off-3f9a…\n\n  exa --yes offline cancel <job-id>"

_COLUMNS = ["job_id", "model", "model_version", "state", "batches_done", "rows_err", "updated_at"]


def _fail(out: dict[str, Any]) -> None:
    if _output.json_mode:
        _output.print_json(out)
        raise typer.Exit(1)
    _output.error(str(out.get("error") or out.get("code")), hint=str(out.get("code") or ""))


def _spec_from_options(**o: Any) -> offline.OfflineJob:
    if o["spec"] is not None:
        try:
            doc = json.loads(Path(o["spec"]).read_text())
        except (OSError, ValueError) as exc:
            _output.error(f"cannot read spec {o['spec']}: {exc}")
        return offline.OfflineJob.from_dict(doc)
    inp = (
        {"type": "local", "path": str(o["input"])}
        if o["input"] is not None
        else {
            "type": "dataplane",
            "source": o["input_source"],
            "table": o["input_table"],
            "revision": o["input_revision"],
        }
    )
    outp = (
        {"type": "local", "path": str(o["output"])}
        if o["output"] is not None
        else {"type": "dataplane", "source": o["output_source"]}
    )
    return offline.OfflineJob.from_dict(
        {
            "kind": o["kind"],
            "model": o["model"],
            "version": o["version"],
            "alias": o["alias"],
            "input": inp,
            "output": outp,
            "batch_size": o["batch_size"],
            "resources": {"cpus": o["cpus"], "gpus": o["gpus"], "memory_gb": o["memory_gb"]},
            "idempotency_key": o["key"],
            "tenant": o["tenant"],
            "project": o["project"],
        }
    )


@app.command("run", epilog=_EX_RUN)
def run_cmd(
    spec: Path | None = typer.Option(None, "--spec", help="A JSON job spec (replaces the flags)"),
    model: str | None = typer.Option(None, "--model", "-m", help="Registered model name"),
    version: str | None = typer.Option(None, "--version", help="Registry version number"),
    alias: str | None = typer.Option(
        None, "--alias", help="Registry alias, resolved once to an immutable version"
    ),
    input_: Path | None = typer.Option(None, "--input", "-i", help="Local Parquet file or dir"),
    input_source: str | None = typer.Option(None, "--input-source", help="Dataplane source name"),
    input_table: str | None = typer.Option(None, "--input-table", help="Snapshot table to score"),
    input_revision: str = typer.Option(
        "latest", "--input-revision", help="Snapshot revision: 'latest' or a full 64-hex id"
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Local directory; receives <revision>/"
    ),
    output_source: str | None = typer.Option(
        None, "--output-source", help="Publish as a snapshot of this dataplane source"
    ),
    key: str | None = typer.Option(
        None, "--key", "-k", help="Idempotency key (required): same key = replay or resume"
    ),
    kind: str = typer.Option("predictive", "--kind", help=f"Workload kind: {_KINDS}"),
    batch_size: int = typer.Option(1000, "--batch-size", min=1, help="Rows per batch"),
    cpus: int = typer.Option(0, "--cpus", min=0, help="Declared CPUs (for cost; 0 = undeclared)"),
    gpus: int = typer.Option(0, "--gpus", min=0, help="Declared GPUs (for cost; 0 = none)"),
    memory_gb: float = typer.Option(0.0, "--memory-gb", min=0, help="Declared memory in GB"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant the job belongs to"),
    project: str = typer.Option("", "--project", help="Project (default dataplane project)"),
    fail_on_errors: bool = typer.Option(
        False, "--fail-on-errors", help="Exit 1 if any row failed (the run still completes)"
    ),
) -> None:
    """Run a registered predictive model over a dataset; resumable, idempotent, content-addressed."""
    try:
        job = _spec_from_options(
            spec=spec,
            model=model,
            version=version,
            alias=alias,
            input=input_,
            input_source=input_source,
            input_table=input_table,
            input_revision=input_revision,
            output=output,
            output_source=output_source,
            key=key,
            kind=kind,
            batch_size=batch_size,
            cpus=cpus,
            gpus=gpus,
            memory_gb=memory_gb,
            tenant=tenant,
            project=project,
        )
    except offline.OfflineSpecError as exc:
        _fail({"ok": False, "code": "invalid_spec", "error": str(exc), "problems": exc.problems})
        return
    out = offline.run(job)
    if not out["ok"]:
        if out.get("state") == "cancelled":
            if _output.json_mode:
                _output.print_json(out)
            else:
                _output.warning(f"Job {out['job_id']} cancelled; re-run the same key to resume")
            raise typer.Exit(CANCELLED_EXIT)
        _fail(out)
        return
    if _output.json_mode:
        _output.print_json(out)
    else:
        c = out["counts"]
        note = " (replayed — nothing recomputed)" if out.get("replayed") else ""
        _output.ok(
            f"Job {out['job_id']} completed{note}: {c['rows_ok']}/{c['rows']} rows scored, "
            f"{c['rows_err']} error(s)"
        )
        _output.print_record(
            {
                "output": out["output"]["uri"],
                "revision": out["output"]["revision"],
                "input revision": out["input"]["revision"],
                "model": f"{out['model']['name']} v{out['model']['version']}",
                "batches": c["batches"],
                "resumed batches skipped": c.get("skipped_batches", 0),
                "cost": (out.get("cost") or {}).get("basis", "—"),
            }
        )
    if out["counts"]["rows_err"] and fail_on_errors:
        raise typer.Exit(1)


def _record(j: dict[str, Any]) -> dict[str, Any]:
    return {
        "job": j["job_id"],
        "state": j["state"],
        "model": f"{j['model']} v{j['model_version']}",
        "batches": f"{j['batches_done']}/{j['batches_total'] if j['batches_total'] is not None else '?'}",
        "rows ok / error": f"{j['rows_ok']} / {j['rows_err']}",
        "attempts": j["attempts"],
        "output revision": j["output_revision"] or "—",
        "output": j["output_uri"] or "—",
        "error": j["error"] or "—",
    }


@app.command("status", epilog=_EX_STATUS)
def status_cmd(job_id: str = typer.Argument(..., help="Offline job id (off-…)")) -> None:
    """Show one offline job: state, progress, tallies and where the output is."""
    out = offline.status(job_id)
    if not out["ok"]:
        _fail(out)
        return
    if _output.json_mode:
        _output.print_json(out["job"])
        return
    if out["job"]["state"] == "stalled":
        _output.warning("The runner stopped without finishing; re-run the same command to resume")
    _output.print_record(_record(out["job"]))


@app.command("list", epilog=_EX_LIST)
def list_cmd(
    state: str | None = typer.Option(
        None, "--state", help="queued | running | stalled | completed | failed | cancelled"
    ),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200, help="Max jobs"),
) -> None:
    """List offline jobs, newest first."""
    out = offline.list_jobs(limit=limit)
    if not out["ok"]:
        _fail(out)
        return
    rows = [j for j in out["jobs"] if state is None or j["state"] == state]
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.ok("No offline jobs found")
        return
    _output.print_table(
        "Offline jobs",
        _COLUMNS,
        [[str(j.get(k) if j.get(k) is not None else "") for k in _COLUMNS] for j in rows],
    )


@app.command("cancel", epilog=_EX_CANCEL)
def cancel_cmd(job_id: str = typer.Argument(..., help="Offline job id to cancel")) -> None:
    """Cancel a job: a live run stops after the batch in flight; committed batches are kept."""
    if not _output.confirm(f"Cancel offline job [bold]{job_id}[/bold]?"):
        _output.info("Cancelled nothing.")
        return
    out = offline.cancel(job_id)
    if not out["ok"]:
        _fail(out)
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    if out["cancelled"]:
        _output.ok(f"Job {job_id} cancelled")
    else:
        _output.info(f"Cancel requested for {job_id}; it stops after the current batch")
    _output.print_record(_record(out["job"]))
