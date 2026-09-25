"""External WORM anchor for audit checkpoints (Phase 2 item 2.4).

The audit hash-chain (D4) makes tampering *within* ``platform.db`` detectable — but an attacker who
can rewrite the whole DB could re-chain a forged history. Anchoring signed checkpoints to an
append-only store **outside** the DB closes that: each ``exa audit checkpoint`` is also appended to a
WORM (write-once-read-many) log whose own lines are hash-chained, so the DB and the external anchor
must agree. Divergence = tampering, in whichever store was altered.

``EXAMLOPS_AUDIT_WORM_PATH`` points at the anchor. Two targets are built:

* a **local append-only file** (the dev/default, one JSON line per entry), and
* an **S3 Object-Lock** location, ``s3://bucket/prefix`` (ADR 0028 decision 2). Each entry is one
  object ``<prefix>/<seq>.json`` written with ``ObjectLockMode`` + ``ObjectLockRetainUntilDate``
  and ``If-None-Match: *`` so an existing entry is never overwritten. The bucket must have been
  created with Object Lock enabled; the client is boto3 (already a dependency of the ``backup``
  extra) and is imported lazily. If the S3 write fails the failure is **counted**
  (:func:`anchor_failures`), logged at ERROR and the entry degrades to a local fallback file
  (``EXAMLOPS_AUDIT_WORM_FALLBACK_PATH``); the caller is told it degraded, never that it anchored.

A third, independent target is a **transparency log** (Rekor, or keyless Sigstore):
:func:`checkpoint_and_anchor` also logs each new checkpoint there when
``EXAMLOPS_AUDIT_TRANSPARENCY``/``EXAMLOPS_AUDIT_REKOR_URL`` is set (:mod:`examlops.audit_transparency`).
When the variable is unset, anchoring is a no-op (the DB chain still stands).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_APPEND_LOCK = threading.Lock()
_GENESIS = "WORM-GENESIS"

#: Anchor writes this process attempted and lost, keyed by backend. Process-local by design (the
#: same shape as ``data.audit.dropped_audit_events``): a loss cannot be recorded in the store that
#: is unreachable, so the count is what makes it visible.
_ANCHOR_FAILURES: dict[str, int] = {}


class WormAnchorError(RuntimeError):
    """An anchor write could not be completed (and no fallback was possible)."""


def anchor_failures() -> dict[str, int]:
    """Failed anchor writes by backend since process start (never silently swallowed)."""
    return dict(_ANCHOR_FAILURES)


def reset_anchor_failures() -> None:
    _ANCHOR_FAILURES.clear()


def _count_failure(backend: str) -> None:
    _ANCHOR_FAILURES[backend] = _ANCHOR_FAILURES.get(backend, 0) + 1


def _worm_path() -> str | None:
    p = os.getenv("EXAMLOPS_AUDIT_WORM_PATH", "").strip()
    return p or None


def _line_hash(prev: str, payload: str) -> str:
    return hashlib.sha256(f"{prev}\n{payload}".encode()).hexdigest()


def _last_worm_hash(path: Path) -> str:
    if not path.exists():
        return _GENESIS
    last = _GENESIS
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    last = json.loads(line)["worm_hash"]
                except (json.JSONDecodeError, KeyError):
                    continue
    return last


def _is_s3(dest: str) -> bool:
    return dest.lower().startswith("s3://")


def _parse_s3(dest: str) -> tuple[str, str]:
    rest = dest[len("s3://") :]
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise WormAnchorError(f"not a valid s3 anchor location: {dest!r}")
    return bucket, prefix.strip("/")


def _s3_client():  # noqa: ANN202 - a boto3 client or a test double
    """S3 client for the anchor. Test seam; own endpoint/credentials, else the MinIO ones."""
    try:
        import boto3  # noqa: PLC0415 - lazy: optional (examlops[backup])
    except ImportError as exc:
        raise WormAnchorError("boto3 not installed - pip install 'examlops[backup]'") from exc
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("EXAMLOPS_AUDIT_WORM_S3_ENDPOINT")
        or os.getenv("MLFLOW_S3_ENDPOINT_URL"),
        aws_access_key_id=os.getenv("EXAMLOPS_AUDIT_WORM_S3_ACCESS_KEY")
        or os.getenv("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.getenv("EXAMLOPS_AUDIT_WORM_S3_SECRET_KEY")
        or os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin"),
    )


def _lock_settings() -> tuple[str, int]:
    mode = os.getenv("EXAMLOPS_AUDIT_WORM_S3_MODE", "GOVERNANCE").strip().upper()
    if mode not in {"GOVERNANCE", "COMPLIANCE"}:
        raise WormAnchorError(
            f"EXAMLOPS_AUDIT_WORM_S3_MODE must be GOVERNANCE or COMPLIANCE, got {mode!r}"
        )
    try:
        days = int(os.getenv("EXAMLOPS_AUDIT_WORM_S3_RETAIN_DAYS", "365"))
    except ValueError as exc:
        raise WormAnchorError("EXAMLOPS_AUDIT_WORM_S3_RETAIN_DAYS must be an integer") from exc
    if days < 1:
        raise WormAnchorError("EXAMLOPS_AUDIT_WORM_S3_RETAIN_DAYS must be >= 1")
    return mode, days


def _fallback_path() -> Path:
    explicit = os.getenv("EXAMLOPS_AUDIT_WORM_FALLBACK_PATH", "").strip()
    if explicit:
        return Path(explicit)
    root = os.getenv("EXAMLOPS_DATA_DIR", "").strip()
    return Path(root or ".") / "audit-worm-fallback.jsonl"


def _error_code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str((resp.get("Error") or {}).get("Code", ""))
    return ""


def _is_conflict(exc: BaseException) -> bool:
    return _error_code(exc) in {
        "PreconditionFailed",
        "ConditionalRequestConflict",
        "412",
        "ObjectAlreadyExists",
    }


def _make_entry(prev: str, checkpoint: dict[str, Any], ts: str) -> dict[str, Any]:
    entry = {
        "head_id": checkpoint.get("head_id"),
        "head_hash": checkpoint.get("head_hash"),
        "key_id": checkpoint.get("key_id"),
        "ts": ts,
        "prev_worm_hash": prev,
    }
    entry["worm_hash"] = _line_hash(prev, json.dumps(entry, sort_keys=True))
    return entry


def _append_local(path: Path, checkpoint: dict[str, Any], ts: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK:
        prev = _last_worm_hash(path)
        entry = _make_entry(prev, checkpoint, ts)
        # O_APPEND -> the OS guarantees the write lands at end-of-file (append-only semantics).
        with os.fdopen(
            os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(entry) + "\n")
    return entry["worm_hash"]


def _s3_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token = None
    while True:
        kw: dict[str, Any] = {"Bucket": bucket, "Prefix": f"{prefix}/" if prefix else ""}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in resp.get("Contents", [])]
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return sorted(keys)


def _s3_get(s3: Any, bucket: str, key: str) -> dict[str, Any]:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise WormAnchorError(f"anchor object {key} is not a JSON object")
    return doc


def _append_s3(dest: str, checkpoint: dict[str, Any], ts: str) -> str:
    bucket, prefix = _parse_s3(dest)
    mode, days = _lock_settings()
    s3 = _s3_client()
    for _attempt in range(4):
        keys = _s3_keys(s3, bucket, prefix)
        prev = _s3_get(s3, bucket, keys[-1])["worm_hash"] if keys else _GENESIS
        seq = (int(keys[-1].rsplit("/", 1)[-1].split(".")[0]) + 1) if keys else 1
        entry = _make_entry(prev, checkpoint, ts)
        key = f"{prefix}/{seq:012d}.json" if prefix else f"{seq:012d}.json"
        try:
            # If-None-Match: * -> the write is refused if the key exists: an anchor entry is never
            # overwritten, and two racing writers cannot both take the same sequence number.
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=json.dumps(entry).encode(),
                ContentType="application/json",
                ObjectLockMode=mode,
                ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(days=days),
                IfNoneMatch="*",
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            if _is_conflict(exc):
                continue  # lost the race for this sequence number - re-read the head and retry
            raise
        return entry["worm_hash"]
    raise WormAnchorError("lost the append race four times - refusing to overwrite an entry")


def anchor_checkpoint_ex(checkpoint: dict[str, Any], *, ts: str) -> dict[str, Any] | None:
    """Anchor a checkpoint and say WHERE it landed.

    Returns ``None`` when no anchor is configured, else
    ``{"worm_hash", "backend": "file"|"s3"|"local-fallback", "degraded": bool[, "error"]}``.
    A failed S3 write is counted, logged and degraded to the local fallback file - it is never
    reported as an S3 anchor. If the fallback also fails the error propagates.
    """
    dest = _worm_path()
    if dest is None:
        return None
    if not _is_s3(dest):
        return {
            "worm_hash": _append_local(Path(dest), checkpoint, ts),
            "backend": "file",
            "degraded": False,
        }
    try:
        return {
            "worm_hash": _append_s3(dest, checkpoint, ts),
            "backend": "s3",
            "degraded": False,
        }
    except Exception as exc:  # noqa: BLE001 - counted + degraded, not swallowed
        _count_failure("s3")
        logger.error(
            "audit WORM anchor to %s FAILED (%s: %s) - degrading to the local fallback file; "
            "the checkpoint is NOT durably anchored off-platform",
            dest,
            type(exc).__name__,
            exc,
        )
        fb = _fallback_path()
        return {
            "worm_hash": _append_local(fb, checkpoint, ts),
            "backend": "local-fallback",
            "degraded": True,
            "error": f"{type(exc).__name__}: {exc}",
        }


def anchor_checkpoint(checkpoint: dict[str, Any], *, ts: str) -> str | None:
    """Append a signed checkpoint to the WORM anchor (chained). Returns its worm_hash, or None.

    No-op (returns None) when ``EXAMLOPS_AUDIT_WORM_PATH`` is unset. ``ts`` is injected so callers
    control the timestamp (CLI stamps it). Use :func:`anchor_checkpoint_ex` to learn the backend.
    """
    res = anchor_checkpoint_ex(checkpoint, ts=ts)
    return res["worm_hash"] if res else None


def checkpoint_and_anchor(
    *, skip_if_unchanged: bool = False, ts: str | None = None
) -> dict[str, Any]:
    """Sign the chain head and anchor it: the periodic-export hook (no daemon).

    Call it from cron, ``exa audit checkpoint``, or any scheduler. With ``skip_if_unchanged`` a
    head that already has a signed checkpoint that is also anchored is left alone, so a frequent
    cron costs one read. Raises :class:`examlops.supplychain.SigningKeyMissing` when no real
    signing key is configured (a checkpoint signed with a default key is forgeable).

    Returns ``{"status": "empty"|"unchanged"|"checkpointed", ...}`` plus, when an anchor is
    configured, ``worm_hash``/``backend``/``degraded`` and ``anchor_error`` if the write failed.
    """
    from examlops.data.audit import audit_chain_head, list_audit_checkpoints, sign_audit_checkpoint
    from examlops.supplychain import _hmac_sign

    head = audit_chain_head()
    if head is None:
        return {"status": "empty"}
    stamp = ts or datetime.now(UTC).isoformat(timespec="seconds")
    cp: dict[str, Any] | None = None
    last = list_audit_checkpoints(1)
    if last and last[0]["head_id"] == head["id"] and last[0]["head_hash"] == head["hash"]:
        cp = {
            "head_id": last[0]["head_id"],
            "head_hash": last[0]["head_hash"],
            "key_id": last[0]["key_id"],
        }
        if skip_if_unchanged:
            v = verify_worm()
            # A checkpoint that sits only in the local fallback (warning) is not anchored yet,
            # and one that a configured transparency log has no receipt for is not logged yet.
            if v["ok"] and not v.get("warning") and _transparency_done(cp):
                return {"status": "unchanged", **cp}
            kept: dict[str, Any] = {"status": "unchanged", **cp}
            if v["ok"] and not v.get("warning"):
                _log_transparency(cp, kept)  # WORM already has it; only the log is missing
                return kept
    if cp is None:
        cp = sign_audit_checkpoint(_hmac_sign(head["hash"]), key_id="d3-hmac")
        if cp is None:  # the head vanished between the read and the write
            return {"status": "empty"}
    out: dict[str, Any] = {"status": "checkpointed", **cp}
    _log_transparency(cp, out)
    try:
        res = anchor_checkpoint_ex(cp, ts=stamp)
    except Exception as exc:  # noqa: BLE001 - counted, reported, not raised: the checkpoint stands
        _count_failure("anchor")
        logger.error("audit checkpoint anchor FAILED (%s: %s)", type(exc).__name__, exc)
        out["anchored"] = False
        out["anchor_error"] = f"{type(exc).__name__}: {exc}"
        return out
    if res is None:
        out["anchored"] = False
        return out
    out.update(res)
    out["anchored"] = not res["degraded"]
    if res.get("error"):
        out["anchor_error"] = res["error"]
    return out


def _transparency_done(cp: dict[str, Any]) -> bool:
    """True when no transparency log is configured, or it already holds this checkpoint."""
    from examlops import audit_transparency as at

    try:
        name = at.backend()
    except at.TransparencyError:
        return False
    return name == "off" or at.get_receipt(str(cp["head_hash"]), name) is not None


def _log_transparency(cp: dict[str, Any], out: dict[str, Any]) -> None:
    """Log ``cp`` to the transparency log, recording the outcome in ``out``. Never raises."""
    from examlops import audit_transparency as at

    if not at.enabled():
        return
    try:
        res = at.anchor_checkpoint(cp)
    except Exception as exc:  # noqa: BLE001 - counted in audit_transparency, reported here
        logger.error(
            "audit checkpoint transparency-log anchor FAILED (%s: %s)", type(exc).__name__, exc
        )
        out["transparency_logged"] = False
        out["transparency_error"] = f"{type(exc).__name__}: {exc}"
        return
    if res is not None:
        out["transparency"] = res
        out["transparency_logged"] = True


def _check_chain(entries: list[dict[str, Any]]) -> str | None:
    """Error text for the first broken link of an already-loaded chain, else ``None``."""
    prev = _GENESIS
    for i, e in enumerate(entries, start=1):
        recomputed = _line_hash(
            prev,
            json.dumps(
                {k: e.get(k) for k in ("head_id", "head_hash", "key_id", "ts", "prev_worm_hash")},
                sort_keys=True,
            ),
        )
        if e.get("prev_worm_hash") != prev or e.get("worm_hash") != recomputed:
            return f"WORM chain broken at entry {i}"
        prev = e["worm_hash"]
    return None


def _cross_check(entries: list[dict[str, Any]], *, extra: list[dict[str, Any]] | None = None):
    """Every DB checkpoint head_hash must be anchored (in ``entries`` or the local fallback)."""
    try:
        from examlops.data.audit import list_audit_checkpoints

        db_hashes = {c["head_hash"] for c in list_audit_checkpoints(10_000)}
        anchored = {e.get("head_hash") for e in entries}
        anchored |= {e.get("head_hash") for e in (extra or [])}
        missing = db_hashes - anchored
        if missing:
            return {
                "ok": False,
                "reason": f"{len(missing)} DB checkpoint(s) not anchored in WORM",
                "entries": len(entries),
                "missing": sorted(missing)[:5],
            }
    except Exception as exc:  # noqa: BLE001 - DB unavailable -> verify the anchor chain alone
        return {
            "ok": True,
            "reason": f"anchor chain valid (DB cross-check skipped: {exc})",
            "entries": len(entries),
        }
    return None


def _verify_s3(dest: str) -> dict[str, Any]:
    try:
        bucket, prefix = _parse_s3(dest)
        s3 = _s3_client()
        entries = [_s3_get(s3, bucket, k) for k in _s3_keys(s3, bucket, prefix)]
    except Exception as exc:  # noqa: BLE001 - an unreadable anchor is a failed verification
        return {
            "ok": False,
            "reason": f"S3 anchor unreadable: {type(exc).__name__}: {exc}",
            "entries": 0,
        }
    err = _check_chain(entries)
    if err:
        return {"ok": False, "reason": err, "entries": len(entries)}
    extra: list[dict[str, Any]] = []
    fb = _fallback_path()
    if fb.exists():
        for line in fb.read_text(encoding="utf-8").splitlines():
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(doc, dict):
                extra.append(doc)
    bad = _cross_check(entries, extra=extra)
    if bad:
        return bad
    out = {"ok": True, "reason": "verified", "entries": len(entries), "backend": "s3"}
    if extra:
        out["warning"] = (
            f"{len(extra)} checkpoint(s) sit only in the local fallback file "
            f"{fb} (an S3 write failed earlier) - re-anchor them"
        )
    return out


def verify_worm() -> dict[str, Any]:
    """Verify the WORM anchor: its internal chain + agreement with the DB checkpoints.

    Returns ``{"ok": bool, "reason": str, "entries": int, ...}``. Never raises for a bad anchor.
    """
    dest = _worm_path()
    if dest is None:
        return {"ok": True, "reason": "no WORM anchor configured", "entries": 0}
    if _is_s3(dest):
        return _verify_s3(dest)
    path = Path(dest)
    if not path.exists():
        return {"ok": True, "reason": "anchor file not yet written", "entries": 0}

    entries: list[dict[str, Any]] = []
    prev = _GENESIS
    with path.open("r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "reason": f"corrupt JSON at line {i}", "entries": len(entries)}
            if not isinstance(e, dict):
                # Valid JSON but not an entry (a foreign/injected line) - a broken chain, not
                # a crash: this function documents that it never raises for a bad anchor.
                return {
                    "ok": False,
                    "reason": f"non-object entry at line {i}",
                    "entries": len(entries),
                }
            # .get(): a truncated entry missing a field must read as a broken chain (the
            # recomputed hash cannot match), never as a KeyError (C10).
            recomputed = _line_hash(
                prev,
                json.dumps(
                    {
                        k: e.get(k)
                        for k in ("head_id", "head_hash", "key_id", "ts", "prev_worm_hash")
                    },
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

    bad = _cross_check(entries)
    if bad:
        return bad
    return {"ok": True, "reason": "verified", "entries": len(entries)}
