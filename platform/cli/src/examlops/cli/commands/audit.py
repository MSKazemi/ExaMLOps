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
    "  exa audit checkpoint"
)

app = typer.Typer(
    help="Platform audit log — tamper-evident, hash-chained (D4)",
    no_args_is_help=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


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
