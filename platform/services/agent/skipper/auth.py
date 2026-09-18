"""Authentication identities and server-owned conversation namespaces."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass

from skipper import config

logger = logging.getLogger(__name__)

_COOKIE_VERSION = "v1"
_EPHEMERAL_SECRET = secrets.token_bytes(32)


@dataclass(frozen=True)
class AgentIdentity:
    """A principal and tenant derived only from verified server configuration."""

    principal: str
    tenant: str


# `_credentials()` runs on every authenticated request; log each distinct problem once so a broken
# map is visible in the log without flooding it.
_REPORTED_PROBLEMS: set[str] = set()


def reset_reported_credential_problems() -> None:
    """Forget what has already been logged, so the next read reports afresh (tests, reload)."""
    _REPORTED_PROBLEMS.clear()


def _parse_credentials() -> tuple[dict[str, str], list[str]]:
    """The credential map, plus every entry that was refused and why.

    Refusing a malformed entry is right — `auth_required` deliberately stays on for a broken map,
    so nothing is opened up. What was wrong was doing it **silently**: an operator who provisions
    five principals and gets three has no signal anywhere that the other two were rejected, and
    their 401s are indistinguishable from a wrong token. The reasons name the principal only;
    credential material never appears in a message.
    """
    credentials: dict[str, str] = {}
    problems: list[str] = []
    if config.AGENT_API_KEY:
        credentials["primary"] = config.AGENT_API_KEY
    if config.AGENT_API_KEYS_JSON:
        try:
            configured = json.loads(config.AGENT_API_KEYS_JSON)
        except (TypeError, ValueError) as exc:
            configured = {}
            problems.append(
                f"AGENT_API_KEYS_JSON could not be parsed as JSON ({type(exc).__name__})"
            )
        if isinstance(configured, dict):
            for principal, token in configured.items():
                if not principal:
                    problems.append("an entry with an empty principal name was ignored")
                elif not isinstance(token, str):
                    problems.append(
                        f"principal {str(principal)!r} was ignored: its value is "
                        f"{type(token).__name__}, not a string"
                    )
                elif not token:
                    problems.append(f"principal {str(principal)!r} was ignored: its value is empty")
                else:
                    credentials[str(principal)] = token
        elif not problems:
            problems.append(
                f"AGENT_API_KEYS_JSON is a {type(configured).__name__}, not an object of "
                "principal → token; every entry was ignored"
            )
    return credentials, problems


def credential_config_problems() -> list[str]:
    """Credential-map entries the server refused. Empty when the map applied cleanly."""
    return _parse_credentials()[1]


def _credentials() -> dict[str, str]:
    credentials, problems = _parse_credentials()
    for problem in problems:
        if problem not in _REPORTED_PROBLEMS:
            _REPORTED_PROBLEMS.add(problem)
            logger.warning("Agent credential configuration rejected: %s", problem)
    return credentials


_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def credentials_configured() -> bool:
    return bool(config.AGENT_API_KEY or config.AGENT_API_KEYS_JSON)


def unauthenticated_permitted() -> bool:
    """Whether the anonymous ``local`` identity may be used at all.

    Only for an agent bound to loopback, or with an explicit development opt-out. The compose
    service binds 0.0.0.0 inside the stack network, where every container — JupyterHub notebooks
    included — can reach it, and the agent holds a write-capable control-plane credential. An unset
    ``AGENT_API_KEY`` there used to mean "everyone is `local`" (plan P0.7 / finding S5).
    """
    host = os.getenv("AGENT_SERVER_HOST", "127.0.0.1").strip().lower()
    opted_out = os.getenv("AGENT_ALLOW_UNAUTHENTICATED", "").strip().lower() in _TRUTHY
    return host in _LOOPBACK or opted_out


def auth_required() -> bool:
    # A malformed explicit credential map must fail closed, not turn authentication off; so must a
    # network-exposed agent with no credential at all.
    return credentials_configured() or not unauthenticated_permitted()


def auth_misconfigured() -> bool:
    """Exposed beyond loopback with no credential: every request is refused until one is set."""
    return not credentials_configured() and not unauthenticated_permitted()


MISCONFIGURED_DETAIL = (
    "Agent authentication is not configured: set AGENT_API_KEY or AGENT_API_KEYS_JSON "
    "(or bind AGENT_SERVER_HOST=127.0.0.1)"
)


def local_identity() -> AgentIdentity:
    return AgentIdentity(principal="local", tenant=config.AGENT_TENANT)


def authenticate_key(candidate: str | None) -> AgentIdentity | None:
    """Resolve a raw configured credential to its trusted principal."""
    credentials = _credentials()
    if not auth_required():
        return local_identity()
    if not candidate:
        return None
    matches: list[str] = []
    for principal, expected in credentials.items():
        if hmac.compare_digest(str(candidate), expected):
            matches.append(principal)
    return AgentIdentity(matches[0], config.AGENT_TENANT) if len(matches) == 1 else None


def authenticate_bearer(authorization: str | None) -> AgentIdentity | None:
    if not auth_required():
        return local_identity()
    scheme, _, credential = (authorization or "").partition(" ")
    return authenticate_key(credential if scheme.lower() == "bearer" else None)


def server_secret(purpose: bytes) -> bytes:
    """Derive a stable server secret without exposing configured credential material."""
    configured = config.AGENT_ACTION_SIGNING_KEY or config.CONTROL_PLANE_TOKEN
    if not configured:
        configured = "\0".join(f"{key}:{value}" for key, value in sorted(_credentials().items()))
    material = configured.encode() if configured else _EPHEMERAL_SECRET
    return hmac.new(material, purpose, hashlib.sha256).digest()


def issue_cookie(identity: AgentIdentity, *, now: float | None = None) -> str:
    credential = _credentials().get(identity.principal, "")
    issued_at = time.time() if now is None else now
    expires = int(issued_at) + max(1, config.AGENT_BROWSER_SESSION_TTL_SECONDS)
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "principal": identity.principal,
                    "tenant": identity.tenant,
                    "credential": hashlib.sha256(credential.encode()).hexdigest(),
                    "expires": expires,
                },
                separators=(",", ":"),
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    signed = f"{_COOKIE_VERSION}.{payload}"
    signature = hmac.new(
        server_secret(b"examlops-agent-cookie-v1"), signed.encode(), hashlib.sha256
    ).hexdigest()
    return f"{signed}.{signature}"


def authenticate_cookie(cookie: str | None) -> AgentIdentity | None:
    if not cookie:
        return None
    try:
        version, payload, supplied = cookie.split(".", 2)
        signed = f"{version}.{payload}"
        expected = hmac.new(
            server_secret(b"examlops-agent-cookie-v1"), signed.encode(), hashlib.sha256
        ).hexdigest()
        if version != _COOKIE_VERSION or not hmac.compare_digest(supplied, expected):
            return None
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    principal = claims.get("principal") if isinstance(claims, dict) else None
    tenant = claims.get("tenant") if isinstance(claims, dict) else None
    credential = claims.get("credential") if isinstance(claims, dict) else None
    expires = claims.get("expires") if isinstance(claims, dict) else None
    if not isinstance(principal, str) or not isinstance(tenant, str):
        return None
    expected_credential = _credentials().get(principal)
    if (
        tenant != config.AGENT_TENANT
        or expected_credential is None
        or not isinstance(credential, str)
        or not isinstance(expires, int)
        or int(time.time()) >= expires
        or not hmac.compare_digest(
            credential, hashlib.sha256(expected_credential.encode()).hexdigest()
        )
    ):
        return None
    return AgentIdentity(principal=principal, tenant=tenant)


def authenticate(
    authorization: str | None = None, cookie: str | None = None
) -> AgentIdentity | None:
    identity = authenticate_bearer(authorization)
    if identity is not None and (authorization or not auth_required()):
        return identity
    return authenticate_cookie(cookie)


def owner_prefix(identity: AgentIdentity, *, read_only: bool = False) -> str:
    """Return a non-secret namespace derived from a verified identity."""
    owner = hashlib.sha256(f"{identity.tenant}\0{identity.principal}".encode()).hexdigest()[:24]
    mode = "ro" if read_only else "rw"
    return f"owner:{owner}:{mode}:"


def scope_thread_id(
    identity: AgentIdentity, client_thread_id: str, *, read_only: bool = False
) -> str:
    return f"{owner_prefix(identity, read_only=read_only)}{client_thread_id}"


def unscoped_thread_id(
    identity: AgentIdentity, stored_thread_id: str, *, read_only: bool = False
) -> str | None:
    prefix = owner_prefix(identity, read_only=read_only)
    return stored_thread_id[len(prefix) :] if stored_thread_id.startswith(prefix) else None
