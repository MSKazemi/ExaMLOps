"""Verify SPIFFE JWT-SVIDs: services prove who they are with short-lived identities (ADR 0125).

A JWT-SVID is a JWT whose subject is a SPIFFE ID (``spiffe://<trust-domain>/<path>``), signed by
the trust domain's SPIRE server and valid for minutes. A service fetches one for the audience it
calls from its SPIRE agent and sends it as its bearer credential. :func:`verify` checks it the way
the SPIFFE JWT-SVID specification requires: a signature from a key in the trust bundle, an
asymmetric algorithm (never ``none`` or HMAC, which would let a public key be used as a shared
secret), the expected audience, an expiry, and a subject inside the configured trust domain.

Configuration:

``EXAMLOPS_SPIFFE_TRUST_DOMAIN``
    The trust domain, e.g. ``examlops.internal``. Unset: workload identities are off.
``EXAMLOPS_SPIFFE_BUNDLE``
    The trust bundle's JWT keys: a path to a JWKS / SPIFFE bundle file (``spire-server bundle
    show -format spiffe``) or to the bundle set spiffe-helper writes (``jwt_bundle_file_name``),
    the JSON itself, or an ``https://`` URL of SPIRE's OIDC discovery provider keys. A file is
    re-read when it changes, so SPIRE's key rotation needs no restart.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Asymmetric only (SPIFFE JWT-SVID §3: RS*, PS*, ES*).
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
_URL_TTL_SECONDS = 300.0


class WorkloadIdentityError(Exception):
    """The credential is not a valid JWT-SVID for this trust domain and audience."""


@dataclass(frozen=True)
class Workload:
    spiffe_id: str
    audience: tuple[str, ...]
    expires_at: float


def trust_domain() -> str:
    return os.getenv("EXAMLOPS_SPIFFE_TRUST_DOMAIN", "").strip().lower()


def enabled() -> bool:
    return bool(trust_domain() and os.getenv("EXAMLOPS_SPIFFE_BUNDLE", "").strip())


def looks_like_svid(token: str) -> bool:
    """Cheap pre-check, no verification: a JWT whose unverified subject is a SPIFFE ID."""
    if token.count(".") != 2:
        return False
    try:
        import jwt  # noqa: PLC0415

        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001 - not a JWT at all
        return False
    return str(claims.get("sub", "")).startswith("spiffe://")


class _Bundle:
    """The trust bundle's JWT keys, re-read when the file changes or the URL copy ages out."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._source = ""
        self._stamp: Any = None
        self._keys: dict[str, Any] = {}

    def keys(self) -> dict[str, Any]:
        source = os.getenv("EXAMLOPS_SPIFFE_BUNDLE", "").strip()
        with self._lock:
            stamp = self._freshness(source)
            if source != self._source or stamp != self._stamp or not self._keys:
                self._keys = _parse_keys(_read(source))
                self._source, self._stamp = source, stamp
            return self._keys

    @staticmethod
    def _freshness(source: str) -> Any:
        if source.startswith(("http://", "https://")):
            return int(time.monotonic() // _URL_TTL_SECONDS)
        if source.startswith("{"):
            return source
        try:
            stat = Path(source).stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)


def _read(source: str) -> str:
    if source.startswith("{"):
        return source
    if source.startswith("https://"):
        import httpx  # noqa: PLC0415

        response = httpx.get(source, timeout=5.0)
        response.raise_for_status()
        return response.text
    if source.startswith("http://"):
        raise WorkloadIdentityError("the trust bundle must come from a file or an https:// URL")
    try:
        return Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkloadIdentityError(f"cannot read the trust bundle: {exc}") from exc


def _own_domain_jwks(document: Any) -> Any:
    """The JWKS for the configured trust domain out of a Workload API bundle set.

    spiffe-helper's ``jwt_bundle_file_name`` writes ``{"<trust domain>": base64(JWKS)}``, one entry
    per domain including federated ones. Only this trust domain's keys may verify its SVIDs.
    """
    if not isinstance(document, dict) or "keys" in document:
        return document
    entry = document.get(trust_domain())
    if isinstance(entry, str):
        try:
            return json.loads(base64.b64decode(entry, validate=True))
        except ValueError as exc:
            raise WorkloadIdentityError(f"the trust bundle entry is not a JWKS: {exc}") from exc
    return entry if isinstance(entry, dict) else {}


def _parse_keys(text: str) -> dict[str, Any]:
    """``kid`` → public key, for the JWT-SVID keys of a JWKS, SPIFFE bundle or bundle set."""
    import jwt  # noqa: PLC0415

    try:
        document = _own_domain_jwks(json.loads(text))
    except ValueError as exc:
        raise WorkloadIdentityError(f"the trust bundle is not JSON: {exc}") from exc
    keys: dict[str, Any] = {}
    for jwk in document.get("keys", []):
        # A SPIFFE bundle also lists the X.509 CA (`use: x509-svid`); only JWT keys verify SVIDs.
        if jwk.get("use") not in (None, "jwt-svid", "sig") or not jwk.get("kid"):
            continue
        try:
            keys[jwk["kid"]] = jwt.PyJWK(jwk).key
        except Exception:  # noqa: BLE001 - one malformed key does not disable the others
            continue
    if not keys:
        raise WorkloadIdentityError("the trust bundle holds no JWT-SVID signing key")
    return keys


_bundle = _Bundle()


def verify(token: str, audience: str) -> Workload:
    """The workload a JWT-SVID proves, or :class:`WorkloadIdentityError`."""
    import jwt  # noqa: PLC0415

    domain = trust_domain()
    if not domain:
        raise WorkloadIdentityError("no SPIFFE trust domain is configured")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise WorkloadIdentityError(f"not a JWT: {exc}") from exc
    if header.get("alg") not in ALGORITHMS:
        raise WorkloadIdentityError(
            f"algorithm {header.get('alg')!r} is not allowed for a JWT-SVID"
        )
    key = _bundle.keys().get(str(header.get("kid", "")))
    if key is None:
        raise WorkloadIdentityError("signed by a key that is not in the trust bundle")
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=ALGORITHMS,
            audience=audience,
            options={"require": ["sub", "aud", "exp"]},
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise WorkloadIdentityError(str(exc)) from exc
    subject = str(claims["sub"])
    if not subject.startswith(f"spiffe://{domain}/"):
        raise WorkloadIdentityError(f"{subject} is not in trust domain {domain}")
    aud = claims["aud"]
    return Workload(
        spiffe_id=subject,
        audience=tuple(aud if isinstance(aud, list) else [aud]),
        expires_at=float(claims["exp"]),
    )
