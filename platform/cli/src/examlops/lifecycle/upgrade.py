"""Upgrading an instance onto a new ExaMLOps release (ADR 0128).

Installing a new release replaces the *core* layer only. Its *instance data* is then brought
forward in place:

* ``plan`` — what the new code thinks of the data: the stamp, the compatibility verdict, the
  migrations it would run, and whether the site profile still names only modules this release
  knows. Read-only.
* ``apply`` — take a verified backup bundle of the data first (the migration's undo), then run
  every pending migration — online and offline — each in its own transaction, advancing the
  stamp and writing one ``platform_upgrades`` row and one audit event per step.

Online migrations also run by themselves the first time any process opens the datastore, so a
plain release upgrade needs no operator step; ``apply`` exists for the offline ones and for an
operator who wants the backup and the record before anything moves.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from examlops.backup import postgres_tier as _pg
from examlops.lifecycle import dataformat as _fmt
from examlops.lifecycle import migrations as _mig


def _read_stamp() -> _fmt.Stamp | None:
    """The stamp, read on a raw connection — *not* through ``init_db``, which refuses too-new data
    and would therefore hide the very verdict ``plan`` exists to report."""
    from examlops.data import get_db

    try:
        with get_db() as conn:
            return _fmt.read_stamp(conn)
    except Exception:  # noqa: BLE001 — an unreachable datastore has no stamp to report
        return None


def plan() -> dict[str, Any]:
    """The upgrade plan for this instance. Never writes."""
    from examlops.lifecycle import modules
    from examlops.lifecycle.datadir import data_root, deployment_kind

    stamp = _read_stamp()
    compat = _fmt.evaluate_stamp(stamp)
    profile = modules.resolve()
    blockers: list[str] = []
    if compat.status == _fmt.TOO_NEW:
        blockers.append(compat.message)
    return {
        "code_version": _fmt.code_version(),
        "code_data_format": _mig.code_format(_mig.MIGRATIONS),
        "deployment": deployment_kind(),
        "data_root": str(data_root()) if data_root() else None,
        "stamp": stamp.to_dict() if stamp else None,
        "compatibility": compat.to_dict(),
        "pending": compat.pending,
        "site_profile_warnings": profile.warnings,
        "blockers": blockers,
        "ready": not blockers,
    }


def _backup_dir(explicit: str | None) -> str:
    if explicit:
        return explicit
    from examlops.lifecycle.datadir import data_path

    if (p := data_path("backups")) is not None and not os.getenv("EXAMLOPS_BACKUP_DIR"):
        return str(p)
    from examlops.backup._config import load

    return load().out_dir


def _audit(action: str, details: dict[str, Any]) -> None:
    # Best-effort, as before — an upgrade is never failed by the audit log, and the
    # `platform_upgrades` row written by `dataformat` is an independent record of what ran. That
    # argument is a reason not to *fail*, not a reason not to *count*: a separate record does not
    # make this log complete, and a silent drop left `dropped_audit_events()` at zero.
    from examlops.data.audit import audit_best_effort

    audit_best_effort(
        "exa-upgrade",
        os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "exa-upgrade",
        action,
        "platform",
        details,
    )


def default_backup_tiers() -> list[str]:
    """Tiers the pre-upgrade bundle must capture, for the engine that is actually in use.

    **The rollback point has to hold the data being migrated.** The default was
    ``["sqlite", "config"]`` on every engine — but under ``EXAMLOPS_DB_BACKEND=postgres`` the
    sqlite tier skips the platform DB *on purpose* (its state is in Postgres), so that bundle
    captured none of what the migration was about to change. It still reported ``partial`` because
    the config tier succeeded, and a partial bundle is allowed through.

    The sqlite tier makes the same argument about the file it declines to copy: "backing up the
    leftover file here would produce a bundle that looks complete and restores nothing."
    """
    from examlops.backup.postgres_tier import platform_dsn

    return ["postgres" if platform_dsn() else "sqlite", "config"]


def _captured(manifest: dict[str, Any], tier: str) -> bool:
    """Whether the bundle actually holds something for ``tier``.

    A tier can be requested and still capture nothing — `pg_dump` absent, the server unreachable,
    the file missing — and it is then present in the manifest with zero ``ok`` items. Asking the
    manifest what it holds is the only way to tell a rollback point from a directory.
    """
    body = (manifest.get("tiers") or {}).get(tier) or {}
    return any((item or {}).get("status") == "ok" for item in (body.get("items") or []))


def apply(
    *,
    dry_run: bool = False,
    backup: bool = True,
    backup_dir: str | None = None,
    tiers: list[str] | None = None,
) -> dict[str, Any]:
    """Bring the data to this release's format. See the module docstring."""
    p = plan()
    if not p["ready"]:
        return {**p, "applied": [], "backup": None, "ok": False, "dry_run": dry_run}
    pend = _mig.pending(
        p["stamp"]["data_format"] if p["stamp"] else _fmt.BASELINE_FORMAT, _mig.MIGRATIONS
    )
    if dry_run:
        return {**p, "applied": [], "backup": None, "ok": True, "dry_run": True}

    backup_info: dict[str, Any] | None = None
    if backup and (pend or p["stamp"] is None):
        from examlops.backup import create_bundle

        out = _backup_dir(backup_dir)
        Path(out).mkdir(parents=True, exist_ok=True)
        res = create_bundle(out, tiers=tiers or default_backup_tiers(), profile="pre-upgrade")
        backup_info = {"bundle": res.bundle_dir, "status": res.overall_status}
        # `failed` is not the only status that means "no rollback point". A bundle whose every
        # requested tier was skipped reports `skipped` — a directory with a manifest and no data in
        # it — and migrating on that is exactly what this step exists to prevent. `partial` is
        # allowed through: at least one requested tier was captured, and refusing it would block
        # every instance whose (say) config tier has nothing to snapshot.
        if res.overall_status in ("failed", "skipped"):
            return {
                **p,
                "applied": [],
                "backup": backup_info,
                "ok": False,
                "dry_run": False,
                "reason": (
                    f"refusing to upgrade: the pre-upgrade backup is '{res.overall_status}' — it "
                    "captured nothing, so there would be no rollback point. Check the backup tiers "
                    "(`exa backup create --all` and `exa backup verify-bundle`), or re-run with "
                    "`--no-backup` if you have a rollback point of your own."
                ),
            }

    # Requesting the right tier is not the same as getting it: a `partial` bundle whose platform
    # tier captured nothing is still no rollback point for the data about to be migrated.
    if backup_info is not None:
        state_tier = "postgres" if _pg.platform_dsn() else "sqlite"
        if not _captured(getattr(res, "manifest", {}) or {}, state_tier):
            return {
                **p,
                "applied": [],
                "backup": backup_info,
                "ok": False,
                "dry_run": False,
                "reason": (
                    f"refusing to upgrade: the pre-upgrade backup captured nothing for the "
                    f"'{state_tier}' tier, which is where this instance keeps its platform state — "
                    "so it could not roll back the data this migration changes. Check that tier "
                    "(`exa backup create --tier "
                    + state_tier
                    + "` then `exa backup verify-bundle`), "
                    "or re-run with `--no-backup` if you have a rollback point of your own."
                ),
            }

    # Schema first (additive DDL + stamp + online migrations), then whatever is still pending.
    from examlops.data import get_db, init_db

    init_db(force=True)
    applied: list[str] = []
    bid = Path(backup_info["bundle"]).name if backup_info else None
    for m in pend:
        with get_db() as conn:
            stamp = _fmt.read_stamp(conn)
            if stamp is not None and stamp.data_format >= m.version:
                continue  # an online step init_db already ran
            applied.extend(_fmt.apply_migrations(conn, [m], kind="upgrade", backup_id=bid))
    # Ask the data what happened rather than asserting it. `ok` used to be the literal `True`,
    # which made "every migration ran" and "the compare-and-set lost the race so none of them did"
    # the same answer — and since the CLI prints an empty `applied` as "none pending", an upgrade
    # that moved nothing read exactly like an instance that had nothing to move.
    after = _read_stamp()
    still_pending = _mig.pending(
        after.data_format if after else _fmt.BASELINE_FORMAT, _mig.MIGRATIONS
    )
    result = {
        **p,
        "applied": applied,
        "backup": backup_info,
        "stamp_after": after.to_dict() if after else None,
        "pending_after": _fmt._pending_dicts(still_pending),
        "ok": not still_pending,
        "dry_run": False,
    }
    _audit(
        "platform_upgraded",
        {
            "applied": applied,
            "to_format": after.data_format if after else None,
            "version": _fmt.code_version(),
            "backup": bid,
        },
    )
    return result


def history(limit: int = 50) -> list[dict[str, Any]]:
    from examlops.data import get_db

    try:
        with get_db() as conn:
            return _fmt.history(conn, limit)
    except Exception:  # noqa: BLE001
        return []
