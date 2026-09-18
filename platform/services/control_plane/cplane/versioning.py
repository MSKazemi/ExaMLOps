"""The versioned API: ``/v1`` for every operator route, deprecation headers on the legacy paths.

Plan P1.6. ``/v1`` started with the asynchronous commands; every other route lived at an
unversioned path, so there was no way to change one without breaking whoever called it. This
module gives each legacy route a ``/v1`` path that is **the same handler** — registered from the
same endpoint, dependencies and response model, so the two cannot drift — with the one intended
difference that errors under ``/v1`` are RFC 9457 problem documents (``problems.py``).

The legacy paths keep working unchanged and announce their successor on every response:

* ``Deprecation: @1789084800`` (RFC 9745 — deprecated since 2026-09-11), and
* ``Link: </v1/...>; rel="successor-version"`` with the caller's own path parameters filled in.

No ``Sunset`` header: no removal date has been decided, and a date nobody chose would be a false
statement to every client that plans around it.

Routes that are infrastructure rather than API — probes (``/health``, ``/readyz``, ``/livez``,
``/ready``), ``/metrics`` and the inbound provider webhooks — stay unversioned on purpose: their
callers (orchestrators, Prometheus, GitHub/GitLab) are configured once and do not negotiate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEPRECATED_SINCE = 1789084800  # 2026-09-11T00:00:00Z


@dataclass(frozen=True)
class Alias:
    method: str
    legacy: str
    v1: str
    same_handler: bool = True  # False: the successor exists but behaves differently (no alias)


ALIASES: tuple[Alias, ...] = (
    Alias("GET", "/status", "/v1/status"),
    Alias("GET", "/models", "/v1/models"),
    Alias("GET", "/models/{name}/meta", "/v1/models/{name}/meta"),
    Alias("GET", "/models/{name}/readme", "/v1/models/{name}/readme"),
    Alias("GET", "/models/{name}/images/{filename}", "/v1/models/{name}/images/{filename}"),
    Alias("GET", "/retrain/{flow_run_id}", "/v1/runs/{flow_run_id}"),
    Alias("GET", "/approvals", "/v1/approvals"),
    Alias("POST", "/approve/{model_id}", "/v1/approvals/{model_id}/approve"),
    Alias("POST", "/reject/{model_id}", "/v1/approvals/{model_id}/reject"),
    Alias("DELETE", "/approvals/{approval_id}", "/v1/approvals/{approval_id}"),
    Alias("POST", "/api/changes", "/v1/changes"),
    Alias("GET", "/modelzoo/status", "/v1/modelzoo/status"),
    Alias("GET", "/modelzoo/events", "/v1/modelzoo/events"),
    Alias("POST", "/modelzoo/sync", "/v1/modelzoo/sync"),
    Alias("GET", "/modelzoo/config", "/v1/modelzoo/config"),
    Alias("PUT", "/modelzoo/config", "/v1/modelzoo/config"),
    Alias("POST", "/admin/reload", "/v1/admin/reload"),
    # The synchronous retrain's successor is the asynchronous command: same intent, a 202 and a
    # command to follow instead of a blocking call — so it is announced, not aliased.
    Alias("POST", "/retrain", "/v1/retrain", same_handler=False),
)

_PARAM = re.compile(r"\{([^}]+)\}")


def _matcher(template: str) -> re.Pattern[str]:
    """``/approve/{model_id}`` → ``^/approve/(?P<model_id>[^/]+)$``."""
    parts: list[str] = []
    pos = 0
    for m in _PARAM.finditer(template):
        parts.append(re.escape(template[pos : m.start()]))
        parts.append(f"(?P<{m.group(1)}>[^/]+)")
        pos = m.end()
    parts.append(re.escape(template[pos:]))
    return re.compile("^" + "".join(parts) + "$")


def install_v1_aliases(app: FastAPI) -> list[str]:
    """Register a ``/v1`` twin of every aliased legacy route. Returns the paths added.

    Raises when an alias names a route the app does not have: a silently skipped alias is a
    ``/v1`` path documented and missing.
    """
    by_key = {
        (method, route.path): route
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    added: list[str] = []
    for alias in ALIASES:
        if not alias.same_handler:
            continue
        route = by_key.get((alias.method, alias.legacy))
        if route is None:
            raise RuntimeError(
                f"/v1 alias for a route that does not exist: {alias.method} {alias.legacy}"
            )
        app.add_api_route(
            alias.v1,
            route.endpoint,
            methods=[alias.method],
            response_model=route.response_model,
            status_code=route.status_code,
            dependencies=list(route.dependencies),
            summary=route.summary,
            description=route.description,
            include_in_schema=route.include_in_schema,
            name=f"v1_{route.name}",
            tags=["v1"],
        )
        added.append(f"{alias.method} {alias.v1}")
    return added


class DeprecationHeaders:
    """ASGI middleware: legacy routes announce their ``/v1`` successor (RFC 9745 + RFC 8288)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._rules = [(a.method, _matcher(a.legacy), a.v1) for a in ALIASES]

    def _successor(self, method: str, path: str) -> str | None:
        for rule_method, pattern, v1 in self._rules:
            if rule_method == method:
                match = pattern.match(path)
                if match:
                    return _PARAM.sub(lambda m: match.group(m.group(1)), v1)
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        successor = self._successor(scope["method"], scope["path"])
        if successor is None:
            await self.app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[Any] = list(message.get("headers", []))
                headers.append((b"deprecation", f"@{DEPRECATED_SINCE}".encode()))
                headers.append((b"link", f'<{successor}>; rel="successor-version"'.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)
