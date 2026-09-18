"""Authentication for Ray Serve's *admin* routes: model reload and live traffic-rule updates.

Plan P0.6 / findings S1. ``/reload``, ``/reload/{model}`` and ``/infer-pipeline/traffic-rules/{model}``
were unauthenticated on a port published to every interface. Anyone who could reach it could force
model reloads in a loop, or silently reroute production traffic to a canary.

The admin routes now require ``Authorization: Bearer $RAY_SERVE_ADMIN_TOKEN``. Unset (or a
placeholder) means **closed** — the same convention as the control plane's ``CONTROL_PLANE_TOKEN``
— and never degrades the data path: ``/predict`` and ``/infer-pipeline/infer`` are untouched, and
serving still picks up alias moves on its own within ``RAY_RELOAD_POLL_SECONDS``, so a stack that has
not set the token loses only the *immediate* reload, not correctness.
"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException, status

from examlops.credentials import bearer_matches, is_usable_secret

ADMIN_TOKEN_ENV = "RAY_SERVE_ADMIN_TOKEN"


def require_serving_admin(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency guarding a serving admin route."""
    secret = os.getenv(ADMIN_TOKEN_ENV, "")
    if not is_usable_secret(secret):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Serving admin routes are disabled: set {ADMIN_TOKEN_ENV} to a real secret. "
            "Alias moves still reload on their own within RAY_RELOAD_POLL_SECONDS.",
        )
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    if not bearer_matches(authorization, secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid bearer token")


def admin_headers() -> dict[str, str]:
    """Headers a *caller* of the admin routes sends (empty when no token is configured)."""
    secret = os.getenv(ADMIN_TOKEN_ENV, "")
    return {"Authorization": f"Bearer {secret}"} if secret else {}
