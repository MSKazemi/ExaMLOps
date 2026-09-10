"""Site feature profile enforcement for the dashboard API (ADR 0128).

A module the site profile switches off is not merely hidden in the UI: every API route it owns
answers ``404`` with ``code: module_disabled`` (a feature that is not installed here is *not
found*, not *forbidden* — authorization stays the F15 capability layer's job). The route → module
map lives in :mod:`examlops.lifecycle.modules`, next to the CLI commands and Compose services the
same module owns, so the three surfaces cannot disagree.

The profile is re-resolved at most every :data:`_TTL` seconds, so ``exa modules disable X`` reaches
a running dashboard within seconds without re-reading a TOML file on every request.
"""

from __future__ import annotations

import json
import time
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

_TTL = 5.0
_cache: dict[str, Any] = {"at": 0.0, "profile": None}


def current_profile() -> Any | None:
    """The resolved site profile (cached for a few seconds), or ``None`` if unavailable."""
    now = time.monotonic()
    if _cache["profile"] is not None and now - _cache["at"] < _TTL:
        return _cache["profile"]
    try:
        from examlops.lifecycle.modules import resolve

        profile = resolve()
    except Exception:  # noqa: BLE001 — no examlops / broken profile ⇒ fail open, as before
        profile = None
    _cache.update(at=now, profile=profile)
    return profile


def reset_cache() -> None:
    _cache.update(at=0.0, profile=None)


def module_enabled(module_id: str | None) -> bool:
    if module_id is None:
        return True
    profile = current_profile()
    return True if profile is None else bool(profile.is_enabled(module_id))


def disabled_module_for_path(path: str) -> str | None:
    """The disabled module that owns ``path``, or ``None`` when the route is allowed."""
    if not path.startswith("/api/"):
        return None
    try:
        from examlops.lifecycle.modules import module_for_api_path
    except Exception:  # noqa: BLE001
        return None
    owner = module_for_api_path(path)
    return None if module_enabled(owner) else owner


class ModuleGateMiddleware:
    """Pure-ASGI middleware: 404 ``module_disabled`` for API routes of switched-off modules."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        owner = disabled_module_for_path(scope.get("path", ""))
        if owner is None:
            await self.app(scope, receive, send)
            return
        body = json.dumps(
            {
                "detail": f"The '{owner}' module is disabled at this site.",
                "code": "module_disabled",
                "module": owner,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
