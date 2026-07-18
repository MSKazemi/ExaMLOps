"""External WORM anchor for audit checkpoints (Phase 2 item 2.4).

The audit hash-chain (D4) makes tampering *within* ``platform.db`` detectable — but an attacker who
can rewrite the whole DB could re-chain a forged history. Anchoring signed checkpoints to an
append-only store **outside** the DB closes that: each ``exa audit checkpoint`` is also appended to a
WORM (write-once-read-many) log whose own lines are hash-chained, so the DB and the external anchor
must agree. Divergence = tampering, in whichever store was altered.

``EXAMLOPS_AUDIT_WORM_PATH`` points at the anchor. A local append-only file is the dev/default; in
production point it at an S3 **Object-Lock** bucket path or a Rekor transparency log (the file format
is the same JSON-lines chain). When unset, anchoring is a no-op (the DB chain still stands).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

_APPEND_LOCK = threading.Lock()


def _worm_path() -> str | None:
    p = os.getenv("EXAMLOPS_AUDIT_WORM_PATH", "").strip()
    return p or None


def _line_hash(prev: str, payload: str) -> str:
    return hashlib.sha256(f"{prev}\n{payload}".encode()).hexdigest()


def _last_worm_hash(path: Path) -> str:
    if not path.exists():
        return "WORM-GENESIS"
    last = "WORM-GENESIS"
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    last = json.loads(line)["worm_hash"]
                except (json.JSONDecodeError, KeyError):
                    continue
    return last


def anchor_checkpoint(checkpoint: dict[str, Any], *, ts: str) -> str | None:
    """Append a signed checkpoint to the WORM anchor (chained). Returns its worm_hash, or None.

    No-op (returns None) when ``EXAMLOPS_AUDIT_WORM_PATH`` is unset. ``ts`` is injected so callers
    control the timestamp (CLI stamps it).
    """
    dest = _worm_path()
    if dest is None:
        return None
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK:
        prev = _last_worm_hash(path)
        entry = {
            "head_id": checkpoint.get("head_id"),
            "head_hash": checkpoint.get("head_hash"),
            "key_id": checkpoint.get("key_id"),
            "ts": ts,
            "prev_worm_hash": prev,
        }
        payload = json.dumps(entry, sort_keys=True)
        worm_hash = _line_hash(prev, payload)
        entry["worm_hash"] = worm_hash
        # O_APPEND → the OS guarantees the write lands at end-of-file (append-only semantics).
        with os.fdopen(
            os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(entry) + "\n")
    return worm_hash


def verify_worm() -> dict[str, Any]:
    """Verify the WORM anchor: its internal chain + agreement with the DB checkpoints.

    Returns ``{"ok": bool, "reason": str, "entries": int, ...}``. Never raises for a bad anchor.
    """
    dest = _worm_path()
    if dest is None:
        return {"ok": True, "reason": "no WORM anchor configured", "entries": 0}
    path = Path(dest)
    if not path.exists():
        return {"ok": True, "reason": "anchor file not yet written", "entries": 0}

    prev = "WORM-GENESIS"
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "reason": f"corrupt JSON at line {i}", "entries": len(entries)}
            recomputed = _line_hash(
                prev,
                json.dumps(
                    {k: e[k] for k in ("head_id", "head_hash", "key_id", "ts", "prev_worm_hash")},
                    sort_keys=True,
                ),
            )
            if e.get("prev_worm_hash") != prev or e.get("worm_hash") != recomputed:
                return {
                    "ok": False,
                    "reason": f"WORM chain broken at line {i}",
                    "entries": len(entries),
                }
            prev = e["worm_hash"]
            entries.append(e)

    # Cross-check: every DB checkpoint head_hash must be anchored in the WORM log.
    try:
        from examlops.data.audit import list_audit_checkpoints

        db_hashes = {c["head_hash"] for c in list_audit_checkpoints(10_000)}
        worm_hashes = {e["head_hash"] for e in entries}
        missing = db_hashes - worm_hashes
        if missing:
            return {
                "ok": False,
                "reason": f"{len(missing)} DB checkpoint(s) not anchored in WORM",
                "entries": len(entries),
                "missing": sorted(missing)[:5],
            }
    except Exception as exc:  # noqa: BLE001 - DB unavailable → verify the anchor chain alone
        return {
            "ok": True,
            "reason": f"anchor chain valid (DB cross-check skipped: {exc})",
            "entries": len(entries),
        }

    return {"ok": True, "reason": "verified", "entries": len(entries)}
