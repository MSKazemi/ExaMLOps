"""`exa pipeline distributed suspend` — the suspend/resume seam (ADR 0109) from the command line.

Thin over :mod:`examlops.suspend.service` (which audits every snapshot, resume, refusal and
discard) and :mod:`examlops.suspend.report` (capability + preemption verdict + timing split).
A refusal is an exit code 1 with the backend's own reason, never a silent fallback.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Any

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Suspend/resume seam (ADR 0109) — pin, restore, release workload state",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa pipeline distributed suspend backends\n\n"
    "  exa pipeline distributed suspend snapshot dist-run-7 --backend training-checkpoint\n\n"
    "  exa pipeline distributed suspend snapshot llm-a --kind serving "
    "--backend vllm-sleep --base-url http://gpu-03:8000\n\n"
    "  exa pipeline distributed suspend resume <snapshot-id>\n\n"
    "  exa pipeline distributed suspend list --status suspended"
)


class SubjectKind(StrEnum):
    training = "training"
    agent = "agent"
    serving = "serving"


class SnapshotStatus(StrEnum):
    suspended = "suspended"
    resumed = "resumed"
    discarded = "discarded"
    failed = "failed"


def _kind(kind: SubjectKind) -> str:
    from examlops.suspend import STATE_AGENT_SESSION, STATE_SERVING_REPLICA, STATE_TRAINING_RUN

    return {
        SubjectKind.training: STATE_TRAINING_RUN,
        SubjectKind.agent: STATE_AGENT_SESSION,
        SubjectKind.serving: STATE_SERVING_REPLICA,
    }[kind]


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


@app.command("backends", epilog=_EXAMPLES)
def backends() -> None:
    """Every suspend backend: honest capability, preemption verdict, restore timing split."""
    from examlops.suspend.report import seam_report

    report = seam_report()
    if _output.json_mode:
        _output.print_json(report)
        return
    rows = []
    for b in report["backends"]:
        if "error" in b:
            rows.append([b["backend"], "error", str(b["error"])[:50], "", "", ""])
            continue
        cap, pre, t = b["capability"], b["preemption"], b.get("timing") or {}
        rows.append(
            [
                cap["backend"] + (" *" if cap["backend"] == report["selected_backend"] else ""),
                cap["granularity"],
                ",".join(cap["tiers"]) or "—",
                cap["basis"],
                "yes" if pre["can_promise"] else "no",
                str(t.get("restores", "?")),
            ]
        )
    _output.print_table(
        "Suspend Backends (* = selected)",
        ["Backend", "Granularity", "Tiers", "Basis", "Preemption", "Restores"],
        rows,
    )


@app.command("capability")
def capability(
    backend: str = typer.Argument(None, help="Backend name (default: the selected backend)"),
) -> None:
    """One backend's capability, measured from recorded restores, and why it may not promise."""
    from examlops.suspend import SuspendError
    from examlops.suspend.report import backend_report
    from examlops.suspend.service import get_backend

    try:
        report = backend_report(get_backend(backend))
    except SuspendError as exc:
        _output.error(str(exc))
    if _output.json_mode:
        _output.print_json(report)
        return
    cap = report["capability"]
    _output.print_record(
        {
            **{k: (", ".join(v) if isinstance(v, list) else v) for k, v in cap.items()},
            "can_promise_preemption": report["preemption"]["can_promise"],
            "preemption_reasons": "; ".join(report["preemption"]["reasons"]) or "—",
            **{f"timing.{k}": v for k, v in (report.get("timing") or {}).items()},
        }
    )


@app.command("snapshot", epilog=_EXAMPLES)
def snapshot(
    subject: str = typer.Argument(..., help="Run id, agent thread id or serving replica name"),
    kind: SubjectKind = typer.Option(SubjectKind.training, "--kind", help="What is suspended"),
    backend: str = typer.Option(None, "--backend", help="Suspend backend (default: selected)"),
    run_dir: str = typer.Option(
        None, "--run-dir", help="Training run directory (default: the launcher's run dir)"
    ),
    config_hash: str = typer.Option(
        None, "--config-hash", help="Only pin a checkpoint written under this config hash"
    ),
    base_url: str = typer.Option(None, "--base-url", help="vLLM server root (vllm-sleep)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant the record belongs to"),
) -> None:
    """Suspend a subject: pin (or release) its state and record it. Audited; exit 1 on refusal."""
    from examlops.suspend import SuspendError, service

    options: dict[str, Any] = {}
    if run_dir:
        options["run_dir"] = run_dir
    if config_hash:
        options["config_hash"] = config_hash
    if base_url:
        options["base_url"] = base_url
    try:
        handle = service.suspend(
            subject,
            subject_kind=_kind(kind),
            backend=backend,
            options=options,
            tenant=tenant,
            actor=_actor(),
        )
    except SuspendError as exc:
        _output.error(f"suspend refused: {exc}")
    body = {
        "snapshot_id": handle.snapshot_id,
        "backend": handle.backend,
        "subject_kind": handle.subject_kind,
        "subject_id": handle.subject_id,
        "state_bytes": handle.state_bytes,
        "pointer": handle.pointer,
    }
    if _output.json_mode:
        _output.print_json(body)
        return
    _output.ok(f"Suspended {subject} via {handle.backend} → snapshot {handle.snapshot_id}")
    local = (handle.pointer or {}).get("local_tier")
    if isinstance(local, dict):
        if local.get("staged"):
            _output.info(
                f"local tier ({local.get('tier')}): copied {local.get('copied_bytes')} B, "
                f"reused {local.get('reused_bytes')} B"
            )
        else:
            _output.warning(f"local tier skipped: {local.get('reason')}")


@app.command("resume")
def resume(snapshot_id: str = typer.Argument(..., help="Snapshot id")) -> None:
    """Restore a suspended snapshot and record the timing split. Exit 1 if it cannot."""
    from examlops.suspend import SuspendError, service

    try:
        report = service.resume(snapshot_id, actor=_actor())
    except SuspendError as exc:
        _output.error(str(exc))
    body = {
        "snapshot_id": snapshot_id,
        "restored": report.restored,
        "state_transfer_s": report.state_transfer_s,
        "communicator_rebuild_s": report.communicator_rebuild_s,
        "total_s": report.total_s,
        "detail": report.detail,
    }
    if not report.restored:
        _output.error(f"restore failed: {report.detail}")
    if _output.json_mode:
        _output.print_json(body)
        return
    _output.ok(f"Restored {snapshot_id}")
    _output.print_record(body)


@app.command("discard")
def discard(snapshot_id: str = typer.Argument(..., help="Snapshot id")) -> None:
    """Release a snapshot's pin and record. The workload's own checkpoint is never deleted."""
    from examlops.suspend import SuspendError, service

    try:
        service.discard(snapshot_id, actor=_actor())
    except SuspendError as exc:
        _output.error(str(exc))
    _output.ok(f"Discarded snapshot {snapshot_id}")


@app.command("show")
def show(snapshot_id: str = typer.Argument(..., help="Snapshot id")) -> None:
    """One suspend record."""
    from examlops.suspend import service

    row = service.status(snapshot_id)
    if row is None:
        _output.error(f"unknown snapshot {snapshot_id!r}")
    if _output.json_mode:
        _output.print_json(row)
        return
    _output.print_record({k: v for k, v in row.items() if k not in ("capability",)})


@app.command("list")
def list_cmd(
    subject: str = typer.Option(None, "--subject", help="Filter by subject id"),
    status: SnapshotStatus = typer.Option(None, "--status", help="Filter by status"),
    tenant: str = typer.Option(None, "--tenant", help="Filter by tenant"),
    limit: int = typer.Option(50, "--limit", min=1, max=1000, help="Maximum rows"),
) -> None:
    """Suspend records, newest first."""
    from examlops.suspend import service

    rows = service.list_snapshots(subject, status.value if status else None, limit, tenant=tenant)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No suspend records.")
        return
    _output.print_table(
        "Suspend Records",
        ["Snapshot", "Backend", "Subject", "Tenant", "Status", "Transfer s"],
        [
            [
                r["snapshot_id"][:12],
                r["backend"],
                r["subject_id"],
                r["tenant"],
                r["status"],
                "—" if r.get("state_transfer_s") is None else f"{r['state_transfer_s']:.3f}",
            ]
            for r in rows
        ],
    )
