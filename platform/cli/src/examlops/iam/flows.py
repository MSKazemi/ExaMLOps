"""OAuth 2.0 / OIDC client flows ExaMLOps runs *against* a center's IdP (ADR 0120).

* **Authorization Code + PKCE** (RFC 6749 §4.1, RFC 7636 S256) for the dashboard, run by the BFF
  as a confidential client — the browser never sees an IdP access or refresh token
  (draft-ietf-oauth-browser-based-apps, BFF pattern). ``state`` defeats CSRF, ``nonce`` binds the
  ID token to this login, and the RFC 9207 ``iss`` authorization-response parameter is checked
  when the IdP sends it (mix-up defence with several trusted centers).
* **Device Authorization Grant** (RFC 8628) for ``exa`` on headless HPC login nodes: the user
  approves on any browser; the CLI polls, honouring ``interval`` and ``slow_down``.
* **Refresh** (RFC 6749 §6) so a CLI session survives short access-token lifetimes.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from examlops.iam import metadata
from examlops.iam.config import ClientConfig, ProviderConfig, resolve_secret_ref
from examlops.iam.tokens import AuthenticationError, verify_jwt


class FlowError(RuntimeError):
    """An OAuth flow step failed. ``error`` carries the RFC 6749 error code when there is one."""

    def __init__(self, message: str, error: str = "") -> None:
        super().__init__(message)
        self.error = error


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str
    method: str = "S256"


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(64)[:96]  # 43–128 chars of the unreserved set (RFC 7636 §4.1)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return Pkce(verifier, challenge)


def authorization_url(
    provider: ProviderConfig,
    client: ClientConfig,
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    pkce: Pkce,
    acr_values: tuple[str, ...] = (),
    max_age: int | None = None,
    prompt: str | None = None,
) -> str:
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(client.scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
    }
    if acr_values:
        params["acr_values"] = " ".join(acr_values)
    if max_age is not None:
        params["max_age"] = str(max_age)
    if prompt:
        params["prompt"] = prompt
    base = metadata.endpoint(provider, "authorization_endpoint")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


def _token_request(
    provider: ProviderConfig, client: ClientConfig, form: dict[str, str]
) -> dict[str, Any]:
    import httpx

    url = metadata.endpoint(provider, "token_endpoint")
    secret = resolve_secret_ref(client.client_secret_ref)
    auth = (client.client_id, secret) if secret else None
    if auth is None:
        form = {**form, "client_id": client.client_id}  # public client (RFC 6749 §2.3)
    try:
        resp = httpx.post(
            url,
            data=form,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=metadata.http_timeout(),
        )
    except httpx.HTTPError as exc:
        raise FlowError(f"token endpoint unreachable: {exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise FlowError(f"token endpoint returned non-JSON (HTTP {resp.status_code})") from exc
    if resp.status_code != 200:
        err = str(data.get("error", "")) if isinstance(data, dict) else ""
        desc = str(data.get("error_description", "")) if isinstance(data, dict) else ""
        raise FlowError(f"token request refused: {err} {desc}".strip(), err)
    if not isinstance(data, dict) or "access_token" not in data:
        raise FlowError("token response has no access_token")
    return data


def exchange_code(
    provider: ProviderConfig,
    client: ClientConfig,
    *,
    code: str,
    redirect_uri: str,
    pkce: Pkce,
) -> dict[str, Any]:
    return _token_request(
        provider,
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": pkce.verifier,
        },
    )


def verify_id_token(
    provider: ProviderConfig, client: ClientConfig, id_token: str, *, nonce: str
) -> dict[str, Any]:
    """Verify an ID token for this login (OIDC Core §3.1.3.7): signature, iss, aud, exp, nonce."""
    from examlops.iam.config import IamConfig

    try:
        _, claims = verify_jwt(id_token, IamConfig((provider,)), audience=(client.client_id,))
    except AuthenticationError as exc:
        raise FlowError(f"ID token rejected: {exc.reason}") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise FlowError("ID token nonce does not match this login (replay?)")
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != client.client_id:
        raise FlowError("ID token has several audiences but azp is not this client")
    return claims


def check_authorization_response_issuer(provider: ProviderConfig, iss: str | None) -> None:
    """RFC 9207: if the IdP returned ``iss`` with the code, it must be this provider's issuer."""
    if iss is not None and iss != provider.issuer:
        raise FlowError(f"authorization response came from {iss!r}, not {provider.issuer!r}")


# ── RFC 8628 device authorization ─────────────────────────────────────────────


def device_authorize(provider: ProviderConfig, client: ClientConfig) -> dict[str, Any]:
    import httpx

    url = metadata.endpoint(provider, "device_authorization_endpoint")
    try:
        resp = httpx.post(
            url,
            data={"client_id": client.client_id, "scope": " ".join(client.scopes)},
            headers={"Accept": "application/json"},
            timeout=metadata.http_timeout(),
        )
    except httpx.HTTPError as exc:
        raise FlowError(f"device authorization endpoint unreachable: {exc}") from exc
    data = resp.json() if resp.content else {}
    if resp.status_code != 200 or "device_code" not in data:
        raise FlowError(
            f"device authorization refused: {data.get('error', resp.status_code)}",
            str(data.get("error", "")),
        )
    return data


def poll_device_token(
    provider: ProviderConfig,
    client: ClientConfig,
    device: dict[str, Any],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll the token endpoint until the user approves, denies, or the code expires."""
    interval = max(1, int(device.get("interval", 5)))
    deadline = clock() + int(device.get("expires_in", 600))
    while clock() < deadline:
        sleep(interval)
        try:
            return _token_request(
                provider,
                client,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device["device_code"],
                },
            )
        except FlowError as exc:
            if exc.error == "authorization_pending":
                continue
            if exc.error == "slow_down":
                interval += 5  # RFC 8628 §3.5
                continue
            raise
    raise FlowError("device code expired before it was approved", "expired_token")


def refresh(provider: ProviderConfig, client: ClientConfig, refresh_token: str) -> dict[str, Any]:
    return _token_request(
        provider, client, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
