"""``exa backup`` — consistent backup / restore of the platform datastore (Phase 0 item 0.9).

Thin CLI over :mod:`examlops.backup`: an online, WAL-safe SQLite snapshot with a verifiable
manifest, a guarded + re-verified restore (the DR-drill round trip), and integrity checks.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(no_args_is_help=True, help="Backup / restore the platform datastore")

_EX_CREATE = "Examples:\n\n  [dim]# Snapshot into ./backups (default)[/dim]\n  exa backup create\n\n  [dim]# Custom dir[/dim]\n  exa backup create --out /srv/examlops/backups"
_EX_RESTORE = "Examples:\n\n  [dim]# Restore into an empty DB[/dim]\n  exa backup restore ./backups/platform-20260718T120000Z.db\n\n  [dim]# Overwrite a non-empty DB (DANGEROUS)[/dim]\n  exa backup restore ./backups/<file>.db --force"


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "exa-backup"


def _audit(action: str, target: str | None, details: dict) -> None:
    try:
        from examlops.data import init_db
        from examlops.data.audit import write_audit_event

        init_db()
        write_audit_event("exa-backup", _actor(), action, target, details)
    except Exception:  # noqa: BLE001 — auditing is best-effort, never fail the backup
        pass


@app.command("create", epilog=_EX_CREATE)
def create(
    out: str = typer.Option("./backups", "--out", "-o", help="Directory to write the backup into"),
) -> None:
    """Take an online, transactionally-consistent snapshot of the platform DB + write its manifest."""
    from examlops import backup

    try:
        manifest = backup.create_backup(out)
    except FileNotFoundError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    _audit(
        "backup_created", manifest["backup_file"], {"sha256": manifest["sha256"][:16], "dir": out}
    )
    if _output.json_mode:
        _output.print_json(manifest)
        return
    total_rows = sum(manifest["table_counts"].values())
    _output.ok(
        f"Backup written: {out}/{manifest['backup_file']} "
        f"({manifest['size_bytes']} bytes, {total_rows} rows across "
        f"{len(manifest['table_counts'])} tables, sha256 {manifest['sha256'][:12]}…)."
    )


@app.command("list")
def list_cmd(
    directory: str = typer.Option("./backups", "--dir", "-d", help="Backup directory"),
) -> None:
    """List available backups (newest first) with their manifest metadata."""
    from examlops import backup

    rows = backup.list_backups(directory)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info(f"No backups found in {directory}.")
        return
    _output.print_table(
        f"Backups in {directory}",
        ["File", "Created", "Size", "sha256", "Manifest"],
        [
            [
                r["file"],
                r["created_at"] or "—",
                str(r["size_bytes"]),
                (r["sha256"] or "—")[:12],
                "✓" if r["has_manifest"] else "✗",
            ]
            for r in rows
        ],
    )


@app.command("verify")
def verify(
    path: str = typer.Argument(..., help="Path to a backup .db file"),
) -> None:
    """Verify a backup: checksum vs manifest + SQLite integrity + audit hash-chain. Exit 1 if bad."""
    from examlops import backup

    result = backup.verify_backup(path)
    if _output.json_mode:
        _output.print_json(result)
    elif result["ok"]:
        _output.ok(f"Backup verified: {path}")
    else:
        _output.error(f"Backup FAILED verification ({result['reason']}): {result['checks']}")
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("restore", epilog=_EX_RESTORE)
def restore(
    path: str = typer.Argument(..., help="Path to a backup .db file to restore"),
    force: bool = typer.Option(
        False, "--force", help="Overwrite a non-empty target DB (DANGEROUS)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Restore a verified backup over the platform DB (guarded + re-verified after)."""
    from examlops import backup

    if not (yes or _output.json_mode) and not typer.confirm(
        f"Restore {path} over the platform DB{' (FORCE overwrite)' if force else ''}?"
    ):
        _output.info("Aborted.")
        raise typer.Exit(1)
    try:
        result = backup.restore_backup(path, force=force)
    except ValueError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    _audit("backup_restored", path, {"forced": force})
    if _output.json_mode:
        _output.print_json(result)
        return
    _output.ok(
        f"Restored {path} → {result['restored_to']} "
        f"(integrity={result['verification']['integrity_check']}, "
        f"audit_chain={'ok' if result['verification']['audit_chain_ok'] else 'BROKEN'})."
    )
