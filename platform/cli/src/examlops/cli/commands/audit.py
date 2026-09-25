from __future__ import annotations

import json
import os

import typer

from examlops.cli import _output
from examlops.data import get_db, init_db

_EXAMPLES = (
    "Examples:\n\n"
    "  exa audit\n\n"
    "  exa audit --last 7d\n\n"
    "  exa audit --model JPCP\n\n"
    "  exa audit --action model_approved\n\n"
    "  exa --json audit --last 30d\n\n"
    "  exa audit verify\n\n"
    "  exa audit checkpoint\n\n"
    "  exa audit chain <correlation-id>\n\n"
    "  exa audit autonomy --last 30d"
)

app = typer.Typer(
    help="Platform audit log — tamper-evident, hash-chained (D4)",
    no_args_is_help=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def _parse_days(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1])
    return int(s)


def _details_text(details: object) -> str:
    """The stored JSON text of an event's details, as the table always showed it."""
    if details is None:
        return ""
    return details if isinstance(details, str) else json.dumps(details)


@app.callback(invoke_without_command=True, epilog=_EXAMPLES)
def audit(
    ctx: typer.Context,
    last: str = typer.Option("30d", "--last", help="Time window (e.g. 7d, 30d)"),
    model: str | None = typer.Option(None, "--model", "-m", help="Filter by target model"),
    action: str | None = typer.Option(None, "--action", "-a", help="Filter by action type"),
    source: str | None = typer.Option(
        None, "--source", "-s", help="Filter by source (cli/agent/bridge)"
    ),
    limit: int = typer.Option(100, "--limit", "-n", help="Max events to show"),
):
    """Show platform audit log — who did what and when."""
    if ctx.invoked_subcommand is not None:
        return
    # A thin render over the SDK (ADR 0078 clause 2): `examlops.audit.query()` is the one read
    # path — filters in SQL before the LIMIT, `ts DESC, id DESC` (the chain's own order).
    from examlops.sdk import audit as sdk_audit
    from examlops.sdk.errors import SDKError

    try:
        events = sdk_audit.query(
            since_days=_parse_days(last), model=model, action=action, source=source, limit=limit
        )
    except SDKError as e:
        _output.error(str(e))

    if not events:
        _output.ok("No audit events found for the given filters")
        return

    if _output.json_mode:
        _output.print_json(
            [
                {
                    "id": ev.id,
                    "ts": ev.ts,
                    "source": ev.source,
                    "actor": ev.actor,
                    "action": ev.action,
                    "target": ev.target,
                    "details": ev.details,
                }
                for ev in events
            ]
        )
        return

    cols = ["Time", "Source", "Actor", "Action", "Target", "Details"]
    table_rows = [
        [
            ev.ts,
            ev.source or "—",
            ev.actor or "—",
            ev.action,
            ev.target or "—",
            _details_text(ev.details)[:50],
        ]
        for ev in events
    ]
    _output.print_table(f"Audit Log (last {last})", cols, table_rows)


@app.command("chain")
def chain(
    correlation_id: str = typer.Argument(..., help="Correlation id to reconstruct"),
) -> None:
    """Reconstruct one unit of work and everything it caused (ADR 0110).

    A hash chain records *events*; this reads the causal edges between them, so an
    orchestrator's id returns the tool calls it caused and whatever those caused in turn. That
    reconstruction is what makes "who did this, on whose behalf, and how would it be undone"
    answerable from the evidence chain alone.
    """
    from examlops.data.audit import correlation_chain

    events = correlation_chain(correlation_id)
    if not events:
        _output.ok(f"No events correlated to {correlation_id}")
        return
    if _output.json_mode:
        _output.print_json(events)
        return
    _output.print_table(
        f"Correlation chain — {correlation_id}",
        ["ID", "When", "Action", "Target", "Actor", "On behalf of", "Mode", "Undo"],
        [
            [
                str(e["id"]),
                str(e.get("ts") or "—"),
                e.get("action") or "—",
                e.get("target") or "—",
                e.get("actor") or "—",
                e.get("on_behalf_of") or "—",
                e.get("mode") or "—",
                e.get("rollback_ref") or "—",
            ]
            for e in events
        ],
    )


@app.command("autonomy")
def autonomy(
    last: str = typer.Option("30d", "--last", help="Time window (e.g. 7d, 30d)"),
    limit: int = typer.Option(
        500, "--limit", help="How many actions to list (the counts always cover the whole window)"
    ),
) -> None:
    """Every autonomous action in the window, and whether it declared an inverse.

    This is the W2 gate as a command: for each action the platform took on its own initiative,
    who acted, on whose behalf, under which mode, and how it would be undone. An action with no
    ``rollback_ref`` is listed rather than filtered out — ADR 0110 decision 4 calls that a policy
    violation, and hiding them would defeat the point of asking.
    """
    from examlops.data.audit import (
        autonomous_actions,
        count_autonomous_actions,
        count_autonomous_without_rollback,
    )

    days = _parse_days(last)
    # The listing is bounded (nobody reads a hundred thousand rows); the totals are not. Counting
    # violations by looking at the page would answer "among the newest few hundred" while printing
    # a sentence that reads as "in the window" — and the compliance pack sends an auditor here.
    rows = autonomous_actions(since_days=days, limit=limit)
    total = count_autonomous_actions(since_days=days)
    without_rollback = count_autonomous_without_rollback(since_days=days)
    if not total:
        _output.ok(f"No autonomous actions recorded in the last {last}")
        return
    undoable = total - without_rollback
    if _output.json_mode:
        _output.print_json(
            {
                "window": last,
                "count": total,
                "undoable": undoable,
                "without_rollback": without_rollback,
                "listed": len(rows),
                "actions": rows,
            }
        )
        return
    _output.print_table(
        f"Autonomous actions — last {last}",
        ["When", "Action", "Target", "Actor", "On behalf of", "Correlation", "Undo"],
        [
            [
                str(r["ts"]),
                r["action"],
                r["target"] or "—",
                r["actor"] or "—",
                r["on_behalf_of"] or "—",
                (r["correlation_id"] or "—")[:12],
                r["rollback_ref"] or "NONE",
            ]
            for r in rows
        ],
    )
    if len(rows) < total:
        _output.warning(
            f"Showing the {len(rows)} most recent of {total} autonomous action(s) in the window. "
            "The counts below are for the whole window, not this page."
        )
    if without_rollback:
        _output.warning(
            f"{without_rollback} of {total} autonomous action(s) declared no inverse. "
            "ADR 0110 decision 4 treats a NULL rollback_ref on an autonomous action as a policy "
            "violation; the refusal that enforces it is not built yet, so these are reported."
        )


@app.command("verify")
def verify() -> None:
    """Recompute the hash chain and report integrity (D4·R2/R6). Exit 1 if broken."""
    from examlops.data.audit import verify_audit_chain

    result = verify_audit_chain()
    # Said in *both* modes. `_output.warning` writes to stderr, so stdout still carries exactly one
    # JSON document while a scripted compliance check — and whoever is watching it — is told what
    # the verifier could not read. A green exit over a partly-read log is the failure this command
    # exists to prevent, and that is as true of the machine path as of the human one.
    if result.get("warning"):
        _output.warning(result["warning"])
    if _output.json_mode:
        _output.print_json(result)
        if not result["ok"]:
            raise typer.Exit(1)
        return
    if result["ok"]:
        _output.ok(
            f"Audit chain verified — {result['count']} chained event(s) intact "
            f"(head {result.get('head_hash', '')[:12]}…)."
        )
    else:
        _output.error(
            f"AUDIT CHAIN BROKEN at event id {result['broken_at_id']}: {result['reason']}. "
            "The audit trail has been tampered with."
        )


@app.command("anchor")
def anchor() -> None:
    """Anchor the high-volume telemetry side tables into the chain (ADR 0110 decision 2).

    Cron-able; the autopilot also anchors at the end of each live cycle. Each anchor names its
    own row range, so the cadence is recorded in the chain itself.
    """
    from examlops.telemetry_anchor import anchor_telemetry

    results = anchor_telemetry(_actor())
    if _output.json_mode:
        _output.print_json(results)
        return
    for r in results:
        if r.get("anchored"):
            _output.ok(
                f"{r['table']}: anchored rows {r['from_id']}..{r['to_id']} "
                f"({r['rows']}) — {r['sha256'][:12]}…"
            )
        else:
            _output.info(f"{r['table']}: nothing new to anchor")


@app.command("verify-anchors")
def verify_anchors_cmd() -> None:
    """Verify every telemetry anchor against its side table (ADR 0110 decision 5). Exit 1 on a break."""
    from examlops.telemetry_anchor import verify_anchors

    result = verify_anchors()
    if _output.json_mode:
        _output.print_json(result)
        if not result["ok"]:
            raise typer.Exit(1)
        return
    if result["ok"]:
        _output.ok(f"{result['anchors_checked']} telemetry anchor(s) verified intact.")
    else:
        for b in result["breaks"]:
            _output.error(
                f"ANCHOR BROKEN: {b['table']} rows {b.get('from_id')}..{b.get('to_id')} "
                f"(event {b['event_id']}): {b['reason']} — the side table no longer matches "
                "what the chain vouched for."
            )
    if result.get("pruned_anchors"):
        _output.warning(
            f"{len(result['pruned_anchors'])} older anchor(s) superseded by an audited "
            "retention prune (reported, not counted as tampering)."
        )
    for table, n in (result.get("unanchored_rows") or {}).items():
        _output.warning(f"{table}: {n} row(s) newer than the last anchor — run: exa audit anchor")
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("review")
def review(
    sample: int = typer.Option(20, "--sample", help="Events to sample for review"),
    notes: str = typer.Option("", "--notes", help="Reviewer notes, recorded with the review"),
) -> None:
    """Perform and RECORD a sampled audit review (ADR 0113 decision 5).

    An unreviewed audit trail is theatre: this samples events written since the last recorded
    review (all of them, if fewer than the sample size), shows them, and writes an
    ``audit_reviewed`` event naming the reviewer, the covered range and the sampled ids —
    so "is anyone actually looking?" is answerable from the chain. Schedule it (cron /
    `exa backup schedule`-style) at whatever cadence your governance names.
    """
    import random

    init_db()
    with get_db() as conn:
        last = conn.execute(
            "SELECT details FROM audit_events WHERE action='audit_reviewed' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        from_id = 1
        if last and last["details"]:
            try:
                from_id = int(json.loads(last["details"]).get("to_id", 0)) + 1
            except (TypeError, ValueError):
                from_id = 1
        rows = conn.execute(
            "SELECT id, ts, source, actor, action, target FROM audit_events "
            "WHERE id >= ? AND action != 'audit_reviewed' ORDER BY id ASC",
            (from_id,),
        ).fetchall()
    if not rows:
        _output.ok("Nothing new to review since the last recorded review.")
        return
    chosen = rows if len(rows) <= sample else random.sample(list(rows), sample)
    chosen = sorted(chosen, key=lambda r: r["id"])
    to_id = rows[-1]["id"]
    if not _output.json_mode:
        _output.print_table(
            f"Audit review sample — {len(chosen)} of {len(rows)} event(s) since id {from_id}",
            ["ID", "When", "Source", "Actor", "Action", "Target"],
            [
                [
                    str(r["id"]),
                    r["ts"],
                    r["source"],
                    r["actor"] or "-",
                    r["action"],
                    r["target"] or "-",
                ]
                for r in chosen
            ],
        )
    from examlops.data.audit import write_audit_event

    details = {
        "reviewer": _actor(),
        "from_id": from_id,
        "to_id": to_id,
        "total_events": len(rows),
        "sample_ids": [r["id"] for r in chosen],
        "notes": notes.strip() or None,
    }
    write_audit_event("audit", _actor(), "audit_reviewed", None, details)
    if _output.json_mode:
        _output.print_json(details)
    else:
        _output.ok(
            f"Review recorded: {len(chosen)}/{len(rows)} events (ids {from_id}..{to_id}) "
            f"by {_actor()}."
        )


@app.command("reviews")
def reviews(
    last: int = typer.Option(10, "--last", help="How many recorded reviews to show"),
) -> None:
    """List recorded audit reviews — who reviewed, when, covering what."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, ts, actor, details FROM audit_events WHERE action='audit_reviewed' "
            "ORDER BY id DESC LIMIT ?",
            (last,),
        ).fetchall()
    payload = []
    for r in rows:
        try:
            d = json.loads(r["details"]) if r["details"] else {}
        except (TypeError, ValueError):
            d = {}
        payload.append({"event_id": r["id"], "ts": r["ts"], **d})
    if _output.json_mode:
        _output.print_json(payload)
        return
    if not payload:
        _output.warning("No audit review has ever been recorded — run: exa audit review")
        return
    _output.print_table(
        "Recorded audit reviews",
        ["When", "Reviewer", "Range", "Sampled", "Notes"],
        [
            [
                p_["ts"],
                str(p_.get("reviewer", "-")),
                f"{p_.get('from_id', '?')}..{p_.get('to_id', '?')}",
                str(len(p_.get("sample_ids", []))),
                str(p_.get("notes") or "-"),
            ]
            for p_ in payload
        ],
    )


@app.command("checkpoint")
def checkpoint(
    anchor_required: bool = typer.Option(
        False,
        "--anchor",
        help="Require the WORM anchor: exit 1 unless the checkpoint was durably anchored "
        "(cron-friendly; an S3 failure that degraded to the local fallback counts as a failure)",
    ),
    skip_unchanged: bool = typer.Option(
        False,
        "--skip-unchanged",
        help="Do nothing when the head already has an anchored checkpoint (cheap for cron)",
    ),
) -> None:
    """Sign the current chain head and anchor it to the WORM store (D4·R5).

    This is also the periodic-export hook: run it from cron (``exa audit checkpoint --anchor
    --skip-unchanged``) or call :func:`examlops.audit_worm.checkpoint_and_anchor` from a scheduler.
    """
    # FAIL CLOSED (item 0.7): if no signing key is configured we refuse rather than sign with a
    # well-known default - a checkpoint anyone can forge provides zero tamper-evidence, which is
    # worse than no checkpoint.
    from examlops.audit_worm import checkpoint_and_anchor
    from examlops.supplychain import SigningKeyMissing

    try:
        res = checkpoint_and_anchor(skip_if_unchanged=skip_unchanged)
    except SigningKeyMissing as exc:
        _output.error(
            f"Cannot sign audit checkpoint: {exc}. A checkpoint signed with a default key is "
            "forgeable and provides no tamper-evidence - refusing. Configure a real signing key "
            "(EXAMLOPS_SIGNING_KEY, or store secret 'model-signing/key' via exa secrets set)."
        )
        raise typer.Exit(1) from exc
    status = res["status"]
    if status == "empty":
        _output.info("No chained audit events yet - nothing to checkpoint.")
        return
    # An anchor that failed is said out loud in BOTH modes (stderr keeps stdout one JSON document);
    # this used to be swallowed by a bare `except: pass`.
    if res.get("anchor_error"):
        _output.warning(
            f"WORM anchor did not complete: {res['anchor_error']}"
            + (" - degraded to the local fallback file" if res.get("degraded") else "")
        )
    failed = anchor_required and not res.get("anchored")
    if _output.json_mode:
        _output.print_json(res)
    elif status == "unchanged":
        _output.ok(f"Head id {res['head_id']} already has an anchored checkpoint - unchanged.")
    else:
        where = (
            f", anchored to WORM ({res['backend']}) {res['worm_hash'][:12]}..."
            if res.get("worm_hash")
            else ""
        )
        _output.ok(
            f"Checkpoint signed over head id {res['head_id']} "
            f"(hash {res['head_hash'][:12]}..., key {res.get('key_id')}){where}."
        )
    if failed:
        if not _output.json_mode:
            _output.error("--anchor was requested but the checkpoint is not durably anchored.")
        raise typer.Exit(1)


@app.command("verify-worm")
def verify_worm() -> None:
    """Verify the external WORM anchor: its own chain + agreement with the DB checkpoints (item 2.4)."""
    from examlops.audit_worm import verify_worm as _verify

    result = _verify()
    if _output.json_mode:
        _output.print_json(result)
    elif result["ok"]:
        _output.ok(f"WORM anchor verified — {result['entries']} entry(ies) ({result['reason']}).")
    else:
        _output.error(f"WORM anchor FAILED: {result['reason']}")
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("checkpoints")
def checkpoints(
    limit: int = typer.Option(20, "--limit", "-n", help="Max checkpoints to show"),
) -> None:
    """List signed audit checkpoints."""
    from examlops.data.audit import list_audit_checkpoints

    rows = list_audit_checkpoints(limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No audit checkpoints signed yet.")
        return
    _output.print_table(
        "Audit Checkpoints",
        ["Time", "Head ID", "Head Hash", "Key"],
        [[r["ts"], str(r["head_id"]), r["head_hash"][:16] + "…", r["key_id"] or "—"] for r in rows],
    )


@app.command("export")
def export(
    out: str = typer.Option(..., "--out", help="Write the archival JSON export to this file"),
    before: str = typer.Option(None, "--before", help="Only events before this ISO timestamp"),
) -> None:
    """Archival export of the audit trail (D4·R4). Append-only — never deletes."""
    from examlops.data.audit import export_audit_events, write_audit_event

    events = export_audit_events(before_ts=before)
    with open(out, "w") as fh:
        json.dump(events, fh, indent=2, default=str)
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    write_audit_event("cli", actor, "audit_export", out, {"count": len(events), "before": before})
    _output.ok(f"Exported {len(events)} audit event(s) to {out} (retained in place, append-only).")


@app.command("prune")
def prune(
    before: str = typer.Option(
        None,
        "--before",
        help="Prune events older than this date (YYYY-MM-DD or ISO-8601); never newer than the "
        "retention floor (EXAMLOPS_AUDIT_RETENTION_DAYS)",
    ),
    execute: bool = typer.Option(
        False, "--execute", help="Actually delete (default is a dry run that changes nothing)"
    ),
    archive: str = typer.Option(
        None, "--archive", help="File to write the pruned rows to (required with --execute)"
    ),
    allow_unanchored: bool = typer.Option(
        False,
        "--allow-unanchored",
        help="Prune even though no WORM anchor is configured (the cut is then not off-platform)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Prune old audit events under the retention policy WITHOUT breaking the chain (ADR 0028).

    Dry run by default. Refused unless EXAMLOPS_AUDIT_RETENTION_DAYS is set, the chain verifies,
    a fresh signed checkpoint is anchored to the WORM store, and the deleted rows are archived.
    A signed prune record keeps ``exa audit verify`` passing over what remains.
    """
    from examlops.data.audit_retention import (
        RetentionRefused,
        effective_cutoff,
        execute_prune,
        plan_prune,
    )

    try:
        cutoff = effective_cutoff(before)
        if not execute:
            plan = plan_prune(cutoff)
            plan["dry_run"] = True
            if _output.json_mode:
                _output.print_json(plan)
            elif not plan["eligible"]:
                _output.ok(f"Dry run: nothing older than {cutoff} UTC to prune.")
            else:
                _output.info(
                    f"Dry run: would prune {plan['eligible']} event(s) (ids up to {plan['cut_id']}, "
                    f"older than {cutoff} UTC). Re-run with --execute --archive FILE."
                )
            return
        if not archive:
            _output.error("--execute requires --archive FILE (the deleted rows are archived first)")
            raise typer.Exit(1)
        plan = plan_prune(cutoff)
        if plan["eligible"] and not _output.confirm(
            f"[bold red]Permanently delete[/bold red] {plan['eligible']} audit event(s) older "
            f"than {cutoff} UTC? They are archived to {archive} first.",
            auto_yes=yes,
        ):
            _output.info("Cancelled.")
            return
        result = execute_prune(
            before, archive_path=archive, actor=_actor(), allow_unanchored=allow_unanchored
        )
    except RetentionRefused as exc:
        _output.error(f"Prune refused: {exc}")
        raise typer.Exit(1) from exc
    if _output.json_mode:
        _output.print_json(result)
        return
    if result["status"] == "nothing-to-prune":
        _output.ok("Nothing to prune.")
        return
    _output.ok(
        f"Pruned {result['pruned']} audit event(s) up to id {result['cut_id']}; archive "
        f"{result['archive']} (sha256 {result['archive_sha256'][:12]}...). "
        "`exa audit verify` still covers the retained chain."
    )


@app.command("maintain")
def maintain(
    once: bool = typer.Option(False, "--once", help="Run a single cycle then exit (cron/CI)"),
    interval: float = typer.Option(
        None,
        "--interval",
        help="Seconds between cycles (default EXAMLOPS_AUDIT_MAINTENANCE_SECONDS, 3600)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what one cycle would do; change nothing"
    ),
) -> None:
    """Run the scheduled audit maintenance: checkpoint + WORM + transparency log + retention.

    The same job the control plane runs in the background (ADR 0028). One cycle holds a
    cluster-wide lease, so running this next to the control plane never double-prunes.
    Retention pruning runs only with EXAMLOPS_AUDIT_PRUNE_SCHEDULED=1 and a retention policy.
    Exit 1 when a single cycle (--once / --dry-run) is degraded.
    """
    import threading

    from examlops import audit_maintenance as am

    if not once and not dry_run:
        every = interval if interval is not None else am.interval_seconds()
        if every <= 0:
            _output.error("the schedule is disabled (interval 0) - use --once for one cycle")
            raise typer.Exit(1)
        _output.info(f"Audit maintenance every {every:.0f}s - Ctrl-C to stop.")
        stop = threading.Event()
        try:
            while True:
                res = am.run_cycle()
                _output.info(f"cycle: {res['status']}")
                if stop.wait(timeout=max(am.MIN_INTERVAL_S, every)):
                    break
        except KeyboardInterrupt:
            _output.info("Stopped.")
        return
    res = am.run_cycle(dry_run=dry_run)
    if _output.json_mode:
        _output.print_json(res)
    elif res["status"] == "skipped":
        _output.warning(f"Skipped: {res['reason']}.")
    else:
        cp = res.get("checkpoint", {})
        pr = res.get("prune", {})
        _output.info(
            f"checkpoint: {cp.get('status')}"
            + (
                f" ({cp.get('reason') or cp.get('anchor_error') or cp.get('transparency_error')})"
                if cp.get("ok") is False
                else ""
            )
        )
        _output.info(
            f"prune: {pr.get('status')}"
            + (f" ({pr.get('reason')})" if pr.get("ok") is False else "")
        )
        if res["status"] == "degraded":
            _output.error(f"Audit maintenance degraded: {', '.join(res['failed_steps'])}.")
        else:
            _output.ok(f"Audit maintenance {res['status']}.")
    if res["status"] == "degraded":
        raise typer.Exit(1)


@app.command("maintenance-runs")
def maintenance_runs(
    limit: int = typer.Option(20, "--limit", "-n", help="Max runs to show"),
) -> None:
    """Show recent scheduled audit-maintenance cycles (is the schedule actually running?)."""
    from examlops.audit_maintenance import list_runs

    rows = list_runs(limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No audit maintenance cycle has run yet.")
        return
    _output.print_table(
        "Audit Maintenance Runs",
        ["Time", "Status", "Checkpoint", "Prune", "Holder"],
        [
            [
                str(r["ts"]),
                r["status"],
                str((r["result"] or {}).get("checkpoint", {}).get("status", "—"))
                if isinstance(r["result"], dict)
                else "—",
                str((r["result"] or {}).get("prune", {}).get("status", "—"))
                if isinstance(r["result"], dict)
                else "—",
                r.get("holder") or "—",
            ]
            for r in rows
        ],
    )


@app.command("verify-transparency")
def verify_transparency_cmd(
    limit: int = typer.Option(100, "--limit", "-n", help="Newest receipts to re-check"),
) -> None:
    """Re-check checkpoint receipts against the Rekor / Sigstore transparency log. Exit 1 on a failure."""
    from examlops.audit_transparency import verify_transparency

    result = verify_transparency(limit=limit)
    if result.get("warning"):
        _output.warning(result["warning"])
    if _output.json_mode:
        _output.print_json(result)
    elif result["ok"]:
        _output.ok(
            f"Transparency log ({result['backend']}): {result['checked']} receipt(s) verified"
            + ("" if result.get("set_checked", True) else " (log SET not checked: no log key)")
            + "."
        )
    else:
        _output.error(f"Transparency verification FAILED: {result['reason']}")
        for f in result.get("failures", []):
            _output.error(f"  head {f['head_id']} ({f['backend']}): {f['reason']}")
    if not result["ok"]:
        raise typer.Exit(1)
