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
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event(
            "exa-upgrade",
            os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "exa-upgrade",
            action,
            "platform",
            details,
        )
    except Exception:  # noqa: BLE001 — auditing is best-effort; the upgrade row is the record
        pass


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
        res = create_bundle(out, tiers=tiers or ["sqlite", "config"], profile="pre-upgrade")
        backup_info = {"bundle": res.bundle_dir, "status": res.overall_status}
        if res.overall_status == "failed":
            return {**p, "applied": [], "backup": backup_info, "ok": False, "dry_run": False}

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
    after = _read_stamp()
    result = {
        **p,
        "applied": applied,
        "backup": backup_info,
        "stamp_after": after.to_dict() if after else None,
        "ok": True,
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
