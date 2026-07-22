"""Retention / rotation — prune old bundles locally (and, via ``remote``, off-site).

Keeps the most-recent ``keep_n`` bundles and/or bundles newer than ``keep_days``, deleting the rest.
Safety rails: never prune the single newest bundle, and never prune down to zero *good* bundles
(a bundle whose ``overall_status`` is not ``failed``).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_BUNDLE_GLOB = "examlops-backup-*"


def _bundle_meta(bd: Path) -> dict[str, Any]:
    mp = bd / "bundle.manifest.json"
    created = None
    status = "unknown"
    if mp.is_file():
        try:
            m = json.loads(mp.read_text())
            created = m.get("created_at")
            status = m.get("overall_status", "unknown")
        except Exception:  # noqa: BLE001
            pass
    # Fall back to mtime when the manifest is absent/unreadable.
    ts = None
    if created:
        try:
            ts = datetime.fromisoformat(created)
        except ValueError:
            ts = None
    if ts is None:
        ts = datetime.fromtimestamp(bd.stat().st_mtime, tz=UTC)
    return {"path": bd, "created": ts, "status": status}


def prune(
    directory: str,
    *,
    keep_n: int | None = None,
    keep_days: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Delete bundles beyond ``keep_n`` and/or older than ``keep_days``. Returns pruned bundle ids."""
    d = Path(directory)
    if not d.is_dir():
        return []
    bundles = sorted(
        (_bundle_meta(bd) for bd in d.glob(_BUNDLE_GLOB) if bd.is_dir()),
        key=lambda m: m["created"],
        reverse=True,  # newest first
    )
    if not bundles:
        return []

    to_delete: list[dict[str, Any]] = []
    if keep_n is not None:
        to_delete.extend(bundles[keep_n:])
    if keep_days is not None:
        ref = now or datetime.now(UTC)
        cutoff = ref.timestamp() - keep_days * 86400
        for m in bundles:
            if m["created"].timestamp() < cutoff and m not in to_delete:
                to_delete.append(m)

    # Safety: keep the newest bundle, and keep at least one non-failed bundle overall.
    survivors = [m for m in bundles if m not in to_delete]
    good_survivors = [m for m in survivors if m["status"] != "failed"]
    if not any(m["status"] != "failed" for m in bundles):
        good_survivors = survivors  # nothing good exists; don't invent a rule
    pruned: list[str] = []
    for m in to_delete:
        if m["path"] == bundles[0]["path"]:
            continue  # never delete the newest
        if not good_survivors and m["status"] != "failed":
            good_survivors = [m]  # keep this one as the last good bundle
            continue
        shutil.rmtree(m["path"], ignore_errors=True)
        pruned.append(m["path"].name)
    return pruned
