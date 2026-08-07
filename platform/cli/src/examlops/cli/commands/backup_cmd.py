"""``exa backup`` — whole-platform backup / restore (Phase 0 item 0.9, extended).

Thin CLI over :mod:`examlops.backup`. Two layers:

* the original single-DB commands (``create``/``list``/``verify``/``restore``) — an online, WAL-safe
  ``platform.db`` snapshot with a verifiable manifest and a guarded, re-verified restore (the DR
  drill), preserved byte-for-byte;
* the **tiered bundle** commands (``create --all``, ``verify-bundle``, ``restore-bundle``,
  ``schedule``, ``prune``, ``pull``, ``status``) — one backup covering every data store (all SQLite
  DBs, config, Postgres, MinIO), off-site replication, retention, and scheduling.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(no_args_is_help=True, help="Backup / restore the whole platform (tiered bundles)")

_EX_CREATE = "Examples:\n\n  [dim]# Fast control-plane bundle (all SQLite DBs + config) into ./backups[/dim]\n  exa backup create\n\n  [dim]# Everything — + Postgres + MinIO buckets, pushed off-site[/dim]\n  exa backup create --all --push\n\n  [dim]# Selective heavy tiers[/dim]\n  exa backup create --with-postgres --with-objects"
_EX_RESTORE = "Examples:\n\n  [dim]# Restore a single-DB snapshot into an empty DB[/dim]\n  exa backup restore ./backups/platform-20260718T120000Z.db\n\n  [dim]# Overwrite a non-empty DB (DANGEROUS)[/dim]\n  exa backup restore ./backups/<file>.db --force"
_EX_RESTORE_BUNDLE = "Examples:\n\n  [dim]# Restore the SQLite + config tiers of a bundle[/dim]\n  exa backup restore-bundle ./backups/examlops-backup-<ts>\n\n  [dim]# Restore everything incl. Postgres + objects (DANGEROUS)[/dim]\n  exa backup restore-bundle ./backups/examlops-backup-<ts> --tier sqlite --tier postgres --tier objects --force"


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
    all_tiers: bool = typer.Option(
        False, "--all", help="Full bundle: all SQLite DBs + config + Postgres + MinIO objects"
    ),
    bundle_flag: bool = typer.Option(
        False, "--bundle", help="Control-plane bundle (all SQLite DBs + config) instead of one .db"
    ),
    with_postgres: bool = typer.Option(False, "--with-postgres", help="Include the Postgres tier"),
    with_objects: bool = typer.Option(
        False, "--with-objects", help="Include the MinIO/object-store tier"
    ),
    with_content: bool = typer.Option(
        False, "--with-content", help="Include use-case packs / envs / .dualgit classification"
    ),
    push: bool = typer.Option(False, "--push", help="Replicate the finished bundle off-site (S3)"),
    strict: bool = typer.Option(
        False, "--strict", help="Fail (don't skip) any requested tier that can't run"
    ),
) -> None:
    """Snapshot the platform. Bare = single ``platform.db`` file; any tier flag = a tiered bundle."""
    from examlops import backup

    bundle_mode = all_tiers or bundle_flag or with_postgres or with_objects or with_content
    if not bundle_mode:
        # Legacy single-DB snapshot — unchanged behaviour + output.
        try:
            manifest = backup.create_backup(out)
        except FileNotFoundError as exc:
            _output.error(str(exc))
            raise typer.Exit(1) from exc
        _audit(
            "backup_created",
            manifest["backup_file"],
            {"sha256": manifest["sha256"][:16], "dir": out},
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
        return

    # Tiered bundle.
    tiers = ["sqlite", "config"]
    if all_tiers or with_postgres:
        tiers.append("postgres")
    if all_tiers or with_objects:
        tiers.append("objects")
    res = backup.create_bundle(
        out, tiers=tiers, strict=strict, with_content=all_tiers or with_content
    )
    if push:
        from examlops.backup import remote

        try:
            remote.push(res.bundle_dir)
        except Exception as exc:  # noqa: BLE001 — off-site push never fails the local bundle
            _output.warning(f"off-site push failed: {exc}")
    _audit("bundle_created", res.bundle_id, {"status": res.overall_status, "tiers": tiers})
    if _output.json_mode:
        _output.print_json(res.manifest)
        return
    _output.ok(f"Bundle written: {res.bundle_dir} (status={res.overall_status}).")
    for name, tier in res.manifest["tiers"].items():
        if name in tiers:
            reason = f" — {tier['reason']}" if tier.get("reason") else ""
            _output.info(f"  {name}: {tier['status']}{reason}")


@app.command("list")
def list_cmd(
    directory: str = typer.Option("./backups", "--dir", "-d", help="Backup directory"),
    remote: bool = typer.Option(False, "--remote", help="List off-site bundles (S3) instead"),
) -> None:
    """List available backups & bundles (newest first) with their manifest metadata."""
    from examlops import backup

    if remote:
        from examlops.backup import remote as _remote

        try:
            bundles = _remote.list_remote()
        except Exception as exc:  # noqa: BLE001
            _output.error(f"cannot list off-site: {exc}")
            raise typer.Exit(1) from exc
        if _output.json_mode:
            _output.print_json(bundles)
            return
        if not bundles:
            _output.info("No off-site bundles found.")
            return
        _output.print_table(
            "Off-site bundles",
            ["Bundle", "Created", "Status"],
            [[b["bundle_id"], b["created_at"] or "—", b["overall_status"] or "—"] for b in bundles],
        )
        return

    rows = backup.list_backups(directory)
    bundles = backup.list_bundles(directory)
    if _output.json_mode:
        _output.print_json({"snapshots": rows, "bundles": bundles})
        return
    if not rows and not bundles:
        _output.info(f"No backups found in {directory}.")
        return
    if rows:
        _output.print_table(
            f"Single-DB snapshots in {directory}",
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
    if bundles:
        _output.print_table(
            f"Bundles in {directory}",
            ["Bundle", "Created", "Status", "Profile", "Tiers"],
            [
                [
                    b["bundle_id"],
                    b["created_at"] or "—",
                    b["overall_status"] or "—",
                    b["profile"] or "—",
                    ",".join(b["tiers"]),
                ]
                for b in bundles
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
    backup.auto_backup_before("backup-restore")  # rollback point; best-effort, never blocks
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


@app.command("verify-bundle")
def verify_bundle_cmd(
    bundle_dir: str = typer.Argument(..., help="Path to a bundle directory"),
) -> None:
    """Verify a whole bundle: manifest + every tier item's checksum + platform.db audit chain."""
    from examlops import backup

    result = backup.verify_bundle(bundle_dir)
    if _output.json_mode:
        _output.print_json(result)
    elif result["ok"]:
        _output.ok(f"Bundle verified: {bundle_dir}")
    else:
        _output.error(f"Bundle FAILED verification ({result['reason']}): {result['checks']}")
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("restore-bundle", epilog=_EX_RESTORE_BUNDLE)
def restore_bundle_cmd(
    bundle_dir: str = typer.Argument(..., help="Path to a bundle directory to restore"),
    tier: list[str] = typer.Option(
        None, "--tier", help="Tier(s) to restore (repeatable). Default: sqlite + config"
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite non-empty targets (DANGEROUS)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Restore selected tiers from a verified bundle (guarded; verifies before touching anything)."""
    from examlops import backup

    tiers = list(tier) if tier else None
    label = ",".join(tiers) if tiers else "sqlite,config"
    if not (yes or _output.json_mode) and not typer.confirm(
        f"Restore tiers [{label}] from {bundle_dir}{' (FORCE)' if force else ''}?"
    ):
        _output.info("Aborted.")
        raise typer.Exit(1)
    backup.auto_backup_before("restore-bundle")
    try:
        result = backup.restore_bundle(bundle_dir, tiers=tiers, force=force)
    except (ValueError, Exception) as exc:  # noqa: BLE001
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    _audit("bundle_restored", bundle_dir, {"tiers": result["restored_tiers"], "forced": force})
    if _output.json_mode:
        _output.print_json(result)
        return
    _output.ok(f"Restored tiers {result['restored_tiers']} from {bundle_dir}.")


@app.command("schedule")
def schedule_cmd(
    interval: int = typer.Option(None, "--interval", help="Seconds between cycles (env default)"),
    tiers: str = typer.Option(None, "--tiers", help="Comma-separated tiers (env default)"),
    out: str = typer.Option(None, "--out", "-o", help="Backup directory (env default)"),
    push: bool = typer.Option(False, "--push", help="Replicate each bundle off-site"),
    all_tiers: bool = typer.Option(
        False, "--all", help="Shorthand for --tiers sqlite,config,postgres,objects"
    ),
    once: bool = typer.Option(False, "--once", help="Run a single cycle then exit (for tests/CI)"),
) -> None:
    """Run the scheduled backup loop (what the Compose ``backup`` sidecar runs)."""
    from examlops.backup import schedule

    tier_list = (
        ["sqlite", "config", "postgres", "objects"]
        if all_tiers
        else ([t.strip() for t in tiers.split(",") if t.strip()] if tiers else None)
    )
    res = schedule.run_scheduler(
        out_dir=out, tiers=tier_list, interval_s=interval, push=push or None, once=once
    )
    if once:
        if _output.json_mode:
            _output.print_json(res.manifest if res else {"ok": False})
        elif res:
            _output.ok(f"One cycle complete: {res.bundle_id} (status={res.overall_status}).")
        else:
            _output.warning("Cycle produced no bundle.")


@app.command("prune")
def prune_cmd(
    directory: str = typer.Option("./backups", "--dir", "-d", help="Backup directory"),
    keep: int = typer.Option(None, "--keep", help="Keep the newest N bundles"),
    days: int = typer.Option(None, "--days", help="Keep bundles newer than N days"),
) -> None:
    """Prune old bundles by count and/or age (never removes the newest / last-good bundle)."""
    from examlops.backup import retention

    if keep is None and days is None:
        _output.error("specify --keep and/or --days")
        raise typer.Exit(1)
    pruned = retention.prune(directory, keep_n=keep, keep_days=days)
    _audit("backup_pruned", directory, {"pruned": len(pruned)})
    if _output.json_mode:
        _output.print_json({"pruned": pruned})
        return
    _output.ok(f"Pruned {len(pruned)} bundle(s): {', '.join(pruned) or '—'}")


@app.command("pull")
def pull_cmd(
    bundle_id: str = typer.Argument(..., help="Off-site bundle id to fetch"),
    dest: str = typer.Option("./backups", "--dest", help="Directory to extract into"),
) -> None:
    """Download + extract an off-site bundle (verify it before restoring)."""
    from examlops.backup import remote

    try:
        path = remote.pull(bundle_id, dest)
    except Exception as exc:  # noqa: BLE001
        _output.error(f"pull failed: {exc}")
        raise typer.Exit(1) from exc
    if _output.json_mode:
        _output.print_json({"pulled": bundle_id, "path": path})
        return
    _output.ok(f"Pulled {bundle_id} → {path} (run `exa backup verify-bundle {path}` next).")


@app.command("status")
def status_cmd(
    directory: str = typer.Option("./backups", "--dir", "-d", help="Backup directory"),
) -> None:
    """Show the latest bundle, per-tier health, retention count, and off-site reachability."""
    from examlops import backup
    from examlops.backup import _config

    bundles = backup.list_bundles(directory)
    cfg = _config.load()
    latest = bundles[0] if bundles else None
    tier_health = {}
    if latest:
        import json
        from pathlib import Path

        mp = Path(latest["path"]) / "bundle.manifest.json"
        if mp.exists():
            m = json.loads(mp.read_text())
            tier_health = {k: v.get("status") for k, v in m.get("tiers", {}).items()}
    off_site = "configured" if cfg.s3_uri else "not configured"
    payload = {
        "backup_dir": directory,
        "bundle_count": len(bundles),
        "latest_bundle": latest["bundle_id"] if latest else None,
        "latest_status": latest["overall_status"] if latest else None,
        "latest_created": latest["created_at"] if latest else None,
        "tier_health": tier_health,
        "off_site": off_site,
        "off_site_uri": cfg.s3_uri or None,
        "retention": cfg.retain,
    }
    if _output.json_mode:
        _output.print_json(payload)
        return
    if not latest:
        _output.info(f"No bundles in {directory}. Off-site: {off_site}.")
        return
    _output.print_record(payload)
