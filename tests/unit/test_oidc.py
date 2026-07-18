"""OIDC access-token validation + identity propagation (Phase 2 item 2.1).

Uses a locally-generated RSA key + JWKS (no network, no IdP) to prove: a valid RS256 token yields a
verified subject+tenant identity, and every failure mode (wrong issuer/audience, expired, tampered,
missing subject, bad header, OIDC disabled) fails CLOSED with OidcError — never a partial identity.
"""

from __future__ import annotations

import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from examlops import oidc

_ISS = "https://idp.example.org/"
_AUD = "examlops"


@pytest.fixture
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key


@pytest.fixture
def jwks(keypair):
    from cryptography.hazmat.primitives import serialization

    pub = keypair.public_key()
    numbers = pub.public_numbers()

    def _b64(n: int) -> str:
        import base64

        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    _ = serialization  # (public_numbers path avoids PEM parsing)
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-1",
                "use": "sig",
                "alg": "RS256",
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }


def _token(keypair, **overrides):
    claims = {
        "iss": _ISS,
        "aud": _AUD,
        "sub": "alice",
        "tenant": "acme",
        "scope": "read write",
        "exp": 9999999999,
        **overrides,
    }
    return jwt.encode(claims, keypair, algorithm="RS256", headers={"kid": "test-1"})


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OIDC_ISSUER", _ISS)
    monkeypatch.setenv("EXAMLOPS_OIDC_AUDIENCE", _AUD)


def test_valid_token_yields_identity(keypair, jwks, enabled):
    ident = oidc.verify_token(_token(keypair), jwks=jwks)
    assert ident.subject == "alice"
    assert ident.tenant == "acme"
    assert ident.actor == "acme/alice"
    assert set(ident.scopes) == {"read", "write"}


def test_default_tenant_actor_is_bare_subject(keypair, jwks, enabled):
    ident = oidc.verify_token(_token(keypair, tenant="default"), jwks=jwks)
    assert ident.actor == "alice"


def test_wrong_issuer_fails_closed(keypair, jwks, enabled):
    with pytest.raises(oidc.OidcError):
        oidc.verify_token(_token(keypair, iss="https://evil.example/"), jwks=jwks)


def test_wrong_audience_fails_closed(keypair, jwks, enabled):
    with pytest.raises(oidc.OidcError):
        oidc.verify_token(_token(keypair, aud="someone-else"), jwks=jwks)


def test_expired_token_fails_closed(keypair, jwks, enabled):
    with pytest.raises(oidc.OidcError):
        oidc.verify_token(_token(keypair, exp=1), jwks=jwks)


def test_tampered_token_fails_closed(keypair, jwks, enabled):
    token = _token(keypair)
    tampered = token[:-4] + ("aaaa" if token[-4:] != "aaaa" else "bbbb")
    with pytest.raises(oidc.OidcError):
        oidc.verify_token(tampered, jwks=jwks)


def test_missing_subject_fails_closed(keypair, jwks, enabled, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OIDC_SUBJECT_CLAIM", "email")
    with pytest.raises(oidc.OidcError, match="subject"):
        oidc.verify_token(_token(keypair), jwks=jwks)  # token has no 'email' claim


def test_disabled_raises_not_configured(keypair, jwks, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_OIDC_ISSUER", raising=False)
    assert oidc.is_enabled() is False
    with pytest.raises(oidc.OidcNotConfigured):
        oidc.verify_token(_token(keypair), jwks=jwks)


def test_verify_bearer_header_parsing(keypair, jwks, enabled):
    token = _token(keypair)
    assert oidc.verify_bearer(f"Bearer {token}", jwks=jwks).subject == "alice"
    with pytest.raises(oidc.OidcError):
        oidc.verify_bearer(None, jwks=jwks)
    with pytest.raises(oidc.OidcError):
        oidc.verify_bearer(token, jwks=jwks)  # missing 'Bearer ' prefix


def test_inline_jwks_as_json_string(keypair, jwks, enabled):
    ident = oidc.verify_token(_token(keypair), jwks=json.dumps(jwks))
    assert ident.subject == "alice"
