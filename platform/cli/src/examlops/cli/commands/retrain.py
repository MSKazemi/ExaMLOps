from __future__ import annotations

import os
from contextlib import nullcontext

import typer

from examlops import control_plane_api
from examlops.cli import _client, _output
from examlops.cli._config import load_config
from examlops.cli._enums import StorageBackend
from examlops.cli._provenance import audit_details, reason_option, scope_hint

_EXAMPLES = (
    "Examples:\n\n"
    "  # Fast dev-safe retrain (no data download)\n"
    "  exa retrain JPCP --dummy\n\n"
    "  # Production-style retrain via the Control Plane\n"
    "  exa retrain JPCP --dataset PM100Dataset --backend minio\n\n"
    "  # Specify dataset explicitly\n"
    "  exa retrain JPCP --dataset PM100Dataset\n\n"
    "  # Preview without triggering\n"
    "  exa retrain JPCP --dry-run\n\n"
    "  # Queue it and return at once (follow with `exa commands show <id>`)\n"
    "  exa retrain JPCP --dataset PM100Dataset --async\n\n"
    "  # Non-interactive (CI): skip the confirmation prompt\n"
    "  exa --yes retrain JPCP --dummy"
)

_EXAMPLES_STATUS = (
    "Examples:\n\n"
    "  # Is the retrain I scheduled done? (the id `exa retrain` printed)\n"
    "  exa retrain-status 4f0c1e2a-…\n\n"
    "  exa --json retrain-status 4f0c1e2a-…"
)


def retrain_status(
    flow_run_id: str = typer.Argument(..., help="Flow run id printed by `exa retrain`"),
) -> None:
    """Show the state of one retrain run (scheduled, running, completed, failed)."""
    cfg = load_config()
    try:
        status = control_plane_api.run_status(
            flow_run_id, base=cfg.control_plane_url, token=cfg.control_plane_token
        )
    except _client.ClientError as exc:
        _output.error(
            f"Could not read retrain run {flow_run_id}: {exc}",
            hint="Check the id `exa retrain` printed, and that the control plane is up: exa status",
        )
    _output.print_record(status if isinstance(status, dict) else {"status": status})


def retrain(
    model: str = typer.Argument(..., help="Model ID (e.g. JPCP)"),
    dataset: str | None = typer.Option(None, "--dataset", "-d", help="Dataset class name"),
    dummy: bool = typer.Option(False, "--dummy", help="Use dummy data (dev-safe, no downloads)"),
    backend: StorageBackend | None = typer.Option(None, "--backend", help="Storage backend"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be scheduled without triggering it"
    ),
    reason: str | None = reason_option(),
    queue: bool = typer.Option(
        False,
        "--async",
        help="Submit as an asynchronous command (/v1/retrain) and return at once; the control "
        "plane's workers dispatch it with retries. Follow with `exa commands show <id>`.",
    ),
) -> None:
    """Trigger a Prefect training run via the Control Plane."""
    from examlops.usecase import default_dataset_for

    cfg = load_config()
    # No hardcoded dataset (ADR 0094): fall back to the model's primary dataset from the pack YAML.
    dataset_name = dataset or default_dataset_for(model)
    if not dataset_name:
        _output.error(f"--dataset is required (no default dataset in {model}'s YAML)")
        raise typer.Exit(1)
    backend_name = backend.value if backend is not None else None
    body = {
        "model_name": model,
        "dataset_name": dataset_name,
        "is_dummy": dummy,
        "backend_name": backend_name,
    }

    # ── Dry run: describe the action, change nothing ──────────────────────────
    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "would_schedule": body})
        else:
            _output.info("Dry run — no retrain will be scheduled.")
            _output.print_record(
                {
                    "model": model,
                    "dataset": dataset_name,
                    "dummy": dummy,
                    "backend": backend_name or "default",
                }
            )
        return

    # ── Policy-as-code gate (ADR 0079): no file/no match ⇒ unchanged (allow) behaviour.
    # `decide_safe` (not `decide`) so a bug in the engine itself denies and is durably audited
    # instead of crashing this command with an unhandled traceback and no record of what
    # happened (BL-080) — a human is at the keyboard here, but the traceback told them nothing.
    from examlops import policy

    decision = policy.decide_safe(
        "retrain",
        {"model": model, "dataset": dataset_name, "dummy": dummy},
        default_effect=policy.DENY,
    )
    if decision.denied:
        _output.error(
            f"Policy denied retrain of {model}: {decision.reason}",
            hint="See your policy.yaml or run: exa policy list",
        )
        raise typer.Exit(1)

    # ── Early access-scope hint: retrain needs a Control Plane token (N5) ──────
    scope_hint("a CONTROL_PLANE_TOKEN", bool(cfg.control_plane_token))

    # ── Confirm the mutation (auto-yes under --yes / --json / CI) ──────────────
    approval_note = " [policy requires approval]" if decision.requires_approval else ""
    if not _output.confirm(
        f"Schedule a retrain of {model} on {dataset_name}"
        f"{' (dummy)' if dummy else ''}?{approval_note}",
        default=not decision.requires_approval,
    ):
        _output.warning("Aborted — no retrain scheduled.")
        raise typer.Exit(0)

    # The control plane consults the same `retrain` rule; a human who just confirmed a
    # require_approval prompt has already given the approval it would ask for (ADR 0079).
    from examlops.policy import http_gate

    ack = http_gate.approval_acknowledged() if decision.requires_approval else nullcontext()

    if queue:
        with ack:
            _submit_async(cfg, model, dataset_name, body)
        return

    from examlops import retrain_command

    with _output.spinner(f"Scheduling retrain for {model}…"), ack:
        try:
            # The command API, waited on until the retrain is dispatched (plan P1.6c).
            result = retrain_command.submit(
                body, base=cfg.control_plane_url, token=cfg.control_plane_token
            )
        except _client.ClientError as e:
            _output.error(
                f"Failed to schedule retrain for {model}: {e}",
                hint="Is the control plane running? Try: exa status",
            )
            return

    _record_audit(model, dataset_name, dummy, backend_name, result, reason)

    if retrain_command.dispatched(result):
        _output.ok(f"Retrain scheduled for {model} (dataset: {dataset_name})")
    else:
        _output.warning(
            f"Retrain of {model} accepted but not dispatched yet — the control plane will "
            f"dispatch it (command {result.get('command_id')})"
        )
    _output.print_record(
        {
            "flow_run_id": result.get("flow_run_id", "—"),
            "command_id": result.get("command_id", "—"),
            "operation_id": result.get("operation_id", "—"),
            "model": model,
            "dataset": dataset_name,
            "dummy": dummy,
        }
    )
    _emit_retrain_lineage(model, dataset_name, result)
    if retrain_command.dispatched(result):
        _output.hint(
            f"Follow it: exa retrain-status {result['flow_run_id']}"
            "  |  Logs: exa stack logs --service orchestrator"
        )
    else:
        _output.hint(f"Follow it: exa commands show {result.get('command_id')}")
    if _output.json_mode:
        _output.print_json(result)


def _emit_retrain_lineage(model: str, dataset: str, result: dict) -> None:
    """A2 lineage for a retrain request (ADR 0004 clause 1). Fail-open.

    The request and the training it schedules are two runs of two jobs. This one — job
    ``retrain:<MODEL>`` — is complete the moment the flow run is scheduled, so it is a
    ``COMPLETE``, and ``examlops.scheduled_run`` links it to the training run: the flow's own
    ``START``/``COMPLETE``/``FAIL`` use the Prefect flow run id as their run id, so a receiver
    and Prefect name that run the same way.

    It used to be a ``START`` under the flow run id on this job, while the flow closed a different
    run on job ``train:<model>`` — so the request never completed, and in any lineage receiver
    every retrain stayed running forever. No output node either: the version it will produce does
    not exist yet, and a ``<model>/pending`` node became a dataset no run ever wrote.
    """
    try:
        from examlops.lineage import dataset_node, emit_lineage, scheduled_run_facet

        flow_run_id = result.get("flow_run_id")
        ref = flow_run_id or result.get("command_id")
        emit_lineage(
            "COMPLETE",
            job=f"retrain:{model.upper()}",
            run_id=f"retrain:{ref}" if ref else f"retrain-{model.upper()}-{dataset}",
            inputs=[dataset_node(dataset)],
            facets=scheduled_run_facet(str(flow_run_id)) if flow_run_id else {},
            model=model.upper(),
        )
    except Exception:  # noqa: BLE001
        pass


def _record_audit(
    model: str,
    dataset: str,
    dummy: bool,
    backend: str | None,
    result: dict,
    reason: str | None = None,
) -> None:
    """Write a best-effort audit event — never fail the command on audit errors.

    ``retrain_triggered`` is an EU-AI-Act Art. 12 required event, so a lost one is logged and
    counted rather than swallowed: neither the hash chain nor the coverage report can see an event
    that never arrived.
    """
    from examlops.data.audit import audit_best_effort

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"
    audit_best_effort(
        "exa-retrain",
        actor,
        "retrain_triggered",
        model.upper(),
        audit_details(
            {
                "dataset": dataset,
                "dummy": dummy,
                "backend": backend,
                "flow_run_id": result.get("flow_run_id"),
            },
            reason,
        ),
    )


def _submit_async(cfg, model: str, dataset_name: str, body: dict) -> None:
    """Queue the retrain as a durable command; the control plane dispatches it (plan P1.2)."""
    try:
        view = _client.post(
            f"{cfg.control_plane_url}/v1/retrain", body, token=cfg.control_plane_token
        )
    except _client.ClientError as exc:
        _output.error(
            f"Failed to queue retrain for {model}: {exc}",
            hint="Is the control plane running? Try: exa status",
        )
        return
    if _output.json_mode:
        # `operation_id` is the handle of `exa ops status|wait|cancel` (ADR 0147 d5).
        _output.print_json({**view, "operation_id": view.get("command_id")})
        return
    _output.ok(f"Retrain queued for {model} (dataset: {dataset_name})")
    _output.print_record(
        {
            "command_id": view.get("command_id"),
            "operation_id": view.get("command_id"),
            "state": view.get("state"),
            "model": model,
        }
    )
    _output.info(f"Follow it: exa commands show {view.get('command_id')}")
