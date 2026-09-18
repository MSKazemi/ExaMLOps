"""Shared-secret hygiene for service-to-service bearer credentials.

Every service that accepts a static bearer (the control plane, Ray Serve's admin routes, the inference
ingress) has to answer the same two questions: *is the configured secret real?* and *does this
request carry it?* Answering them in one place keeps the rules identical everywhere — an example
value from ``.env.example`` must fail closed on every service, not just on the one that happened to
grow a placeholder list.

These are interim controls. Workload identity (SPIFFE/SPIRE, plan P3.1) replaces static bearers for
service-to-service calls; until then a static secret must at least be non-trivial, compared in
constant time, and absent-means-closed.
"""

from __future__ import annotations

import hmac

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


def is_usable_secret(value: str | None, *, min_length: int = MIN_SECRET_LENGTH) -> bool:
    """True only for a configured, non-placeholder secret of at least ``min_length`` characters."""
    token = (value or "").strip()
    lowered = token.lower()
    if len(token) < min_length or lowered in _PLACEHOLDERS:
        return False
    return not any(marker in lowered for marker in _MARKERS)


def bearer_matches(authorization: str | None, secret: str) -> bool:
    """Constant-time check that an ``Authorization`` header carries ``Bearer <secret>``.

    The secret is stripped exactly as :func:`is_usable_secret` strips it before judging it: a value
    read from a file or an environment variable with a trailing newline was judged usable and then
    refused the correct bearer (403). An empty token never matches, even an empty secret.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return False
    supplied = authorization.removeprefix("Bearer ").strip()
    expected = (secret or "").strip()
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())
