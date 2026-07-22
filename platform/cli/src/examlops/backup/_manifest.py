"""Shared backup primitives: checksums, the per-tier result contract, and the degradation wrapper.

Every tier module (sqlite / postgres / objects / config) returns a :class:`TierResult`. The
:func:`run_tier` wrapper enforces the platform's degrade-to-``skipped`` convention (R5/R11): a
tier that cannot run because a tool is missing, an endpoint is unreachable, or an optional source
is absent raises :class:`TierUnavailable` and is recorded as ``skipped`` — the bundle still
succeeds. An *unexpected* error is recorded as ``failed``. Under ``strict`` both re-raise, so CI /
a DR drill can demand a fully-complete bundle.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Tier / item status vocabulary — kept as plain strings so manifests are trivially JSON-serialisable.
OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"
PARTIAL = "partial"


class TierUnavailable(Exception):
    """A tier cannot run for an *expected* reason (missing tool, unreachable endpoint, absent source).

    Raising this (rather than a bare ``Exception``) is how a tier says "degrade me to skipped, don't
    fail the bundle". :func:`run_tier` catches it and records ``status=skipped`` with the message.
    """


@dataclass
class TierResult:
    """The common currency every tier module returns; aggregated by :mod:`examlops.backup.bundle`."""

    name: str
    status: str = OK
    reason: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    sha256: str | None = None
    size_bytes: int = 0

    def to_manifest(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "items": self.items}
        if self.reason:
            out["reason"] = self.reason
        if self.sha256:
            out["sha256"] = self.sha256
        if self.size_bytes:
            out["size_bytes"] = self.size_bytes
        return out


def rollup_status(statuses: list[str]) -> str:
    """Roll a set of item/tier statuses up into one: failed > partial(skip+ok) > skipped > ok."""
    if not statuses:
        return SKIPPED
    if any(s == FAILED for s in statuses):
        return FAILED
    has_ok = any(s == OK for s in statuses)
    has_skip = any(s == SKIPPED for s in statuses)
    if has_skip and has_ok:
        return PARTIAL
    if has_skip:
        return SKIPPED
    return OK


def run_tier(
    name: str,
    fn: Callable[[], TierResult],
    *,
    requested: bool,
    strict: bool,
) -> TierResult:
    """Execute a tier honouring the degrade-to-skipped convention.

    * not requested → ``skipped`` (reason "not requested"), never runs ``fn``.
    * :class:`TierUnavailable` → ``skipped`` with the message (re-raised under ``strict``).
    * any other exception → ``failed`` with ``repr`` (re-raised under ``strict``).
    """
    if not requested:
        return TierResult(name, status=SKIPPED, reason="not requested")
    try:
        return fn()
    except TierUnavailable as exc:
        if strict:
            raise
        return TierResult(name, status=SKIPPED, reason=str(exc))
    except Exception as exc:  # noqa: BLE001 — deliberate: a broken tier must not sink the bundle
        if strict:
            raise
        return TierResult(name, status=FAILED, reason=repr(exc))


def sha256_file(path: Path) -> str:
    """Streaming sha256 of a file (1 MiB chunks)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
