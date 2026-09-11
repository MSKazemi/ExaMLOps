"""Discovery metadata + JWKS for trusted issuers, cached and rotation-aware (ADR 0120).

* **Discovery** (OIDC Discovery 1.0 §4 / RFC 8414 §3): ``<issuer>/.well-known/openid-configuration``
  is fetched once and cached; its ``issuer`` member must equal the configured issuer *exactly*,
  or the document is refused (§4.3 — otherwise a metadata endpoint could redirect key trust).
* **JWKS**: cached for ``EXAMLOPS_IAM_JWKS_TTL`` seconds (default 3600). A token whose ``kid`` is
  not in the cached set triggers **one** refetch — that is how a center's key rotation is picked up
  without a restart — but never more than once per ``EXAMLOPS_IAM_JWKS_MIN_REFRESH`` seconds
  (default 60), so a stream of tokens with random ``kid`` values cannot turn this service into a
  request amplifier against the center's IdP.

Only HTTPS endpoints are fetched unless the provider is loopback/explicitly insecure (validated in
``config``). Every fetch has a hard timeout (``EXAMLOPS_IAM_HTTP_TIMEOUT``, default 5 s).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from examlops.iam.config import ProviderConfig


class MetadataError(RuntimeError):
    """Discovery or key material could not be obtained or is not trustworthy (fail closed)."""


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def http_timeout() -> float:
    return _f("EXAMLOPS_IAM_HTTP_TIMEOUT", 5.0)


def _get_json(url: str) -> dict[str, Any]:
    import httpx

    try:
        resp = httpx.get(url, timeout=http_timeout(), follow_redirects=False)
    except httpx.HTTPError as exc:
        raise MetadataError(f"cannot fetch {url}: {exc.__class__.__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise MetadataError(f"{url} answered HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise MetadataError(f"{url} did not return JSON") from exc
    if not isinstance(data, dict):
        raise MetadataError(f"{url} did not return a JSON object")
    return data


_lock = threading.Lock()
_discovery: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks: dict[str, tuple[float, dict[str, Any]]] = {}
_last_forced: dict[str, float] = {}


def discovery(provider: ProviderConfig) -> dict[str, Any]:
    """The provider's OpenID/OAuth metadata (cached). Raises :class:`MetadataError`."""
    ttl = _f("EXAMLOPS_IAM_JWKS_TTL", 3600.0)
    with _lock:
        hit = _discovery.get(provider.name)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    url = provider.issuer.rstrip("/") + "/.well-known/openid-configuration"
    doc = _get_json(url)
    if doc.get("issuer") != provider.issuer:
        raise MetadataError(
            f"discovery document at {url} names issuer {doc.get('issuer')!r}, "
            f"not the configured {provider.issuer!r} — refusing it (OIDC Discovery §4.3)"
        )
    with _lock:
        _discovery[provider.name] = (time.monotonic(), doc)
    return doc


def endpoint(provider: ProviderConfig, key: str) -> str:
    """A metadata endpoint (``token_endpoint``, ``device_authorization_endpoint`` …)."""
    doc = discovery(provider)
    url = doc.get(key)
    if not isinstance(url, str) or not url:
        raise MetadataError(f"provider {provider.name!r} does not advertise {key}")
    if not url.startswith("https://") and not (
        provider.allow_insecure_http or url.startswith(("http://localhost", "http://127.0.0.1"))
    ):
        raise MetadataError(f"{key} {url!r} is not HTTPS")
    return url


def _jwks_uri(provider: ProviderConfig) -> str:
    if provider.jwks_uri:
        return provider.jwks_uri
    return endpoint(provider, "jwks_uri")


def jwks(provider: ProviderConfig, *, force: bool = False) -> dict[str, Any]:
    """The provider's JWKS. ``force`` refetches (rate-limited) — used on an unknown ``kid``."""
    if provider.jwks is not None:
        return provider.jwks
    ttl = _f("EXAMLOPS_IAM_JWKS_TTL", 3600.0)
    min_refresh = _f("EXAMLOPS_IAM_JWKS_MIN_REFRESH", 60.0)
    now = time.monotonic()
    with _lock:
        hit = _jwks.get(provider.name)
        if hit and not force and now - hit[0] < ttl:
            return hit[1]
        if hit and force and now - _last_forced.get(provider.name, -1e9) < min_refresh:
            return hit[1]  # rotation refetch is rate-limited; keep serving the cached set
        if force:
            _last_forced[provider.name] = now
    doc = _get_json(_jwks_uri(provider))
    if not isinstance(doc.get("keys"), list):
        raise MetadataError(f"JWKS for {provider.name!r} has no 'keys' list")
    with _lock:
        _jwks[provider.name] = (time.monotonic(), doc)
    return doc


def clear_cache() -> None:
    with _lock:
        _discovery.clear()
        _jwks.clear()
        _last_forced.clear()
