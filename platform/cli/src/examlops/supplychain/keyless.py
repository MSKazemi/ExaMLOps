"""Sigstore keyless signing and verification (ADR 0013 clause 1).

The signer never holds a long-lived key. It presents an OIDC identity token to **Fulcio**, which
issues a short-lived certificate binding an ephemeral key to that identity; the signature is then
logged in the **Rekor** transparency log. What comes back is a Sigstore *bundle* (certificate,
signature, log inclusion proof), stored in ``model_signatures.signature`` as JSON.

A verifier trusts **identities**, not keys: ``EXAMLOPS_SIGSTORE_IDENTITIES`` lists the
``<identity>|<issuer>`` pairs allowed to sign models (a CI workflow URI, a release operator's
e-mail). Verification is fail-closed on every axis:

* no trusted identity configured → ``untrusted-identity`` (never "any identity will do");
* an entry without an issuer is ignored — an identity is only meaningful modulo its issuer;
* the ``sigstore`` library missing → ``unavailable``, which ``enforce`` refuses like any other
  failure to verify.

The library is an optional extra (``examlops[supplychain]``) imported lazily: a slim serving image
without it still verifies Ed25519/HMAC rows, and only a ``sigstore-v1`` row needs it.

Configuration:

``EXAMLOPS_SIGSTORE_IDENTITY_TOKEN`` / ``EXAMLOPS_SIGSTORE_IDENTITY_TOKEN_FILE``
    The OIDC token to sign with. Unset → ambient detection (GitHub Actions, GitLab, Buildkite,
    GCP) via ``sigstore.oidc.detect_credential``. There is deliberately no interactive browser
    flow: signing runs in pipelines.
``EXAMLOPS_SIGSTORE_INSTANCE``
    ``production`` (default) or ``staging``.
``EXAMLOPS_SIGSTORE_OFFLINE``
    Truthy → verify with the trusted root already in the local TUF cache, never refreshing it
    (air-gapped HPC). Signing always needs Fulcio and Rekor, so it is unaffected.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SIGSTORE = "sigstore-v1"
IDENTITIES_ENV = "EXAMLOPS_SIGSTORE_IDENTITIES"
_TRUTHY = {"1", "true", "yes", "on"}


class KeylessUnavailable(RuntimeError):
    """Keyless signing cannot run here: no ``sigstore`` library, or no OIDC identity token."""


@dataclass(frozen=True)
class KeylessVerdict:
    ok: bool
    reason: str


def available() -> bool:
    """Whether the optional ``sigstore`` library is importable."""
    try:
        return importlib.util.find_spec("sigstore") is not None
    except (ImportError, ValueError, AttributeError):  # a broken/blocked install is "not here"
        return False


def trusted_identities() -> list[tuple[str, str]]:
    """``(identity, issuer)`` pairs from ``EXAMLOPS_SIGSTORE_IDENTITIES`` (comma-separated
    ``<identity>|<issuer>``). An entry with no issuer is dropped, never widened to "any issuer"."""
    out: list[tuple[str, str]] = []
    for item in os.getenv(IDENTITIES_ENV, "").split(","):
        identity, sep, issuer = item.strip().partition("|")
        if sep and identity.strip() and issuer.strip():
            out.append((identity.strip(), issuer.strip()))
    return out


def _offline() -> bool:
    return os.getenv("EXAMLOPS_SIGSTORE_OFFLINE", "").strip().lower() in _TRUTHY


def _trust_config(*, offline: bool = False) -> Any:
    from sigstore.models import ClientTrustConfig  # noqa: PLC0415 - optional extra

    instance = os.getenv("EXAMLOPS_SIGSTORE_INSTANCE", "production").strip().lower()
    if instance == "staging":
        return ClientTrustConfig.staging(offline=offline)
    if instance != "production":
        raise KeylessUnavailable(f"unknown EXAMLOPS_SIGSTORE_INSTANCE {instance!r}")
    return ClientTrustConfig.production(offline=offline)


def _raw_identity_token() -> str:
    token = os.getenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN", "").strip()
    path = os.getenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN_FILE", "").strip()
    if not token and path:
        token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        from sigstore.oidc import detect_credential  # noqa: PLC0415 - optional extra

        token = (detect_credential() or "").strip()
    if not token:
        raise KeylessUnavailable(
            "no OIDC identity token: set EXAMLOPS_SIGSTORE_IDENTITY_TOKEN(_FILE) or run where an "
            "ambient CI credential is available"
        )
    return token


def _require_library() -> None:
    if not available():
        raise KeylessUnavailable(
            "the sigstore library is not installed (pip install 'examlops[supplychain]')"
        )


def _signer_context() -> tuple[Any, Any]:
    from sigstore.oidc import IdentityToken  # noqa: PLC0415 - optional extra
    from sigstore.sign import SigningContext  # noqa: PLC0415

    token = IdentityToken(_raw_identity_token())
    return SigningContext.from_trust_config(_trust_config()), token


def _identity_of(token: Any) -> str:
    return json.dumps(
        {"identity": str(token.identity), "issuer": str(token.issuer)}, sort_keys=True
    )


def sign_blob(payload: bytes) -> tuple[str, str]:
    """Keyless-sign ``payload``: ``(bundle_json, signer_identity_json)``.

    Raises :class:`KeylessUnavailable` when signing cannot run here — the caller decides whether a
    configured Ed25519 key is an acceptable fallback (ADR 0013: "D7-managed key fallback for
    offline HPC").
    """
    _require_library()
    ctx, token = _signer_context()
    with ctx.signer(token, cache=True) as signer:
        bundle = signer.sign_artifact(payload)
    return bundle.to_json(), _identity_of(token)


def sign_statement(statement_json: bytes) -> tuple[str, str]:
    """Keyless-sign an in-toto statement as a DSSE envelope: ``(bundle_json, identity_json)``."""
    _require_library()
    from sigstore import dsse  # noqa: PLC0415 - optional extra

    ctx, token = _signer_context()
    with ctx.signer(token, cache=True) as signer:
        bundle = signer.sign_dsse(dsse.Statement(statement_json))
    return bundle.to_json(), _identity_of(token)


def _verifier_and_policy() -> tuple[Any, Any] | KeylessVerdict:
    if not available():
        return KeylessVerdict(False, "unavailable: sigstore library not installed")
    identities = trusted_identities()
    if not identities:
        return KeylessVerdict(False, f"untrusted-identity: {IDENTITIES_ENV} is empty")
    from sigstore.verify import Verifier, policy  # noqa: PLC0415 - optional extra

    try:
        verifier = Verifier(trusted_root=_trust_config(offline=_offline()).trusted_root)
    except Exception as exc:  # noqa: BLE001 - no trusted root is no verification
        return KeylessVerdict(False, f"unavailable: no Sigstore trusted root ({exc})")
    rules = [policy.Identity(identity=i, issuer=iss) for i, iss in identities]
    return verifier, policy.AnyOf(rules)


def verify_blob(payload: bytes, bundle_json: str) -> KeylessVerdict:
    """Check a keyless signature over ``payload`` against the trusted identities."""
    prepared = _verifier_and_policy()
    if isinstance(prepared, KeylessVerdict):
        return prepared
    verifier, rule = prepared
    from sigstore.models import Bundle  # noqa: PLC0415 - optional extra

    try:
        verifier.verify_artifact(payload, Bundle.from_json(bundle_json), rule)
    except Exception as exc:  # noqa: BLE001 - VerificationError, malformed bundle, …
        return KeylessVerdict(False, f"bad-signature: {exc}")
    return KeylessVerdict(True, "verified")


def verify_statement(bundle_json: str) -> tuple[KeylessVerdict, bytes | None]:
    """Verify a keyless DSSE bundle and return its payload (only when verified)."""
    prepared = _verifier_and_policy()
    if isinstance(prepared, KeylessVerdict):
        return prepared, None
    verifier, rule = prepared
    from sigstore.models import Bundle  # noqa: PLC0415 - optional extra

    try:
        payload_type, payload = verifier.verify_dsse(Bundle.from_json(bundle_json), rule)
    except Exception as exc:  # noqa: BLE001
        return KeylessVerdict(False, f"bad-signature: {exc}"), None
    if payload_type != "application/vnd.in-toto+json":
        return KeylessVerdict(False, f"malformed: unexpected payload type {payload_type!r}"), None
    return KeylessVerdict(True, "verified"), payload
