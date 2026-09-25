"""OAuth 2.1 resource server for the MCP Streamable-HTTP transport (ADR 0082 layer 3).

The MCP authorization model (spec 2025-06-18) makes an HTTP MCP server an OAuth 2.1 *resource
server*. This module implements that role as one pure-ASGI middleware, :class:`McpHttpGuard`,
wrapped around FastMCP's HTTP app by :func:`examlops.mcp.server.serve`:

* **Origin validation** on every request, in every mode — the spec's MUST against DNS rebinding.
  A request carrying an ``Origin`` that is not allowed is refused with 403 before anything runs.
* **Protected Resource Metadata** (RFC 9728) at ``/.well-known/oauth-protected-resource`` (and the
  path-suffixed form for a resource with a path), naming the authorization servers from the ADR
  0120 trust file and the scope catalogue.
* **Bearer verification** through the platform's one verifier (:mod:`examlops.iam.tokens`) —
  signature, issuer, expiry, the account directory — plus **audience binding**: the token's
  ``aud`` must name this MCP resource (RFC 8707/9068), so a token minted for another service is
  never accepted here (no token passthrough).
* **Per-tool scopes**: ``tools/call`` needs the tool's scope (see :func:`required_scopes`);
  ``plan_change``/``apply_plan`` need the scope of the tool they plan or apply. Failures answer
  ``401``/``403`` with an RFC 6750 ``WWW-Authenticate`` challenge carrying ``resource_metadata``.
* Every refusal and every authorized mutating call is written to ``audit_events`` with the
  verified principal as actor, and counted in ``examlops_mcp_http_auth_total{outcome}``.

Off unless ``EXAMLOPS_MCP_AUTH=oauth``. Without it the HTTP transport stays loopback-only
(:class:`~examlops.mcp.server.UnsafeMCPBind`), exactly as before, but with Origin validation.
stdio (local, single user) is unaffected.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

__all__ = [
    "PRM_PATH",
    "SCOPE_ADMIN",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "TOOL_SCOPE_PREFIX",
    "HttpAuthSettings",
    "McpAuthConfigError",
    "McpHttpGuard",
    "load_settings",
    "protected_resource_metadata",
    "required_scopes",
    "scope_catalog",
    "verify_for_resource",
]

SCOPE_READ = "mcp:tools:read"
SCOPE_WRITE = "mcp:tools:write"
SCOPE_ADMIN = "mcp:tools:admin"
TOOL_SCOPE_PREFIX = "mcp:tool:"
COARSE_SCOPES = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)
PRM_PATH = "/.well-known/oauth-protected-resource"
DEFAULT_MAX_BODY = 1_048_576
_MODES = ("none", "oauth")

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
Verifier = Callable[[str, str], Any]
#: ``(principal, action, resource) -> Decision-like`` with ``.allowed`` and ``.reason``.
Authorizer = Callable[[Any, str, dict[str, Any]], Any]


class McpAuthConfigError(ValueError):
    """The HTTP auth configuration is incomplete or unsafe; the server must not start."""


# ── scope catalogue ───────────────────────────────────────────────────────────


def _specs() -> dict[str, Any]:
    from examlops.mcp.tools import iter_tools

    return {s.name: s for s in iter_tools(include_writes=True)}


def _scopes_for_spec(name: str, spec: Any | None) -> tuple[str, ...]:
    """Any-of scopes that allow calling ``name``. Unknown tools need admin (fail closed)."""
    own = f"{TOOL_SCOPE_PREFIX}{name}"
    if spec is None:
        return (SCOPE_ADMIN, own)
    if not spec.mutating:
        return (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN, own)
    if spec.tier == "A":
        return (SCOPE_WRITE, SCOPE_ADMIN, own)
    return (SCOPE_ADMIN, own)  # B (high-impact) and C (human-only)


def required_scopes(name: str) -> tuple[str, ...]:
    """Scopes of which a token must hold at least one to call tool ``name``.

    ``read`` tools: ``mcp:tools:read`` (or write/admin). Tier-A writes: ``mcp:tools:write`` (or
    admin). Tier-B/C writes: ``mcp:tools:admin``. Every tool also accepts its own fine-grained
    ``mcp:tool:<name>`` scope, so an authorization server can grant exactly one capability.
    """
    return _scopes_for_spec(name, _specs().get(name))


def _planned_tool(plan_hash: str) -> str | None:
    """The tool a stored plan would execute, or ``None`` if the plan cannot be read."""
    try:
        from examlops.data import plans as store

        store.init_db()
        row = store.get(plan_hash)
        if row is None:
            return None
        return str(json.loads(row["plan_json"]).get("tool") or "") or None
    except Exception:  # noqa: BLE001 - unreadable plan: fall back to the tool's own scopes
        return None


def required_scopes_for_call(name: str, arguments: dict[str, Any] | None) -> tuple[str, ...]:
    """Scopes for one ``tools/call`` — plan tools inherit the scopes of the tool they act on."""
    args = arguments if isinstance(arguments, dict) else {}
    if name == "plan_change" and isinstance(args.get("tool"), str):
        return required_scopes(args["tool"])
    if name == "apply_plan":
        target = (
            _planned_tool(args["plan_hash"]) if isinstance(args.get("plan_hash"), str) else None
        )
        # A plan that cannot be resolved here (unknown, unreadable store) could still be a tier-B/C
        # plan by the time the tool reads it: fail closed to the strongest scope, never to the
        # apply tool's own tier-A scope.
        return required_scopes(target) if target else (SCOPE_ADMIN, f"{TOOL_SCOPE_PREFIX}{name}")
    return required_scopes(name)


#: JSON-RPC methods that read platform data outside ``tools/call``. A fine-grained
#: ``mcp:tool:<name>`` scope grants exactly that tool, so these need a coarse scope.
DATA_METHODS = frozenset({"resources/read", "resources/subscribe", "prompts/get"})


def authz_action(name: str) -> str:
    """The ADR 0120 platform action a ``tools/call`` of ``name`` is authorized as.

    Scopes say what the *client* was granted; the platform role (and the center's PDP) say what
    the *user* may do — both must allow a call. Reads → ``api.read`` (viewer), tier-A writes →
    ``api.write`` (operator), tier-B/C and unknown tools → ``mcp.tools.admin`` (an unclassified
    action, so :func:`examlops.iam.pdp.min_role_for` requires ``admin``).
    """
    spec = _specs().get(name)
    if spec is not None and not spec.mutating:
        return "api.read"
    if spec is not None and spec.tier == "A":
        return "api.write"
    return "mcp.tools.admin"


def authz_action_for_call(name: str, arguments: dict[str, Any] | None) -> str:
    """Like :func:`authz_action`; plan tools are authorized as the tool they plan or apply."""
    args = arguments if isinstance(arguments, dict) else {}
    if name == "plan_change" and isinstance(args.get("tool"), str):
        return authz_action(args["tool"])
    if name == "apply_plan":
        target = (
            _planned_tool(args["plan_hash"]) if isinstance(args.get("plan_hash"), str) else None
        )
        return authz_action(target) if target else "mcp.tools.admin"
    return authz_action(name)


def scope_catalog(include_writes: bool = True) -> dict[str, Any]:
    """The documented scope catalogue: coarse scopes plus the scopes each tool accepts."""
    from examlops.mcp.tools import iter_tools

    tools = {
        s.name: {
            "tier": s.tier if s.mutating else "read",
            "scopes": list(_scopes_for_spec(s.name, s)),
        }
        for s in iter_tools(include_writes=include_writes)
    }
    return {
        "coarse": {
            SCOPE_READ: "call read-only tools, read resources and prompts",
            SCOPE_WRITE: "also call tier-A (low-risk) mutating tools",
            SCOPE_ADMIN: "also call tier-B/C (high-impact, human-only) mutating tools",
        },
        "per_tool_prefix": TOOL_SCOPE_PREFIX,
        "tools": tools,
    }


# ── settings ──────────────────────────────────────────────────────────────────


def _is_loopback_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


@dataclass(frozen=True)
class HttpAuthSettings:
    """Resolved HTTP transport security settings."""

    mode: str = "none"
    resource: str = ""
    allowed_origins: frozenset[str] = frozenset()
    allow_loopback_origins: bool = True
    max_body: int = DEFAULT_MAX_BODY
    authorization_servers: tuple[str, ...] = ()

    @property
    def oauth(self) -> bool:
        return self.mode == "oauth"

    def prm_paths(self) -> tuple[str, ...]:
        """RFC 9728 §3.1: the well-known path, plus its path-suffixed form for ``resource``."""
        path = urlsplit(self.resource).path.rstrip("/") if self.resource else ""
        return (PRM_PATH, f"{PRM_PATH}{path}") if path else (PRM_PATH,)

    def prm_url(self) -> str:
        if not self.resource:
            return PRM_PATH
        return _origin_of(self.resource) + self.prm_paths()[-1]


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def load_settings(host: str = "127.0.0.1") -> HttpAuthSettings:
    """Read ``EXAMLOPS_MCP_AUTH`` & co.; raise :class:`McpAuthConfigError` if oauth is unsafe."""
    mode = (os.getenv("EXAMLOPS_MCP_AUTH", "") or "none").strip().lower()
    if mode not in _MODES:
        raise McpAuthConfigError(f"EXAMLOPS_MCP_AUTH must be one of {_MODES}, got {mode!r}")
    origins = frozenset(
        o.strip().rstrip("/").lower()
        for o in os.getenv("EXAMLOPS_MCP_ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    )
    base = HttpAuthSettings(
        mode=mode,
        allowed_origins=origins,
        allow_loopback_origins=_is_loopback_host(host),
        max_body=_env_int("EXAMLOPS_MCP_MAX_BODY_BYTES", DEFAULT_MAX_BODY),
    )
    if mode == "none":
        return base
    resource = os.getenv("EXAMLOPS_MCP_RESOURCE", "").strip()
    if not resource:
        raise McpAuthConfigError(
            "EXAMLOPS_MCP_AUTH=oauth needs EXAMLOPS_MCP_RESOURCE — the canonical URI of this MCP "
            "endpoint (e.g. https://mcp.example.org/mcp); tokens must name it in their audience"
        )
    parts = urlsplit(resource)
    if parts.fragment or parts.scheme not in {"https", "http"} or not parts.netloc:
        raise McpAuthConfigError("EXAMLOPS_MCP_RESOURCE must be an absolute URI without fragment")
    if parts.scheme == "http" and not _is_loopback_host(parts.hostname or ""):
        raise McpAuthConfigError("EXAMLOPS_MCP_RESOURCE must use https (http only for loopback)")
    from examlops.iam import IamConfigError, load_config

    try:
        cfg = load_config()
    except IamConfigError as exc:
        raise McpAuthConfigError(f"identity trust file is invalid: {exc}") from exc
    if not cfg.enabled:
        raise McpAuthConfigError(
            "EXAMLOPS_MCP_AUTH=oauth needs a trusted identity provider (EXAMLOPS_IAM_CONFIG, "
            "ADR 0120); none is configured"
        )
    return HttpAuthSettings(
        mode=mode,
        resource=resource,
        allowed_origins=origins | {_origin_of(resource)},
        allow_loopback_origins=base.allow_loopback_origins,
        max_body=base.max_body,
        authorization_servers=tuple(p.issuer for p in cfg.providers),
    )


def protected_resource_metadata(settings: HttpAuthSettings) -> dict[str, Any]:
    """The RFC 9728 Protected Resource Metadata document for this MCP endpoint."""
    return {
        "resource": settings.resource,
        "authorization_servers": list(settings.authorization_servers),
        "scopes_supported": list(COARSE_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "ExaMLOps MCP",
        "resource_documentation": (
            "https://mskazemi.com/ExaMLOps/guides/provider-security-trust-tiers/"
        ),
    }


# ── token verification ────────────────────────────────────────────────────────


def verify_for_resource(token: str, resource: str) -> Any:
    """Verify a bearer for this MCP resource and return the federated ``Principal``.

    Uses the platform verifier (ADR 0120) with the audience overridden to ``resource``: a JWT
    whose ``aud`` does not name this MCP endpoint is refused even when its signature is good.
    Opaque tokens are introspected at their own issuer and must report ``aud`` naming it too.
    """
    from examlops.iam import tokens

    token = token.strip()
    if not token:
        raise tokens.AuthenticationError("empty bearer token")
    if tokens.looks_like_jwt(token):
        provider, claims = tokens.verify_jwt(token, audience=(resource,))
        principal = tokens.principal_from_claims(provider, claims, "jwt")
    else:
        provider, claims = tokens.introspect(token)
        aud = claims.get("aud")
        auds = {aud} if isinstance(aud, str) else set(aud or [])
        if resource not in auds:
            raise tokens.AuthenticationError("token audience does not name this MCP resource")
        principal = tokens.principal_from_claims(provider, claims, "opaque")
    tokens.check_account(provider, principal, "enforce")
    return principal


# ── metrics + audit ───────────────────────────────────────────────────────────

_METRIC: Any = None
_METRIC_LOCK = threading.Lock()


def _count(outcome: str) -> None:
    global _METRIC
    try:
        if _METRIC is None:
            with _METRIC_LOCK:
                if _METRIC is None:
                    from prometheus_client import Counter

                    _METRIC = Counter(
                        "examlops_mcp_http_auth",
                        "MCP HTTP transport authorization decisions by outcome.",
                        ["outcome"],
                    )
        _METRIC.labels(outcome=outcome).inc()
    except Exception:  # noqa: BLE001 - metrics never break a request (prometheus is optional)
        pass


def _audit(action: str, actor: str, target: str, details: dict[str, Any]) -> None:
    try:
        from examlops.data import init_db
        from examlops.data.audit import write_audit_event

        init_db()
        write_audit_event("mcp-http", actor, action, target, {**details, "via": "mcp-http"})
    except Exception as exc:  # noqa: BLE001 - an audit failure must not become a 500
        log.warning("mcp http audit failed: %s", exc)


def default_authorizer(principal: Any, action: str, resource: dict[str, Any]) -> Any:
    """ADR 0120 ``authorize()``: tenant invariant → platform role → the center's PDP.

    ``authorize`` writes its own ``authz_denied`` audit record for a refusal.
    """
    from examlops import iam

    return iam.authorize(principal, action, resource)


class _WindowLimiter:
    """At most ``limit`` events per ``window`` seconds (fixed window, thread-safe)."""

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit, self.window = limit, window
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._n = 0

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._start >= self.window:
                self._start, self._n = now, 0
            self._n += 1
            return self._n <= self.limit


# ── ASGI middleware ───────────────────────────────────────────────────────────


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _messages(body: bytes) -> list[dict[str, Any]] | None:
    """JSON-RPC messages in a POST body (single or batch); ``None`` if it is not a valid shape.

    ``None`` makes the guard refuse the request. Forwarding a body the guard could not read would
    let a parser differential (a body this parser rejects but the MCP server's accepts) carry a
    ``tools/call`` past the scope check on nothing more than the read authorization.
    """
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    items: Iterable[Any] = data if isinstance(data, list) else [data]
    messages = list(items)
    if not messages or not all(isinstance(m, dict) for m in messages):
        return None
    return messages


class McpHttpGuard:
    """ASGI middleware: Origin validation always; OAuth 2.1 resource server when configured."""

    def __init__(
        self,
        app: ASGIApp,
        settings: HttpAuthSettings,
        *,
        verifier: Verifier | None = None,
        authorizer: Authorizer | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.verifier: Verifier = verifier or verify_for_resource
        self.authorizer: Authorizer = authorizer or default_authorizer
        # Refusals of *unauthenticated* requests are audited only up to this rate: anyone who can
        # reach the port can send bad tokens, and each audit row is a locked write to the
        # hash-chained audit log. Every refusal is still counted in the Prometheus counter.
        self._anon_audit = _WindowLimiter(_env_int("EXAMLOPS_MCP_ANON_DENY_AUDIT_PER_MIN", 60))
        # MCP session id -> the principal it belongs to (spec: bind sessions to the user; a session
        # id is never authentication). Bounded LRU so a flood of sessions cannot grow it forever.
        self._sessions: OrderedDict[str, str] = OrderedDict()
        self._sessions_lock = threading.Lock()
        self._max_sessions = _env_int("EXAMLOPS_MCP_MAX_BOUND_SESSIONS", 10_000)

    # -- session binding --

    def _bind_session(self, session_id: str, owner: str) -> str:
        """Bind ``session_id`` to ``owner`` unless already bound; return the bound owner."""
        with self._sessions_lock:
            bound = self._sessions.get(session_id)
            if bound is None:
                self._sessions[session_id] = bound = owner
                while len(self._sessions) > self._max_sessions:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.move_to_end(session_id)
            return bound

    def _binding_send(self, send: Send, owner: str) -> Send:
        """Bind a session id the MCP server issues in its response to the requesting principal."""

        async def wrapped(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                for key, value in message.get("headers") or []:
                    if bytes(key).lower() == b"mcp-session-id":
                        self._bind_session(bytes(value).decode("latin-1"), owner)
            await send(message)

        return wrapped

    # -- responses --

    async def _send_json(
        self, send: Send, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]]
    ) -> None:
        raw = json.dumps(body).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(raw)).encode()),
                    (b"cache-control", b"no-store"),
                    *headers,
                ],
            }
        )
        await send({"type": "http.response.body", "body": raw})

    def _challenge(self, error: str | None, description: str = "", scope: str = "") -> bytes:
        parts = [f'resource_metadata="{self.settings.prm_url()}"']
        if error:
            parts.append(f'error="{error}"')
        if description:
            parts.append('error_description="{}"'.format(description.replace('"', "'")[:200]))
        if scope:
            parts.append(f'scope="{scope}"')
        return ("Bearer " + ", ".join(parts)).encode("latin-1", "replace")

    async def _deny(
        self,
        send: Send,
        status: int,
        error: str,
        description: str,
        *,
        scope: str = "",
        challenge_error: str | None = None,
        actor: str = "anonymous",
        target: str = "",
        audit: bool = True,
    ) -> None:
        _count(error)
        if audit and actor == "anonymous" and not self._anon_audit.allow():
            _count("audit_suppressed")
            audit = False
        if audit:
            await asyncio.to_thread(
                _audit,
                "mcp_http_denied",
                actor,
                target or error,
                {"status": status, "error": error, "reason": description},
            )
        headers = []
        if status in (401, 403) and error != "invalid_origin":
            headers.append(
                (b"www-authenticate", self._challenge(challenge_error, description, scope))
            )
        await self._send_json(
            send, status, {"error": error, "error_description": description}, headers
        )

    # -- checks --

    def _origin_allowed(self, origin: str) -> bool:
        origin = origin.strip().rstrip("/").lower()
        if origin in self.settings.allowed_origins:
            return True
        if self.settings.allow_loopback_origins:
            parts = urlsplit(origin)
            return parts.scheme in {"http", "https"} and _is_loopback_host(parts.hostname or "")
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind != "http":
            if kind == "websocket":
                # Origin validation applies to every transport, not just HTTP requests.
                ws_origin = _header(scope, b"origin")
                if self.settings.oauth or (
                    ws_origin is not None and not self._origin_allowed(ws_origin)
                ):
                    _count("websocket_refused" if self.settings.oauth else "invalid_origin")
                    await send({"type": "websocket.close", "code": 1008})
                    return
            elif self.settings.oauth:
                return
            await self.app(scope, receive, send)
            return

        origin = _header(scope, b"origin")
        if origin is not None and not self._origin_allowed(origin):
            # Spec MUST (DNS rebinding): refuse before anything else runs. Not audited per request
            # (a hostile page can make a browser send many) — counted instead.
            await self._deny(
                send, 403, "invalid_origin", f"origin {origin!r} is not allowed", audit=False
            )
            return
        if not self.settings.oauth:
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"
        if path in self.settings.prm_paths():
            if scope.get("method") not in {"GET", "HEAD"}:
                await self._send_json(send, 405, {"error": "method_not_allowed"}, [])
                return
            _count("metadata")
            await self._send_json(
                send,
                200,
                protected_resource_metadata(self.settings),
                [(b"access-control-allow-origin", b"*")],
            )
            return

        authz = _header(scope, b"authorization")
        if not authz:
            await self._deny(send, 401, "invalid_request", "missing bearer token", audit=False)
            return
        parts = authz.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            await self._deny(
                send,
                401,
                "invalid_request",
                "Authorization must be 'Bearer <token>'",
                challenge_error="invalid_request",
            )
            return
        try:
            principal = await asyncio.to_thread(self.verifier, parts[1], self.settings.resource)
        except Exception as exc:  # noqa: BLE001 - any verifier failure is a 401, never a 500
            reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
            await self._deny(send, 401, "invalid_token", reason, challenge_error="invalid_token")
            return
        actor = str(getattr(principal, "actor", "") or "unknown")
        held = set(getattr(principal, "scopes", ()) or ())
        if not held & set(COARSE_SCOPES) and not any(s.startswith(TOOL_SCOPE_PREFIX) for s in held):
            await self._deny(
                send,
                403,
                "insufficient_scope",
                "token carries no MCP scope",
                scope=SCOPE_READ,
                challenge_error="insufficient_scope",
                actor=actor,
            )
            return

        owner = f"{getattr(principal, 'id', actor)}|{getattr(principal, 'tenant', '')}"
        session_id = _header(scope, b"mcp-session-id")
        if session_id is not None and self._bind_session(session_id, owner) != owner:
            await self._deny(
                send,
                403,
                "access_denied",
                "this MCP session belongs to another principal",
                actor=actor,
                target="mcp-session",
            )
            return
        send = self._binding_send(send, owner)

        if scope.get("method") != "POST":
            if not await self._authorize(send, principal, actor, "api.read", "mcp"):
                return
            _count("allowed")
            await self.app(scope, receive, send)
            return

        body, too_large = await self._read_body(receive)
        if too_large:
            await self._deny(
                send,
                413,
                "request_too_large",
                f"request body exceeds {self.settings.max_body} bytes",
                actor=actor,
            )
            return
        mutating: list[str] = []
        # (action, target) pairs to authorize against the platform role + center PDP.
        checks: dict[tuple[str, str], None] = {("api.read", "mcp"): None}
        messages = _messages(body)
        if messages is None:
            await self._deny(
                send,
                400,
                "invalid_request",
                "the request body is not a JSON-RPC message or batch",
                actor=actor,
            )
            return
        for msg in messages:
            method = msg.get("method")
            if method != "tools/call":
                if method in DATA_METHODS and not held & set(COARSE_SCOPES):
                    await self._deny(
                        send,
                        403,
                        "insufficient_scope",
                        f"{method} needs one of: {' '.join(COARSE_SCOPES)}",
                        scope=SCOPE_READ,
                        challenge_error="insufficient_scope",
                        actor=actor,
                        target=str(method),
                    )
                    return
                continue
            raw_params = msg.get("params")
            params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            needed = await asyncio.to_thread(required_scopes_for_call, name, arguments)
            if not held & set(needed):
                await self._deny(
                    send,
                    403,
                    "insufficient_scope",
                    f"tool {name!r} needs one of: {' '.join(needed)}",
                    scope=" ".join(needed),
                    challenge_error="insufficient_scope",
                    actor=actor,
                    target=name,
                )
                return
            action = await asyncio.to_thread(authz_action_for_call, name, arguments)
            checks[(action, name)] = None
            if SCOPE_READ not in needed:
                mutating.append(name)
        for action, target in checks:
            if not await self._authorize(send, principal, actor, action, target):
                return
        for name in mutating:
            await asyncio.to_thread(
                _audit,
                "mcp_http_tool_authorized",
                actor,
                name,
                {
                    "principal": getattr(principal, "id", actor),
                    "tenant": getattr(principal, "tenant", ""),
                },
            )
        _count("allowed")
        await self.app(scope, self._replay(body, receive), send)

    async def _authorize(
        self, send: Send, principal: Any, actor: str, action: str, target: str
    ) -> bool:
        """Platform role + center PDP (ADR 0120) on top of the token's scopes; fail closed."""
        resource = {"type": "mcp_tool", "id": target}
        try:
            decision = await asyncio.to_thread(self.authorizer, principal, action, resource)
            allowed = getattr(decision, "allowed", False) is True
            reason = str(getattr(decision, "reason", "") or "denied")
        except Exception as exc:  # noqa: BLE001 - an authorization error is a refusal
            allowed, reason = False, f"authorization unavailable: {exc}"
        if allowed:
            return True
        # iam.authorize already audits its refusal as `authz_denied`; one refusal, one row.
        await self._deny(
            send,
            403,
            "access_denied",
            f"{action} on {target!r} refused: {reason}",
            actor=actor,
            target=target,
            audit=False,
        )
        return False

    async def _read_body(self, receive: Receive) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                break
            chunk = message.get("body", b"") or b""
            size += len(chunk)
            if size > self.settings.max_body:
                return b"", True
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        return b"".join(chunks), False

    @staticmethod
    def _replay(body: bytes, upstream: Receive) -> Receive:
        """Hand the buffered body to the app once, then the real channel (so disconnects arrive)."""
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await upstream()

        return receive
