"""Auto-backup hook — a fast, best-effort control-plane bundle before a risky operation.

Wired (guarded, non-fatal) into destructive paths — ``backup restore``, secrets rekey, forced
schema init, autopilot promote — so there is always a rollback point. It **never raises and never
blocks** the operation: a risky op must proceed even if the pre-op backup fails (the failure is
logged + audited). Only the light ``sqlite`` + ``config`` tiers run, so it is quick.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("examlops.backup.auto")


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "exa-backup"


def auto_backup_before(op_name: str) -> str | None:
    """Create a fast control-plane bundle before ``op_name``. Returns the bundle dir, or None.

    Best-effort: any failure is swallowed (logged + audited) so the caller's operation is never
    blocked by a backup problem.
    """
    try:
        from . import bundle

        out = os.getenv("EXAMLOPS_BACKUP_DIR", "./backups")
        res = bundle.create_bundle(
            out, tiers=["sqlite", "config"], strict=False, profile=f"auto:{op_name}"
        )
        _audit(res.bundle_id, op_name, res.overall_status)
        return res.bundle_dir
    except Exception as exc:  # noqa: BLE001 — a risky op is never blocked by a backup failure
        log.warning("auto-backup before %s failed: %r", op_name, exc)
        return None


def _audit(bundle_id: str, op: str, status: str) -> None:
    try:
        from examlops.data import init_db
        from examlops.data.audit import write_audit_event

        init_db()
        write_audit_event(
            "exa-backup", _actor(), "auto_backup", bundle_id, {"op": op, "status": status}
        )
    except Exception:  # noqa: BLE001
        pass
