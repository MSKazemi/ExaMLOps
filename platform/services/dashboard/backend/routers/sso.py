"""Organisation single sign-on for the dashboard — the BFF side of ADR 0120.

The browser only ever talks to this backend. The backend is a *confidential* OIDC client of the
data center's IdP and runs Authorization Code + PKCE (RFC 7636, S256) with ``state`` and
``nonce``; the IdP's access/refresh tokens never reach the browser, and the resulting dashboard
session lives in an ``HttpOnly; Secure; SameSite=Strict`` cookie (RFC 10017, BFF pattern).

Flow::

    GET /api/auth/sso/providers              → which centers offer SSO (+ whether passwords work)
    GET /api/auth/sso/{provider}/login        → 302 to the IdP; the PKCE verifier, state and nonce
                                                ride in a signed, HttpOnly, 10-minute cookie
    GET /api/auth/sso/callback                → verify state + RFC 9207 iss, exchange the code,
                                                verify the ID token (nonce), map roles/tenant,
                                                set the session cookie, 302 back into the SPA
    POST /api/auth/sso/logout                 → clear the session; returns the IdP's
                                                end-session URL for RP-initiated logout

Routes are plain ``def`` on purpose: they make blocking calls to the IdP (discovery, token
endpoint, JWKS), which FastAPI then runs in its threadpool instead of on the event loop.

``?step_up=1`` on the login route asks the IdP for the provider's ``step_up.acr_values`` with
``max_age=0`` / ``prompt=login`` — the SPA sends the user there after an RFC 9470 challenge.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import jwt
from auth import claims_from_principal, issue_token, session_cookie_name
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/sso", tags=["auth"])

_STATE_COOKIE_TTL = 600
_CALLBACK_PATH = "/api/auth/sso/callback"


def _state_cookie_name() -> str:
    return "__Host-examlops_sso" if settings.dashboard_session_cookie_secure else "examlops_sso"


def _state_key() -> bytes:
    # Derived, never the session secret itself: a leaked login-state cookie must not mint sessions.
    return hmac.new(
        settings.dashboard_jwt_secret.encode(), b"examlops:sso-login-state:v1", hashlib.sha256
    ).digest()


def _iam():
    try:
        from examlops import iam  # type: ignore
        from examlops.iam import flows  # type: ignore
    except ImportError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "identity federation is not installed"
        ) from exc
    return iam, flows


def _config():
    iam, _ = _iam()
    try:
        return iam.load_config()
    except iam.IamConfigError as exc:
        logger.error("SSO unavailable — invalid trust file: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "identity federation is misconfigured"
        ) from exc


def _redirect_uri() -> str:
    return settings.public_dashboard_url.rstrip("/") + _CALLBACK_PATH


def _safe_return_to(value: str | None) -> str:
    """Only same-origin absolute paths — never ``//host`` or a scheme (open-redirect defence)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _audit(actor: str, action: str, details: dict[str, Any], tenant: str = "default") -> None:
    try:
        from audit_write import audit

        audit(actor, action, None, details, source="dashboard-sso", tenant=tenant)
    except Exception as exc:  # noqa: BLE001 — never block a login on audit, never lose it silently
        logger.warning("SSO audit write failed (%s): %s", action, exc)


def _fail(reason: str) -> RedirectResponse:
    resp = RedirectResponse(f"/?{urlencode({'sso_error': reason})}", status_code=302)
    resp.delete_cookie(_state_cookie_name(), path="/")
    return resp


@router.get("/providers", summary="Identity providers offering dashboard SSO")
def providers() -> dict[str, Any]:
    try:
        iam, _ = _iam()
        cfg = iam.load_config()
    except Exception:  # noqa: BLE001 — a login page must render even when federation is broken
        return {"providers": [], "local_login": settings.dashboard_local_login}
    items = [
        {
            "name": p.name,
            "display_name": p.label,
            "login_url": f"/api/auth/sso/{p.name}/login",
            "step_up": p.step_up.enabled,
        }
        for p in cfg.providers
        if p.client("dashboard") is not None
    ]
    return {"providers": items, "local_login": settings.dashboard_local_login}


@router.get("/{provider}/login", summary="Start SSO with a data center's IdP")
def login(provider: str, return_to: str | None = None, step_up: bool = False) -> RedirectResponse:
    _, flows = _iam()
    p = _config().by_name(provider)
    client = p.client("dashboard") if p is not None else None
    if p is None or client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such SSO provider")
    pkce = flows.new_pkce()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    try:
        url = flows.authorization_url(
            p,
            client,
            redirect_uri=_redirect_uri(),
            state=state,
            nonce=nonce,
            pkce=pkce,
            acr_values=p.step_up.acr_values if step_up else (),
            max_age=0 if step_up else None,
            prompt="login" if step_up else None,
        )
    except Exception as exc:  # noqa: BLE001 — discovery failure: say which center is unreachable
        logger.error("SSO login to %s failed: %s", provider, exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"cannot reach the identity provider '{p.label}'"
        ) from exc
    login_state = jwt.encode(
        {
            "p": p.name,
            "s": state,
            "n": nonce,
            "v": pkce.verifier,
            "r": _safe_return_to(return_to),
            "exp": int(time.time()) + _STATE_COOKIE_TTL,
        },
        _state_key(),
        algorithm="HS256",
    )
    resp = RedirectResponse(url, status_code=302)
    # SameSite=Lax, not Strict: the IdP's redirect back to /callback is a cross-site top-level
    # navigation, and the cookie has to ride on it. It holds nothing that grants access by itself.
    resp.set_cookie(
        _state_cookie_name(),
        login_state,
        max_age=_STATE_COOKIE_TTL,
        httponly=True,
        secure=settings.dashboard_session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return resp


@router.get("/callback", summary="OIDC redirect endpoint (Authorization Code + PKCE)")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    iss: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    iam, flows = _iam()
    raw = request.cookies.get(_state_cookie_name())
    if not raw:
        return _fail("session_expired")
    try:
        ls = jwt.decode(raw, _state_key(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return _fail("session_expired")
    if error:
        _audit(f"sso:{ls.get('p')}", "sso_login_failed", {"error": error[:64]})
        return _fail("idp_" + "".join(c for c in error if c.isalnum() or c == "_")[:40])
    if not code or not state or not secrets.compare_digest(state, str(ls.get("s", ""))):
        return _fail("state_mismatch")
    p = _config().by_name(str(ls.get("p")))
    client = p.client("dashboard") if p is not None else None
    if p is None or client is None:
        return _fail("unknown_provider")
    try:
        flows.check_authorization_response_issuer(p, iss)  # RFC 9207 mix-up defence
        tokens = flows.exchange_code(
            p,
            client,
            code=code,
            redirect_uri=_redirect_uri(),
            pkce=flows.Pkce(str(ls["v"]), ""),
        )
        id_claims = flows.verify_id_token(p, client, str(tokens.get("id_token", "")), nonce=ls["n"])
    except Exception as exc:  # noqa: BLE001 — every failure is a failed login, never a session
        logger.warning("SSO callback from %s rejected: %s", p.name, exc)
        _audit(f"sso:{p.name}", "sso_login_failed", {"reason": str(exc)[:200]})
        return _fail("login_rejected")

    from examlops.iam.tokens import principal_from_claims  # type: ignore

    # Roles from the access token when it is a JWT for this platform (groups usually live there);
    # otherwise from the ID token. acr/amr/auth_time always from the ID token (OIDC Core §2).
    principal = None
    access = str(tokens.get("access_token", ""))
    if iam.looks_like_jwt(access):
        try:
            principal = iam.verify_access_token(access, iam.IamConfig((p,)))
        except iam.AuthenticationError:
            principal = None
    try:
        if principal is None:
            principal = principal_from_claims(p, id_claims, "id_token")
        else:
            principal = principal_from_claims(p, {**principal.claims, **id_claims}, "id_token")
    except iam.AuthenticationError as exc:
        _audit(f"sso:{p.name}", "sso_login_failed", {"reason": exc.reason[:200]})
        return _fail("login_rejected")
    if principal.role is None:
        _audit(principal.actor, "sso_login_denied", {"reason": "no_role"}, principal.tenant)
        return _fail("no_role")

    identity = claims_from_principal(principal, via="sso")
    token, expires_at = issue_token(
        principal.role,  # type: ignore[arg-type]
        identity=identity,
        ttl=timedelta(hours=settings.dashboard_sso_session_hours),
    )
    _audit(
        principal.actor,
        "sso_login",
        {
            "provider": p.name,
            "role": principal.role,
            "acr": principal.acr,
            "matched": list(principal.matched)[:10],
        },
        principal.tenant,
    )
    resp = RedirectResponse(_safe_return_to(str(ls.get("r", "/"))), status_code=302)
    resp.delete_cookie(_state_cookie_name(), path="/")
    resp.set_cookie(
        session_cookie_name(),
        token,
        max_age=int(timedelta(hours=settings.dashboard_sso_session_hours).total_seconds()),
        httponly=True,
        secure=settings.dashboard_session_cookie_secure,
        samesite="strict",
        path="/",
    )
    return resp


@router.post("/logout", summary="End the SSO session (and get the IdP's logout URL)")
def logout(request: Request, response: Response) -> dict[str, Any]:
    from auth import verify_token

    end_session: str | None = None
    cookie = request.cookies.get(session_cookie_name())
    if cookie:
        try:
            claims = verify_token(cookie)
            iam, _ = _iam()
            p = iam.load_config().by_name(str(claims.get("idp", "")))
            if p is not None:
                from examlops.iam import metadata  # type: ignore

                end_session = metadata.discovery(p).get("end_session_endpoint")
                _audit(
                    str(claims.get("sub")),
                    "sso_logout",
                    {"provider": p.name},
                    str(claims.get("tenant", "default")),
                )
        except Exception:  # noqa: BLE001 — logout always succeeds locally
            end_session = None
    response.delete_cookie(session_cookie_name(), path="/")
    return {"ok": True, "end_session_url": end_session}
