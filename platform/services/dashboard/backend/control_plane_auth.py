"""The dashboard's credential for the control plane, and signed URLs for bundled model images.

Plan P0.3 / finding B3. The control plane requires a bearer credential on every read. The dashboard
holds one in two places, in this order of precedence:

1. the ``control_plane_token`` secret in the encrypted config store (set from the Config page, the
   same secret the approvals router and the admin proxy already use), and
2. ``CONTROL_PLANE_TOKEN`` in the dashboard's environment, so a fresh stack works before anyone has
   opened the Config page.

The value is cached briefly: the Models registry fans out one control-plane call per model and must
not decrypt the secret once per call. A rotation from the Config page is picked up within the TTL.

Bundled README images are the one place a *browser* reads the control plane. An ``<img>`` cannot
send a bearer header, so the dashboard hands the browser a short-lived HMAC-signed URL to its own
endpoint, which fetches the image server-side with the credential — the same shape as the presigned
MinIO URLs used for uploaded images.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time

from settings import settings

_TOKEN_TTL_SECONDS = 30.0
_token_cache: tuple[float, str | None] | None = None
_token_lock = asyncio.Lock()

IMAGE_URL_TTL_SECONDS = 3600
# Model names and bundled-image filenames are both single path segments in the control plane's
# own routes. Refusing anything else here keeps a signed URL from ever naming another path.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


async def _read_store_secret() -> str | None:
    from database import AsyncSessionLocal  # noqa: PLC0415 - avoid engine creation at import
    from routers.config import get_decrypted_secret  # noqa: PLC0415

    async with AsyncSessionLocal() as db:
        return await get_decrypted_secret(db, "control_plane_token")


async def control_plane_token() -> str | None:
    """The bearer credential for control-plane calls, or ``None`` when none is configured."""
    global _token_cache
    async with _token_lock:
        now = time.monotonic()
        if _token_cache is not None and now < _token_cache[0]:
            return _token_cache[1]
        try:
            token = await _read_store_secret()
        except Exception:  # noqa: BLE001 - an unreadable store falls back to the environment
            token = None
        token = token or settings.control_plane_token or None
        _token_cache = (now + _TOKEN_TTL_SECONDS, token)
        return token


def reset_token_cache() -> None:
    """Forget the cached credential (tests; the Config page after a rotation)."""
    global _token_cache
    _token_cache = None


def _signing_key() -> bytes:
    # Derived, never the JWT secret itself: a leaked image signature must not help forge a session.
    return hmac.new(
        settings.dashboard_jwt_secret.encode(), b"examlops:bundled-image-url:v1", hashlib.sha256
    ).digest()


def _signature(name: str, filename: str, expires: int) -> str:
    message = f"{name}\0{filename}\0{expires}".encode()
    return hmac.new(_signing_key(), message, hashlib.sha256).hexdigest()


def valid_segment(value: str) -> bool:
    return bool(_SEGMENT.match(value)) and ".." not in value


def signed_image_path(name: str, filename: str, *, now: float | None = None) -> str:
    """A same-origin path the browser can load for ``IMAGE_URL_TTL_SECONDS``."""
    if not (valid_segment(name) and valid_segment(filename)):
        raise ValueError("model name and image filename must be single safe path segments")
    expires = int((now if now is not None else time.time()) + IMAGE_URL_TTL_SECONDS)
    sig = _signature(name, filename, expires)
    return f"/api/models/{name}/bundled-images/{filename}?exp={expires}&sig={sig}"


def verify_image_signature(
    name: str, filename: str, expires: int, sig: str, *, now: float | None = None
) -> bool:
    if not (valid_segment(name) and valid_segment(filename)):
        return False
    if expires < int(now if now is not None else time.time()):
        return False
    return hmac.compare_digest(_signature(name, filename, expires), sig)
