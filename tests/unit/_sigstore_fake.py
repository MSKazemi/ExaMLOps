"""A faithful, offline stand-in for the ``sigstore`` 4.x library (ADR 0013 keyless tests).

It reproduces exactly the API surface ``examlops.supplychain.keyless`` calls —
``ClientTrustConfig.production/staging(offline=)``, ``SigningContext.from_trust_config(...)
.signer(token, cache=)``, ``Signer.sign_artifact/sign_dsse``, ``Bundle.to_json/from_json``,
``Verifier(trusted_root=).verify_artifact/verify_dsse``, ``policy.Identity/AnyOf`` and
``oidc.IdentityToken/detect_credential`` — and it is strict where the real one is strict:

* the "Fulcio CA" is a secret held by the fake; a bundle whose certificate MAC does not verify
  under it is rejected, so an attacker-edited identity in a bundle fails;
* the signature covers the SHA-256 of the input, so any other input fails;
* the identity policy is checked against the bundle's certificate identity *and* issuer.

What it cannot model is the network (Fulcio, Rekor, TUF): ``SigningContext`` records how many
times it signed, and ``FAIL_SIGNING`` simulates an unreachable Fulcio.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.machinery
import json
import sys
import types
from contextlib import contextmanager
from typing import Any

_CA_SECRET = b"fake-fulcio-ca"
STATE: dict[str, Any] = {"signed": 0, "fail_signing": None, "ambient_token": None}


class VerificationError(Exception):
    pass


def _cert_mac(identity: str, issuer: str, digest: str) -> str:
    msg = f"{identity}\n{issuer}\n{digest}".encode()
    return hmac.new(_CA_SECRET, msg, hashlib.sha256).hexdigest()


def _token_claims(raw: str) -> dict[str, str]:
    # A fake OIDC token: base64(json({"sub":..., "iss":...})).
    return json.loads(base64.b64decode(raw))


def make_token(identity: str, issuer: str) -> str:
    return base64.b64encode(json.dumps({"sub": identity, "iss": issuer}).encode()).decode()


class IdentityToken:
    def __init__(self, raw_token: str, client_id: str = "sigstore") -> None:
        claims = _token_claims(raw_token)
        self._identity = claims["sub"]
        self._iss = claims["iss"]

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def issuer(self) -> str:
        return self._iss


def detect_credential(client_id: str = "sigstore") -> str | None:
    return STATE["ambient_token"]


class Bundle:
    def __init__(self, inner: dict[str, Any]) -> None:
        self._inner = inner

    def to_json(self) -> str:
        return json.dumps(self._inner, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Bundle:
        data = json.loads(raw)
        if not isinstance(data, dict) or "verificationMaterial" not in data:
            raise ValueError("not a Sigstore bundle")
        return cls(data)


class _TrustedRoot:
    pass


class ClientTrustConfig:
    def __init__(self, instance: str, offline: bool) -> None:
        self.instance = instance
        self.offline = offline
        self.trusted_root = _TrustedRoot()

    @classmethod
    def production(cls, offline: bool = False) -> ClientTrustConfig:
        return cls("production", offline)

    @classmethod
    def staging(cls, offline: bool = False) -> ClientTrustConfig:
        return cls("staging", offline)


class Statement:
    def __init__(self, contents: bytes) -> None:
        self._contents = contents


class Signer:
    def __init__(self, token: IdentityToken) -> None:
        self._token = token

    def _material(self, digest: str) -> dict[str, Any]:
        if STATE["fail_signing"]:
            raise ConnectionError(STATE["fail_signing"])
        STATE["signed"] += 1
        ident, iss = self._token.identity, self._token.issuer
        return {
            "certificate": {"identity": ident, "issuer": iss, "mac": _cert_mac(ident, iss, digest)},
            "tlogEntries": [{"logIndex": STATE["signed"], "kindVersion": {"kind": "fake"}}],
        }

    def sign_artifact(self, input_: bytes) -> Bundle:
        digest = hashlib.sha256(input_).hexdigest()
        return Bundle(
            {
                "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                "verificationMaterial": self._material(digest),
                "messageSignature": {"messageDigest": {"algorithm": "SHA2_256", "digest": digest}},
            }
        )

    def sign_dsse(self, input_: Statement) -> Bundle:
        body = input_._contents
        digest = hashlib.sha256(body).hexdigest()
        return Bundle(
            {
                "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                "verificationMaterial": self._material(digest),
                "dsseEnvelope": {
                    "payload": base64.b64encode(body).decode(),
                    "payloadType": "application/vnd.in-toto+json",
                    "signatures": [{"sig": digest}],
                },
            }
        )


class SigningContext:
    def __init__(self, cfg: ClientTrustConfig) -> None:
        self.cfg = cfg

    @classmethod
    def from_trust_config(cls, trust_config: ClientTrustConfig) -> SigningContext:
        return cls(trust_config)

    @contextmanager
    def signer(self, identity_token: IdentityToken, *, cache: bool = True):
        yield Signer(identity_token)


class Identity:
    def __init__(self, *, identity: str, issuer: str | None = None) -> None:
        self._identity = identity
        self._issuer = issuer

    def verify(self, cert: dict[str, Any]) -> None:
        if cert["identity"] != self._identity:
            raise VerificationError(f"identity mismatch: {cert['identity']}")
        if self._issuer and cert["issuer"] != self._issuer:
            raise VerificationError(f"issuer mismatch: {cert['issuer']}")


class AnyOf:
    def __init__(self, children: list[Identity]) -> None:
        self._children = children

    def verify(self, cert: dict[str, Any]) -> None:
        for child in self._children:
            try:
                child.verify(cert)
                return
            except VerificationError:
                continue
        raise VerificationError("no policy matched")


class Verifier:
    def __init__(self, *, trusted_root: _TrustedRoot) -> None:
        if not isinstance(trusted_root, _TrustedRoot):
            raise TypeError("trusted_root required")

    @staticmethod
    def _check_cert(bundle: Bundle, digest: str, rule: Any) -> None:
        cert = bundle._inner["verificationMaterial"]["certificate"]
        if not hmac.compare_digest(
            cert["mac"], _cert_mac(cert["identity"], cert["issuer"], digest)
        ):
            raise VerificationError("certificate/signature does not verify under the CA")
        rule.verify(cert)

    def verify_artifact(self, input_: bytes, bundle: Bundle, policy: Any) -> None:
        digest = hashlib.sha256(input_).hexdigest()
        recorded = bundle._inner.get("messageSignature", {}).get("messageDigest", {})
        if recorded.get("digest") != digest:
            raise VerificationError("artifact digest mismatch")
        self._check_cert(bundle, digest, policy)

    def verify_dsse(self, bundle: Bundle, policy: Any) -> tuple[str, bytes]:
        env = bundle._inner.get("dsseEnvelope")
        if env is None:
            raise VerificationError("no DSSE envelope")
        body = base64.b64decode(env["payload"])
        digest = hashlib.sha256(body).hexdigest()
        self._check_cert(bundle, digest, policy)
        return env["payloadType"], body


def install(monkeypatch) -> dict[str, Any]:
    """Register the fake as ``sigstore`` (and its submodules) for one test."""
    STATE.update({"signed": 0, "fail_signing": None, "ambient_token": None})
    root = types.ModuleType("sigstore")
    root.__spec__ = importlib.machinery.ModuleSpec("sigstore", None, is_package=True)
    root.__path__ = []  # type: ignore[attr-defined]
    mods: dict[str, types.ModuleType] = {"sigstore": root}

    def sub(name: str, **attrs: Any) -> None:
        mod = types.ModuleType(f"sigstore.{name}")
        mod.__spec__ = importlib.machinery.ModuleSpec(f"sigstore.{name}", None)
        for key, value in attrs.items():
            setattr(mod, key, value)
        setattr(root, name.split(".")[0], mod)
        mods[f"sigstore.{name}"] = mod

    sub("models", Bundle=Bundle, ClientTrustConfig=ClientTrustConfig)
    sub("oidc", IdentityToken=IdentityToken, detect_credential=detect_credential)
    sub("sign", SigningContext=SigningContext)
    sub("dsse", Statement=Statement)
    sub("errors", VerificationError=VerificationError)
    policy_mod = types.ModuleType("sigstore.verify.policy")
    policy_mod.Identity = Identity  # type: ignore[attr-defined]
    policy_mod.AnyOf = AnyOf  # type: ignore[attr-defined]
    sub("verify", Verifier=Verifier, policy=policy_mod)
    mods["sigstore.verify.policy"] = policy_mod
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return STATE
