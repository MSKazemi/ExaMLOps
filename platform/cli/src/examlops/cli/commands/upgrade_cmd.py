"""``exa upgrade`` — bring an instance's data forward to the installed release (ADR 0128)."""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(
    no_args_is_help=True,
    help="Upgrade this instance's data to the installed release — plan, apply, history",
)

_EX_PLAN = (
    "Examples:\n\n"
    "  [dim]# After installing a new release: what would change, and is it safe?[/dim]\n"
    "  exa upgrade plan\n\n"
    "  exa --json upgrade plan"
)
_EX_APPLY = (
    "Examples:\n\n"
    "  [dim]# Back up, then run every pending migration[/dim]\n"
    "  exa upgrade apply\n\n"
    "  [dim]# Preview only[/dim]\n"
    "  exa upgrade apply --dry-run\n\n"
    "  [dim]# Include Postgres + object storage in the pre-upgrade backup[/dim]\n"
    "  exa upgrade apply --tier sqlite --tier config --tier postgres --tier objects"
)
_EX_HISTORY = (
    "Examples:\n\n"
    "  [dim]# Every create / adopt / migration this datastore has seen[/dim]\n"
    "  exa upgrade history\n\n"
    "  exa --json upgrade history --limit 10"
)


def _print_plan(p: dict) -> None:
    compat = p["compatibility"]
    stamp = p["stamp"] or {}
    _output.print_record(
        {
            "release": f"{p['code_version']} (data format {p['code_data_format']})",
            "deployment": p["deployment"],
            "data root": p["data_root"] or "(not set)",
            "data": f"format {stamp.get('data_format', '—')} · last opened with "
            f"{stamp.get('last_opened_with', '—')}",
            "verdict": f"{compat['status']} — {compat['message']}",
        }
    )
    if p["pending"]:
        _output.print_table(
            "Pending migrations",
            ["Version", "Name", "Online", "Breaking", "Description"],
            [
                [m["version"], m["name"], m["online"], m["breaking"], m["description"]]
                for m in p["pending"]
            ],
        )
    for w in p.get("site_profile_warnings", []):
        _output.warning(f"site profile: {w}")
    for b in p["blockers"]:
        _output.warning(f"blocked: {b}")


@app.command("plan", epilog=_EX_PLAN)
def plan() -> None:
    """What the installed release makes of this instance's data. Exit 1 if it must not open it."""
    from examlops.lifecycle import upgrade

    p = upgrade.plan()
    if _output.json_mode:
        _output.print_json(p)
    else:
        _print_plan(p)
        if p["ready"] and not p["pending"]:
            _output.ok("Nothing to migrate — the data is at this release's format.")
    if not p["ready"]:
        raise typer.Exit(1)


@app.command("apply", epilog=_EX_APPLY)
def apply(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would run; change nothing"),
    no_backup: bool = typer.Option(
        False, "--no-backup", help="Skip the pre-upgrade backup bundle (not recommended)"
    ),
    backup_dir: str = typer.Option(
        "", "--backup-dir", help="Where to write the pre-upgrade bundle (default: backup dir)"
    ),
    tier: list[str] = typer.Option(
        [], "--tier", help="Backup tier(s) for the pre-upgrade bundle (default: sqlite, config)"
    ),
) -> None:
    """Back up the data, then run every pending migration and restamp it."""
    from examlops.lifecycle import upgrade

    if not dry_run:
        p = upgrade.plan()
        if p["ready"] and p["pending"] and not _output.yes_mode:
            names = ", ".join(m["name"] for m in p["pending"])
            if not _output.confirm(f"Apply {len(p['pending'])} migration(s): {names}?"):
                _output.error("Aborted — nothing changed.")
    res = upgrade.apply(
        dry_run=dry_run,
        backup=not no_backup,
        backup_dir=backup_dir or None,
        tiers=tier or None,
    )
    if _output.json_mode:
        _output.print_json(res)
    else:
        _print_plan(res)
        if res.get("backup"):
            # Not a green tick when the bundle captured nothing — that line used to be the last
            # thing a refused upgrade printed, above a bare exit 1.
            line = f"Pre-upgrade backup: {res['backup']['bundle']} ({res['backup']['status']})"
            (_output.ok if res["backup"]["status"] in ("ok", "partial") else _output.warning)(line)
        if res.get("reason"):
            # A pre-flight refusal sets no `pending_after`, so the branch below never matched it
            # and the operator was stopped without being told why.
            _output.error(res["reason"])
        if dry_run:
            _output.info("Dry run — nothing changed.")
        elif res["ok"]:
            after = res.get("stamp_after") or {}
            applied = ", ".join(res["applied"]) or "none pending"
            _output.ok(f"Data at format {after.get('data_format', '—')} — applied: {applied}")
        elif res.get("pending_after"):
            # Not the same as "nothing to do", and the difference is the operator's next move.
            names = ", ".join(m["name"] for m in res["pending_after"])
            _output.error(
                f"{len(res['pending_after'])} migration(s) did not apply and are still pending: "
                f"{names}. The data was not left half-migrated — each migration runs in its own "
                "transaction — so it is safe to run this again; if it keeps happening, another "
                "process is upgrading the same datastore."
            )
    if not res["ok"]:
        raise typer.Exit(1)


@app.command("history", epilog=_EX_HISTORY)
def history(limit: int = typer.Option(50, "--limit", "-n", help="Rows to show")) -> None:
    """Every create, adopt, migration and restore this datastore has recorded."""
    from examlops.lifecycle import upgrade

    rows = upgrade.history(limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No lifecycle events recorded yet.")
        return
    _output.print_table(
        "Instance data history",
        ["When", "Kind", "From", "To", "Migration", "Release", "Actor", "Backup"],
        [
            [
                r["ts"],
                r["kind"],
                r["from_format"] if r["from_format"] is not None else "—",
                r["to_format"],
                r["migration"] or "—",
                r["to_version"],
                r["actor"],
                r["backup_id"] or "—",
            ]
            for r in rows
        ],
    )
