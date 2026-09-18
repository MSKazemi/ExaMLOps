"""Authentication for the dataplane service (ADR 0130 §9) — the control plane's fail-closed rules.

Three ways in, checked in this order:

* the static bearer ``DATAPLANE_TOKEN`` (a real, non-placeholder secret) — scopes ``read``,
  ``write`` and ``ingest``;
* the optional static bearer ``DATAPLANE_INGEST_TOKEN`` (ADR 0131 d6) — scope ``ingest`` only, so
  a producer that pushes stream messages holds a credential that can do nothing else;
* a token from a trusted data-center IdP (ADR 0120, ``examlops.iam``) — ``read`` and ``ingest``
  for any mapped role, ``write`` from ``operator`` up — after which the center's PDP may still
  veto the call.

``ingest`` (``POST /streams/{name}/messages``) is a write-like side effect — it runs a model — so
it is never open: with nothing configured it answers 503. A federated caller's ``ingest`` scope
is only a ticket to the project check (:func:`may_ingest`): it needs ``operator`` (the ``write``
rule) or ``editor`` on the stream's project.

A federated caller is further scoped to projects (spec §10, :func:`may_access_project`): with
``EXAMLOPS_MULTITENANCY`` on, a project's sources need ``viewer`` (read) / ``editor`` (write) on
``project:<p>``, and a global source is written only from ``operator`` up. Source-scoped routes
authenticate with :func:`authenticate_read`/:func:`authenticate_write` and then call
:func:`authorize_source` once the project is known (it may sit in the request body).

With nothing configured (``DATAPLANE_TOKEN`` and ``DATAPLANE_INGEST_TOKEN`` unset or blank, no
trust file) the service is a loopback-only deployment: reads are open and every write and ingest
is refused (503). A configured ingest token alone is *not* "nothing configured": reads then need a
credential too, and with no ``DATAPLANE_TOKEN`` or federation there is none (503). A token that is
set but is a placeholder or too short is *not* "unset": with no working federation it closes every
route (503). A trust file that fails to parse refuses federated tokens and nothing else, so the static
token keeps working while the file is being fixed. Nothing here logs or returns a token.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, HTTPException, Request, status

logger = logging.getLogger(__name__)

TOKEN_ENV = "DATAPLANE_TOKEN"
STATIC_ACTOR = "dataplane-api"
#: The optional ingest-only static bearer (ADR 0131 d6) and the actor it acts as.
INGEST_TOKEN_ENV = "DATAPLANE_INGEST_TOKEN"
INGEST_ACTOR = "dataplane-ingest"
INGEST_DISABLED_DETAIL = f"dataplane ingest is disabled: set {TOKEN_ENV} or {INGEST_TOKEN_ENV}"

# same rules as examlops.credentials (switch to it once that module is committed)
MIN_SECRET_LENGTH = 16

# Exact values that ship in examples, tutorials and defaults.
_PLACEHOLDERS = frozenset(
    {
        "changeme",
        "change-me",
        "change_me",
        "changethis",
        "changeme123",
        "placeholder",
        "example",
        "secret",
        "password",
        "token",
        "todo",
        "none",
        "null",
        "your-token-here",
        "yourtoken",
        "examlops-dev-key",
    }
)
# The same placeholders as they appear *decorated* in example files (`change-me-control-plane-token`).
# Each marker is long enough not to occur by accident inside a random secret.
_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "changethis",
    "placeholder",
    "your-token",
    "yourtoken",
    "replace-me",
    "replaceme",
)


def _is_usable_secret(value: str | None, *, min_length: int = MIN_SECRET_LENGTH) -> bool:
    """True only for a configured, non-placeholder secret of at least ``min_length`` characters."""
    token = (value or "").strip()
    lowered = token.lower()
    if len(token) < min_length or lowered in _PLACEHOLDERS:
        return False
    return not any(marker in lowered for marker in _MARKERS)


def _bearer_matches(authorization: str | None, secret: str) -> bool:
    """Constant-time check that an ``Authorization`` header carries ``Bearer <secret>``."""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    supplied = authorization.removeprefix("Bearer ").strip()
    # `.strip()` on the secret too (a deliberate difference from examlops.credentials): a token
    # read from a file usually ends in a newline, and the header value never does.
    return hmac.compare_digest(supplied.encode(), secret.strip().encode())


@dataclass(frozen=True)
class Caller:
    """Who is calling. ``identity`` is the verified ``examlops.iam.Principal`` for a federated
    caller and ``None`` for the static token."""

    actor: str
    scopes: frozenset[str]
    tenant: str = "default"
    identity: Any = field(default=None, compare=False, repr=False)


def _secret() -> str:
    return os.getenv(TOKEN_ENV, "")


def _ingest_secret() -> str:
    return os.getenv(INGEST_TOKEN_ENV, "")


def _token_state(secret: str) -> str:
    """``unset`` (absent or blank) | ``ok`` | ``invalid`` (set, but a placeholder or too short)."""
    if not secret.strip():
        return "unset"
    return "ok" if _is_usable_secret(secret) else "invalid"


def _iam_status() -> str:
    """Identity federation (ADR 0120): ``off`` | ``ok`` | ``fail: <why>`` — never raises.

    An invalid trust file is ``fail``: federated tokens are refused (fail closed) while the static
    token keeps working.
    """
    try:
        from examlops import iam
    except ImportError:
        return "off"
    try:
        return "ok" if iam.load_config().enabled else "off"
    except iam.IamConfigError as exc:
        return f"fail: {exc}"
    except Exception as exc:  # noqa: BLE001 — an unreadable trust config must not open anything
        return f"fail: {type(exc).__name__}"


def _federated(token: str) -> Caller | None:
    """Verify a data-center IdP token, or ``None`` when it is not one we should verify."""
    from examlops import iam

    if not iam.looks_like_jwt(token):
        return None
    try:
        principal = iam.verify_access_token(token)
    except iam.AuthenticationError as exc:
        # `reason` is safe to log and return (it never contains the token).
        logger.warning("dataplane: federated token rejected: %s", exc.reason)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"Invalid bearer token: {exc.reason}",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
    if principal.role is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Authenticated as {principal.actor}, but your identity provider grants no "
            "ExaMLOps role",
        )
    # `ingest` for every mapped role is only a ticket to the project check: `may_ingest` then
    # needs `operator` (the write rule) or `editor` on the stream's project.
    scopes = {"read", "write", "ingest"} if principal.has_role("operator") else {"read", "ingest"}
    return Caller(
        actor=principal.actor,
        scopes=frozenset(scopes),
        tenant=principal.tenant,
        identity=principal,
    )


def auth_mode() -> str:
    """How this service authenticates right now, for the startup log and ``/health``.

    ``open`` (no token, no federation — reads open, writes refused) or a ``+``-joined subset of
    ``static`` / ``token-invalid``, ``ingest`` / ``ingest-token-invalid`` and ``federated`` /
    ``trust-file-invalid``. Never includes a token or the trust file's parse error.
    """
    parts: list[str] = []
    token = _token_state(_secret())
    if token == "ok":
        parts.append("static")
    elif token == "invalid":
        parts.append("token-invalid")
    ingest = _token_state(_ingest_secret())
    if ingest == "ok":
        parts.append("ingest")
    elif ingest == "invalid":
        parts.append("ingest-token-invalid")
    iam_state = _iam_status()
    if iam_state == "ok":
        parts.append("federated")
    elif iam_state != "off":
        parts.append("trust-file-invalid")
    return "+".join(parts) or "open"


def _authenticate(authorization: str | None, required: str) -> Caller | None:
    """``required`` is ``read``, ``write`` or ``ingest``. ``None`` only for an open read."""
    secret = _secret()
    token = _token_state(secret)
    ingest_secret = _ingest_secret()
    ingest = _token_state(ingest_secret)
    iam_state = _iam_status()
    if token == "unset" and ingest == "unset" and iam_state == "off":
        # The one open state: nothing configured at all (a loopback-only deployment).
        if required == "read":
            return None
        if required == "ingest":  # it runs a model: never open
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, INGEST_DISABLED_DETAIL)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"dataplane writes are disabled: set {TOKEN_ENV} to a real secret",
        )
    # The ingest token can authenticate only an ingest; for any other scope it is not a way in.
    static_ok = token == "ok" or (required == "ingest" and ingest == "ok")
    if not static_ok and iam_state != "ok":
        # Something is configured but nothing can authenticate. Fail closed — reads included:
        # a typo'd token or trust file must never be what opens the service. Neither the
        # token nor the trust file's parse error (it can quote a secret reference) is echoed.
        if logger.isEnabledFor(logging.DEBUG):  # auth_mode() re-reads the trust config
            logger.debug("dataplane: refusing request, auth mode %s", auth_mode())
        if token == "invalid":
            detail = f"{TOKEN_ENV} is set but is a placeholder or too short"
        elif required == "ingest" and ingest == "invalid":
            detail = f"{INGEST_TOKEN_ENV} is set but is a placeholder or too short"
        elif required == "ingest":
            detail = (
                f"dataplane authentication is not configured: set {TOKEN_ENV} or "
                f"{INGEST_TOKEN_ENV}, or fix the identity-federation trust file"
            )
        else:
            detail = (
                f"dataplane authentication is not configured: set {TOKEN_ENV} or fix the "
                "identity-federation trust file"
            )
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    if token == "ok" and _bearer_matches(authorization, secret):
        return Caller(actor=STATIC_ACTOR, scopes=frozenset({"read", "write", "ingest"}))
    if ingest == "ok" and _bearer_matches(authorization, ingest_secret):
        # Matched whatever the route: a write route then refuses it on the missing scope (403).
        return Caller(actor=INGEST_ACTOR, scopes=frozenset({"ingest"}))
    supplied = authorization.removeprefix("Bearer ").strip()
    caller = _federated(supplied) if iam_state == "ok" else None
    if caller is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid bearer token")
    return caller


# The one answer to a project-scoped deny: nothing about the project, its sources or the caller's
# own grants — the same body whether or not the source exists (no existence oracle).
FORBIDDEN_DETAIL = "Denied: no access to this source"

# The ``examlops.authz`` relation a scope needs on ``project:<p>`` (viewer ⊆ editor ⊆ owner), and
# the platform role a project-scoped role *in the token* must reach to count as that relation.
_RELATION = {"read": "viewer", "write": "editor"}
_ROLE_FOR = {"read": "viewer", "write": "operator"}


def _authorize_pdp(caller: Caller, required: str, resource: dict[str, Any]) -> None:
    """The center's AuthZEN/OPA decision for a federated caller (ADR 0120), deny-overrides; a PDP
    outage is a deny. The resource carries no ``tenant``: ``iam.authorize`` binds it to the
    principal's own (issuer-bound) tenant — stating the caller's tenant as the resource's own made
    the tenant invariant a tautology. The real scope is the ``project`` (source routes)."""
    from examlops import iam

    decision = iam.authorize(caller.identity, f"dataplane.{required}", resource, local_allowed=True)
    if not decision.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Denied: {decision.reason}")


def _require(
    required: str, request: Request, authorization: str | None, *, pdp: bool = True
) -> Caller | None:
    caller = _authenticate(authorization, required)
    if caller is None:
        return None
    if required not in caller.scopes:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing {required!r} scope")
    if pdp and caller.identity is not None:
        _authorize_pdp(
            caller,
            required,
            {"type": "dataplane", "id": request.url.path, "method": request.method},
        )
    return caller


def may_access_project(
    caller: Caller | None, required: str, project: str, *, audit: bool = True
) -> bool:
    """Spec §10: may this caller ``read``/``write`` sources of ``project`` (``""`` = global)?

    Only a federated caller is ever limited. The open loopback read (``None``) and the static
    ``DATAPLANE_TOKEN`` — the platform credential — keep full access. For a federated caller:

    * a global source is read by any mapped role and written only from ``operator`` up;
    * a project's source needs ``viewer`` (read) or ``editor`` (write) on ``project:<project>``
      in ``examlops.authz`` — or a project role at least as strong in the token's own project
      claims (the IdP's word, as ``iam.pdp.effective_role`` takes it). ``authz.check`` allows
      everything while ``EXAMLOPS_MULTITENANCY`` is off, and fails closed on a store error.

    ``audit=False`` is for *filtering* a listing: the caller asked for no particular project, so a
    source it cannot see is not an access attempt, and must not write one ``authz_deny`` audit
    event per hidden source (final review FC). It reads the same relations without the audit.
    """
    if caller is None or caller.identity is None:
        return True
    principal = caller.identity
    if not project:
        return required == "read" or bool(principal.has_role("operator"))
    from examlops import authz, iam
    from examlops.dataplane.safety import validate_name
    from examlops.dataplane.types import SpecError

    if not authz.multitenancy_enabled():
        return True
    try:
        validate_name(project, "project")
    except SpecError:
        # Not a project name at all — and `authz.check` walks `/`-separated parents, so `a/x`
        # would otherwise inherit a grant on `a`. Deny; there is nothing to authorise.
        return False
    claimed = (getattr(principal, "projects", None) or {}).get(project)
    if claimed and iam.ROLE_RANK.get(claimed, 0) >= iam.ROLE_RANK[_ROLE_FOR[required]]:
        return True
    if not audit:
        return _holds_relation(principal.id, _RELATION[required], f"project:{project}")
    return authz.check(
        principal.id, _RELATION[required], f"project:{project}", actor=principal.actor
    )


def _holds_relation(subject: str, relation: str, obj: str) -> bool:
    """``authz.check`` without its deny audit, for list filtering. A ``project:<name>`` object has
    no parent to walk (names are validated slash-free), so one lookup is the whole check. Fails
    closed on a store error, like ``authz.check``."""
    from examlops import authz
    from examlops.data.governance import get_relations_for

    try:
        held = get_relations_for(subject, obj)
    except Exception:  # noqa: BLE001 — never grant on a datastore failure
        return False
    return max((authz._rank(r) for r in held), default=0) >= authz._rank(relation)


def authorize_source(
    caller: Caller | None, request: Request, required: str, project: str, name: str | None = None
) -> None:
    """The authorisation of a source-scoped route, called once the route knows the project (it
    may sit in the request body): the project check, then the center's PDP with the real project
    as the resource. 403 ``FORBIDDEN_DETAIL`` on a project deny."""
    if not may_access_project(caller, required, project):
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_DETAIL)
    if caller is not None and caller.identity is not None:
        # The source key's shape (`store.source_key`), formatted rather than validated: a bad
        # name is the route's 400/404 to report, not an authorisation error.
        scope = project or "_global"
        resource_id = f"{scope}/{name}" if name else scope
        _authorize_pdp(
            caller,
            required,
            {"type": "dataplane", "id": resource_id, "method": request.method, "project": project},
        )


# Streams (ADR 0131 d6) get the same one-answer deny as sources: nothing about the project, its
# streams or the caller's own grants, whether or not the stream exists.
STREAM_FORBIDDEN_DETAIL = "Denied: no access to this stream"


def may_ingest(caller: Caller | None, project: str) -> bool:
    """ADR 0131 d6: may this caller push messages to a stream of ``project`` (``""`` = global)?

    The static credentials (``DATAPLANE_TOKEN``, ``DATAPLANE_INGEST_TOKEN``) may. A federated
    caller needs either:

    * ``operator`` or above — then exactly the source ``write`` rule applies
      (:func:`may_access_project`): a global stream from ``operator`` up, a project's stream with
      ``editor`` on it while ``EXAMLOPS_MULTITENANCY`` is on; or
    * ``editor`` on the stream's own project — a relation in ``examlops.authz``, or a project role
      of at least ``operator`` in the token's own project claims. This path is never granted by
      multitenancy being *off* (which makes ``authz.check`` allow everything): the relation must
      actually be held. A global stream has no project to be an editor of, so it needs operator.

    ``caller is None`` (an open read) never reaches here: ingest is never open.
    """
    if caller is None or caller.identity is None:
        return caller is not None
    principal = caller.identity
    if principal.has_role("operator"):
        return may_access_project(caller, "write", project)
    if not project:
        return False
    from examlops import authz, iam
    from examlops.dataplane.safety import validate_name
    from examlops.dataplane.types import SpecError

    try:
        validate_name(project, "project")
    except SpecError:
        return False  # not a project name (and `a/x` must never ride on a grant for `a`)
    claimed = (getattr(principal, "projects", None) or {}).get(project)
    if claimed and iam.ROLE_RANK.get(claimed, 0) >= iam.ROLE_RANK["operator"]:
        return True
    obj = f"project:{project}"
    if authz.multitenancy_enabled():
        return authz.check(principal.id, "editor", obj, actor=principal.actor)
    return _holds_relation(principal.id, "editor", obj)


def authorize_stream(
    caller: Caller | None, request: Request, required: str, project: str, name: str | None = None
) -> None:
    """The authorisation of a stream-scoped route (``read``, ``write`` or ``ingest`` — the A8b
    state/dead-letter routes pass ``write``, which :func:`may_access_project` handles like any
    other scope; review M8), BEFORE the stream is looked up: the project check, then the center's PDP (action ``dataplane.<required>``) with the
    real project as the resource. 403 :data:`STREAM_FORBIDDEN_DETAIL` on a project deny — one
    answer for a stream that exists and one that does not (no existence oracle)."""
    allowed = (
        may_ingest(caller, project)
        if required == "ingest"
        else may_access_project(caller, required, project)
    )
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, STREAM_FORBIDDEN_DETAIL)
    if caller is not None and caller.identity is not None:
        scope = project or "_global"
        resource_id = f"{scope}/{name}" if name else scope
        _authorize_pdp(
            caller,
            required,
            {
                "type": "dataplane",
                "kind": "stream",
                "id": resource_id,
                "method": request.method,
                "project": project,
            },
        )


def require_read(
    request: Request, authorization: str | None = Header(default=None)
) -> Caller | None:
    """Dependency for read routes that are not source-scoped. ``None`` = open read (no auth
    configured at all)."""
    return _require("read", request, authorization)


def require_write(request: Request, authorization: str | None = Header(default=None)) -> Caller:
    """Dependency for write routes that are not source-scoped. Always an authenticated caller
    with the ``write`` scope."""
    caller = _require("write", request, authorization)
    if caller is None:  # unreachable: _authenticate never opens a write
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "dataplane writes are disabled")
    return caller


def authenticate_read(
    request: Request, authorization: str | None = Header(default=None)
) -> Caller | None:
    """Dependency for SOURCE-SCOPED read routes: authentication and scope only. The route MUST
    then call :func:`authorize_source` with the source's project."""
    return _require("read", request, authorization, pdp=False)


def authenticate_write(
    request: Request, authorization: str | None = Header(default=None)
) -> Caller:
    """Dependency for SOURCE-SCOPED write routes: authentication and scope only. The route MUST
    then call :func:`authorize_source` with the source's project."""
    caller = _require("write", request, authorization, pdp=False)
    if caller is None:  # unreachable: _authenticate never opens a write
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "dataplane writes are disabled")
    return caller


def authenticate_ingest(
    request: Request, authorization: str | None = Header(default=None)
) -> Caller:
    """Dependency for the push route (``POST /streams/{name}/messages``): authentication and the
    ``ingest`` scope only. The route MUST then call :func:`authorize_stream` with ``"ingest"``."""
    caller = _require("ingest", request, authorization, pdp=False)
    if caller is None:  # unreachable: _authenticate never opens an ingest
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, INGEST_DISABLED_DETAIL)
    return caller


__all__ = [
    "FORBIDDEN_DETAIL",
    "INGEST_ACTOR",
    "INGEST_DISABLED_DETAIL",
    "INGEST_TOKEN_ENV",
    "STATIC_ACTOR",
    "STREAM_FORBIDDEN_DETAIL",
    "TOKEN_ENV",
    "Caller",
    "auth_mode",
    "authenticate_ingest",
    "authenticate_read",
    "authenticate_write",
    "authorize_source",
    "authorize_stream",
    "may_access_project",
    "may_ingest",
    "require_read",
    "require_write",
]
