"""Backup scheduler — the foreground loop the Compose sidecar runs.

Each cycle: create a bundle → prune per retention → (optionally) push off-site → audit the run. The
loop **survives any error** (a failed cycle is logged + audited, never fatal) and handles SIGTERM
cleanly so ``docker compose down`` finishes the current cycle and exits 0. ``once=True`` runs a
single cycle with no sleep — that is what the unit tests drive.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

from . import _config, bundle, remote, retention

log = logging.getLogger("examlops.backup.schedule")

_STOP = False


def _handle_sigterm(signum, frame) -> None:  # noqa: ANN001, ARG001
    global _STOP
    _STOP = True
    log.info("SIGTERM received — will exit after the current cycle")


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "exa-backup"


def _audit(action: str, target: str | None, details: dict[str, Any]) -> None:
    try:
        from examlops.data import init_db
        from examlops.data.audit import write_audit_event

        init_db()
        write_audit_event("exa-backup", _actor(), action, target, details)
    except Exception:  # noqa: BLE001 — auditing is best-effort
        pass


def run_cycle(
    *,
    out_dir: str,
    tiers: list[str],
    push: bool,
    retain: str,
    with_content: bool = False,
) -> bundle.BundleResult:
    """One backup cycle: create → prune → push → audit. Returns the bundle result."""
    res = bundle.create_bundle(out_dir, tiers=tiers, with_content=with_content, profile="scheduled")
    pruned: list[str] = []
    try:
        policy = _config.parse_retain(retain)
        pruned = retention.prune(
            out_dir, keep_n=policy.get("keep_n"), keep_days=policy.get("keep_days")
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("retention prune failed: %r", exc)
    pushed = False
    push_error: str | None = None
    push_uri: str | None = None
    if push:
        try:
            push_uri = remote.push(res.bundle_dir).get("uri")
            pushed = True
        except Exception as exc:  # noqa: BLE001 — off-site push never fails the local backup
            push_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("off-site push failed: %r", exc)
        # Carried on the result, not just logged. A cycle whose replication has never once worked
        # returned exactly what a fully replicated one returns, and the operator's last line was a
        # green tick — so a backup that existed only on the host it was taken from looked like a
        # disaster-recovery backup. The status stays untouched on purpose: the *local* bundle is
        # fine, and losing it over a broken off-site target would be the worse failure.
        res.offsite = {"requested": True, "ok": pushed, "error": push_error, "uri": push_uri}
    _audit(
        "backup_schedule_run",
        res.bundle_id,
        {
            "status": res.overall_status,
            "pushed": pushed,
            "push_error": push_error,
            "pruned": len(pruned),
        },
    )
    log.info(
        "backup cycle %s → %s (pushed=%s, pruned=%d)",
        res.bundle_id,
        res.overall_status,
        pushed,
        len(pruned),
    )
    return res


def run_scheduler(
    *,
    out_dir: str | None = None,
    tiers: list[str] | None = None,
    interval_s: int | None = None,
    push: bool | None = None,
    retain: str | None = None,
    with_content: bool = False,
    once: bool = False,
) -> bundle.BundleResult | None:
    """Run the scheduled backup loop (or a single cycle when ``once``). Config falls back to env."""
    cfg = _config.load()
    out_dir = out_dir or cfg.out_dir
    tiers = tiers or cfg.tiers
    interval_s = interval_s or cfg.interval_s
    retain = retain or cfg.retain
    push = cfg.s3_uri != "" if push is None else push

    if not once:
        signal.signal(signal.SIGTERM, _handle_sigterm)

    last: bundle.BundleResult | None = None
    while True:
        try:
            last = run_cycle(
                out_dir=out_dir, tiers=tiers, push=push, retain=retain, with_content=with_content
            )
        except Exception as exc:  # noqa: BLE001 — the loop must never die on one bad cycle
            log.error("backup cycle failed: %r", exc)
            _audit("backup_schedule_error", None, {"error": repr(exc)})
        if once or _STOP:
            return last
        # Sleep in short slices so SIGTERM is honoured promptly.
        slept = 0
        while slept < interval_s and not _STOP:
            time.sleep(min(5, interval_s - slept))
            slept += 5
