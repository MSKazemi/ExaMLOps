from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

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
    init_db()
    days = _parse_days(last)
    since = datetime.utcnow() - timedelta(days=days)
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")

    query = "SELECT id, ts, source, actor, action, target, details FROM audit_events WHERE ts >= ?"
    params: list = [since_str]
    if model:
        query += " AND target=?"
        params.append(model)
    if action:
        query += " AND action=?"
        params.append(action)
    if source:
        query += " AND source=?"
        params.append(source)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        _output.ok("No audit events found for the given filters")
        return

    if _output.json_mode:
        _output.print_json(
            [
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "source": r["source"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "target": r["target"],
                    "details": json.loads(r["details"]) if r["details"] else None,
                }
                for r in rows
            ]
        )
        return

    cols = ["Time", "Source", "Actor", "Action", "Target", "Details"]
    table_rows = [
        [
            r["ts"],
            r["source"] or "—",
            r["actor"] or "—",
            r["action"],
            r["target"] or "—",
            (r["details"] or "")[:50],
        ]
        for r in rows
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
) -> None:
    """Every autonomous action in the window, and whether it declared an inverse.

    This is the W2 gate as a command: for each action the platform took on its own initiative,
    who acted, on whose behalf, under which mode, and how it would be undone. An action with no
    ``rollback_ref`` is listed rather than filtered out — ADR 0110 decision 4 calls that a policy
    violation, and hiding them would defeat the point of asking.
    """
    from examlops.data.audit import autonomous_actions

    rows = autonomous_actions(since_days=_parse_days(last))
    if not rows:
        _output.ok(f"No autonomous actions recorded in the last {last}")
        return
    undoable = sum(1 for r in rows if r["undoable"])
    if _output.json_mode:
        _output.print_json(
            {"window": last, "count": len(rows), "undoable": undoable, "actions": rows}
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
    if undoable < len(rows):
        _output.warning(
            f"{len(rows) - undoable} of {len(rows)} autonomous action(s) declared no inverse. "
            "ADR 0110 decision 4 treats a NULL rollback_ref on an autonomous action as a policy "
            "violation; the refusal that enforces it is not built yet, so these are reported."
        )


@app.command("verify")
def verify() -> None:
    """Recompute the hash chain and report integrity (D4·R2/R6). Exit 1 if broken."""
    from examlops.data.audit import verify_audit_chain

    result = verify_audit_chain()
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
        # Say what was NOT verified. A green tick over a partially-read log is the failure this
        # exists to prevent: until 2026-09-02 every dashboard-written event was unchained, and
        # this command reported success with a count that silently excluded all of them.
        if result.get("unchained"):
            _output.warning(result["warning"])
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
def checkpoint() -> None:
    """Sign the current chain head, producing a detached checkpoint signature (D4·R5)."""
    from examlops.data.audit import audit_chain_head, sign_audit_checkpoint

    head = audit_chain_head()
    if head is None:
        _output.info("No chained audit events yet — nothing to checkpoint.")
        return
    # Sign the head hash with the D7-managed signing key (D3's HMAC helper). FAIL CLOSED
    # (item 0.7): if no signing key is configured we refuse rather than sign with a
    # well-known default — a checkpoint anyone can forge provides zero tamper-evidence,
    # which is worse than no checkpoint.
    from examlops.supplychain import SigningKeyMissing, _hmac_sign

    try:
        signature = _hmac_sign(head["hash"])
    except SigningKeyMissing as exc:
        _output.error(
            f"Cannot sign audit checkpoint: {exc}. A checkpoint signed with a default key is "
            "forgeable and provides no tamper-evidence — refusing. Configure a real signing key "
            "(EXAMLOPS_SIGNING_KEY, or store secret 'model-signing/key' via exa secrets set)."
        )
        raise typer.Exit(1) from exc
    key_id = "d3-hmac"
    cp = sign_audit_checkpoint(signature, key_id=key_id)
    if cp is None:
        # None means the chain has no head to sign over. The caller checked that a moment ago,
        # so this is the concurrent-truncation case; refusing beats anchoring an empty dict to
        # a WORM store, which would look like a valid checkpoint forever after.
        _output.error("The audit chain has no head to checkpoint — nothing was signed.")
    # Anchor to the external WORM store (item 2.4) so the checkpoint is tamper-evident even against
    # a full-DB rewrite. Best-effort + no-op when EXAMLOPS_AUDIT_WORM_PATH is unset.
    worm_hash = None
    try:
        from datetime import UTC, datetime

        from examlops.audit_worm import anchor_checkpoint

        worm_hash = anchor_checkpoint(cp, ts=datetime.now(UTC).isoformat(timespec="seconds"))
    except Exception:  # noqa: BLE001 - anchoring is best-effort
        pass
    if _output.json_mode:
        _output.print_json({**cp, "worm_hash": worm_hash})
        return
    anchored = f", anchored to WORM {worm_hash[:12]}…" if worm_hash else ""
    _output.ok(
        f"Checkpoint signed over head id {cp['head_id']} "
        f"(hash {cp['head_hash'][:12]}…, key {key_id}){anchored}."
    )


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
