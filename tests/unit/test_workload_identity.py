"""JWT-SVID verification (ADR 0125 phase 1).

Each test mints a token with a real EC key and checks one rule of the SPIFFE JWT-SVID
specification: signature from a bundle key, asymmetric algorithm only, audience, expiry, and a
subject inside the configured trust domain. The HMAC case is the classic key-confusion attack:
a verifier that accepts HS256 can be fooled into treating a public key as the shared secret.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from examlops import workload_identity as wi

DOMAIN = "examlops.internal"
AUD = "control-plane"


def _jwk(private, kid: str, use: str = "jwt-svid") -> dict:
    jwk = json.loads(ECAlgorithm.to_jwk(private.public_key()))
    return {**jwk, "kid": kid, "use": use}


@pytest.fixture()
def keys(tmp_path, monkeypatch):
    signing = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())
    bundle = tmp_path / "bundle.json"
    # A SPIFFE bundle lists the X.509 CA too; only the jwt-svid key verifies tokens.
    bundle.write_text(json.dumps({"keys": [_jwk(signing, "k1"), _jwk(other, "x509", "x509-svid")]}))
    monkeypatch.setenv("EXAMLOPS_SPIFFE_TRUST_DOMAIN", DOMAIN)
    monkeypatch.setenv("EXAMLOPS_SPIFFE_BUNDLE", str(bundle))
    return {"signing": signing, "other": other, "bundle": bundle}


def _svid(key, *, sub=f"spiffe://{DOMAIN}/autopilot", aud=AUD, exp=300, kid="k1", alg="ES256"):
    claims = {"sub": sub, "aud": [aud], "exp": int(time.time()) + exp, "iat": int(time.time())}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def test_a_valid_svid_names_its_workload(keys):
    workload = wi.verify(_svid(keys["signing"]), AUD)
    assert workload.spiffe_id == f"spiffe://{DOMAIN}/autopilot"
    assert workload.audience == (AUD,)


@pytest.mark.parametrize(
    ("token_kw", "message"),
    [
        ({"aud": "dashboard"}, "(?i)audience"),
        ({"exp": -120}, "expired"),
        ({"sub": "spiffe://another.domain/autopilot"}, "not in trust domain"),
        ({"kid": "unknown"}, "not in the trust bundle"),
    ],
)
def test_each_rule_is_enforced(keys, token_kw, message):
    with pytest.raises(wi.WorkloadIdentityError, match=message):
        wi.verify(_svid(keys["signing"], **token_kw), AUD)


def test_a_key_not_in_the_bundle_cannot_sign(keys):
    forged = _svid(keys["other"], kid="k1")  # right kid, wrong key
    with pytest.raises(wi.WorkloadIdentityError):
        wi.verify(forged, AUD)


def test_the_x509_key_of_the_bundle_does_not_verify_jwts(keys):
    with pytest.raises(wi.WorkloadIdentityError, match="not in the trust bundle"):
        wi.verify(_svid(keys["other"], kid="x509"), AUD)


def test_hmac_and_none_are_refused(keys):
    """Key confusion: HS256 'signed' with the public key as the secret must not verify."""
    public_pem = keys["bundle"].read_text()
    hmac = jwt.encode(
        {"sub": f"spiffe://{DOMAIN}/a", "aud": [AUD], "exp": int(time.time()) + 60},
        public_pem,
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(wi.WorkloadIdentityError, match="not allowed"):
        wi.verify(hmac, AUD)
    unsigned = jwt.encode(
        {"sub": f"spiffe://{DOMAIN}/a", "aud": [AUD], "exp": int(time.time()) + 60},
        None,
        algorithm="none",
    )
    with pytest.raises(wi.WorkloadIdentityError, match="not allowed"):
        wi.verify(unsigned, AUD)


def test_a_rotated_bundle_file_is_picked_up_without_a_restart(keys):
    new_key = ec.generate_private_key(ec.SECP256R1())
    token = _svid(new_key, kid="k2")
    with pytest.raises(wi.WorkloadIdentityError):
        wi.verify(token, AUD)
    keys["bundle"].write_text(json.dumps({"keys": [_jwk(new_key, "k2")]}))
    assert wi.verify(token, AUD).spiffe_id.endswith("/autopilot")


def test_the_workload_api_bundle_set_is_read_for_this_trust_domain_only(keys):
    """spiffe-helper's `jwt_bundle_file_name` writes {trust domain: base64(JWKS)}, per domain."""
    import base64

    def encoded(key, kid: str) -> str:
        jwks = {"keys": [{k: v for k, v in _jwk(key, kid).items() if k != "use"}]}
        return base64.b64encode(json.dumps(jwks).encode()).decode()

    federated = ec.generate_private_key(ec.SECP256R1())
    keys["bundle"].write_text(
        json.dumps(
            {DOMAIN: encoded(keys["signing"], "k1"), "partner.org": encoded(federated, "p1")}
        )
    )
    assert wi.verify(_svid(keys["signing"]), AUD).spiffe_id.endswith("/autopilot")
    # A federated domain's key must never verify an SVID for this one.
    with pytest.raises(wi.WorkloadIdentityError, match="not in the trust bundle"):
        wi.verify(_svid(federated, kid="p1"), AUD)
    keys["bundle"].write_text(json.dumps({"partner.org": encoded(federated, "p1")}))
    with pytest.raises(wi.WorkloadIdentityError, match="no JWT-SVID signing key"):
        wi.verify(_svid(keys["signing"]), AUD)


def test_only_a_jwt_with_a_spiffe_subject_looks_like_an_svid(keys):
    assert wi.looks_like_svid(_svid(keys["signing"]))
    idp = jwt.encode({"sub": "alice", "exp": int(time.time()) + 60}, keys["signing"], "ES256")
    assert not wi.looks_like_svid(idp)
    assert not wi.looks_like_svid("-".join(("static", "bearer", "token")))


def test_without_a_trust_domain_nothing_verifies(keys, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SPIFFE_TRUST_DOMAIN")
    assert not wi.enabled()
    with pytest.raises(wi.WorkloadIdentityError, match="no SPIFFE trust domain"):
        wi.verify(_svid(keys["signing"]), AUD)
