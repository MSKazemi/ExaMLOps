"""Sigstore keyless model signing (ADR 0013 clause 1), against a faithful offline fake.

The real library needs Fulcio, Rekor and a TUF root — none reachable from a unit test — so
``tests/unit/_sigstore_fake.py`` stands in with the same API and the same strictness (a CA-bound
certificate identity, a signature over the exact input, an identity+issuer policy). What these
tests pin is *our* side: which scheme is chosen, what is stored, that trust is by identity and
fails closed, and that the offline-HPC fallback to the Ed25519 key is audited, never silent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from examlops import supplychain
from examlops.data.registry import get_model_signature
from examlops.supplychain import keyless
from tests.unit import _sigstore_fake

CI = "https://github.com/MSKazemi/ExaMLOps/.github/workflows/train.yml@refs/heads/main"
GH = "https://token.actions.githubusercontent.com"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for var in (
        "EXAMLOPS_SIGNING_KEY",
        "EXAMLOPS_SIGNING_PRIVATE_KEY_FILE",
        "EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE",
        "EXAMLOPS_SIGNING_PUBLIC_KEYS",
        "EXAMLOPS_SIGSTORE_IDENTITY_TOKEN",
        "EXAMLOPS_SIGSTORE_IDENTITY_TOKEN_FILE",
        "EXAMLOPS_SIGSTORE_INSTANCE",
        "EXAMLOPS_SIGSTORE_OFFLINE",
    ):
        monkeypatch.delenv(var, raising=False)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        supplychain, "_audit", lambda a, m, v, actor, extra: events.append((a, extra))
    )
    state = _sigstore_fake.install(monkeypatch)
    monkeypatch.setenv("EXAMLOPS_SIGNING_SCHEME", "sigstore")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN", _sigstore_fake.make_token(CI, GH))
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITIES", f"{CI}|{GH}")
    return tmp_path, events, state


def _bundle(root: Path) -> list[Path]:
    (root / "model").mkdir(parents=True)
    (root / "MLmodel").write_text("flavors: {}\n")
    (root / "model" / "model.pkl").write_bytes(b"\x80\x04weights")
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_keyless_signature_is_stored_and_verifies_by_identity(env):
    tmp, events, state = env
    paths = _bundle(tmp / "art")
    sig = supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    assert sig.algo == keyless.SIGSTORE and state["signed"] == 1
    row = get_model_signature("jpcp", "7")
    assert row["algo"] == "sigstore-v1"
    assert json.loads(row["cert"]) == {"identity": CI, "issuer": GH}
    assert "verificationMaterial" in json.loads(row["signature"])  # a Sigstore bundle
    assert events[-1][0] == "model_signed"
    assert supplychain.verify_model("JPCP", "7", paths, root=tmp / "art").reason == "verified"


def test_signing_configured_reports_sigstore(env):
    assert supplychain.signing_configured() == "sigstore-v1"


def test_a_signer_outside_the_trusted_identities_is_refused(env, monkeypatch):
    tmp, _, _ = env
    paths = _bundle(tmp / "art")
    supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITIES", f"release@example.org|{GH}")
    result = supplychain.verify_model("JPCP", "7", paths, root=tmp / "art")
    assert not result.ok and result.reason.startswith("bad-signature")


def test_the_same_identity_from_another_issuer_is_refused(env, monkeypatch):
    tmp, _, _ = env
    paths = _bundle(tmp / "art")
    supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITIES", f"{CI}|https://evil.example")
    assert not supplychain.verify_model("JPCP", "7", paths, root=tmp / "art").ok


def test_no_trusted_identity_fails_closed(env, monkeypatch):
    tmp, _, _ = env
    paths = _bundle(tmp / "art")
    supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITIES", f"{CI}")  # no issuer: dropped, not widened
    assert keyless.trusted_identities() == []
    result = supplychain.verify_model("JPCP", "7", paths, root=tmp / "art")
    assert result.reason.startswith("untrusted-identity")


def test_tampered_bytes_are_caught_before_the_signature(env):
    tmp, _, _ = env
    paths = _bundle(tmp / "art")
    supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    (tmp / "art" / "model" / "model.pkl").write_bytes(b"evil")
    assert supplychain.verify_model("JPCP", "7", paths, root=tmp / "art").reason.startswith(
        "tampered"
    )


def test_a_bundle_cannot_be_moved_to_another_version(env):
    tmp, _, _ = env
    paths = _bundle(tmp / "art")
    supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    row = get_model_signature("jpcp", "7")
    result = supplychain.verify_record(row, "jpcp", "8", paths, root=tmp / "art")
    assert not result.ok and result.reason.startswith("bad-signature")


def test_an_edited_identity_in_the_bundle_does_not_verify(env):
    tmp, _, _ = env
    paths = _bundle(tmp / "art")
    supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    row = dict(get_model_signature("jpcp", "7"))
    bundle = json.loads(row["signature"])
    bundle["verificationMaterial"]["certificate"]["identity"] = "attacker@example.org"
    row["signature"] = json.dumps(bundle)
    assert not supplychain.verify_record(row, "jpcp", "7", paths, root=tmp / "art").ok


def test_missing_library_is_a_failure_to_verify_and_enforce_refuses(env, monkeypatch):
    tmp, events, _ = env
    paths = _bundle(tmp / "art")
    supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    monkeypatch.setattr(keyless, "available", lambda: False)
    result = supplychain.verify_model("JPCP", "7", paths, root=tmp / "art")
    assert result.reason.startswith("unavailable")
    assert (
        supplychain.verify_before_load("JPCP", "7", paths, mode="enforce", root=tmp / "art")
        is False
    )
    assert events[-1][0] == "model_verify_failed"


def test_unreachable_fulcio_falls_back_to_the_ed25519_key_and_is_audited(env, monkeypatch):
    tmp, events, state = env
    key = tmp / "k.pem"
    key.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(key))
    state["fail_signing"] = "fulcio.sigstore.dev unreachable"
    paths = _bundle(tmp / "art")
    sig = supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    assert sig.algo == "ed25519-v2"
    assert [a for a, _ in events] == ["model_sign_keyless_fallback", "model_signed"]
    assert "unreachable" in events[0][1]["reason"]


def test_no_token_and_no_fallback_key_refuses_to_sign(env, monkeypatch):
    tmp, _, _ = env
    monkeypatch.delenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN")
    paths = _bundle(tmp / "art")
    with pytest.raises(supplychain.SigningKeyMissing, match="no OIDC identity token"):
        supplychain.sign_model("JPCP", "7", paths, root=tmp / "art")
    assert get_model_signature("jpcp", "7") is None


def test_ambient_ci_credential_is_used_when_no_token_is_set(env, monkeypatch):
    tmp, _, state = env
    monkeypatch.delenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN")
    state["ambient_token"] = _sigstore_fake.make_token(CI, GH)
    paths = _bundle(tmp / "art")
    assert supplychain.sign_model("JPCP", "7", paths, root=tmp / "art").algo == "sigstore-v1"


def test_token_file_is_read(env, monkeypatch):
    tmp, _, _ = env
    monkeypatch.delenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN")
    token = tmp / "oidc.jwt"
    token.write_text(_sigstore_fake.make_token(CI, GH) + "\n")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN_FILE", str(token))
    paths = _bundle(tmp / "art")
    assert supplychain.sign_model("JPCP", "7", paths, root=tmp / "art").algo == "sigstore-v1"


def test_unknown_scheme_is_refused_not_downgraded(env, monkeypatch):
    tmp, _, _ = env
    monkeypatch.setenv("EXAMLOPS_SIGNING_SCHEME", "sigstor")
    with pytest.raises(supplychain.SigningKeyMissing, match="unknown EXAMLOPS_SIGNING_SCHEME"):
        supplychain.sign_model("JPCP", "7", _bundle(tmp / "art"), root=tmp / "art")
    assert supplychain.signing_configured() is None


def test_unknown_instance_is_refused(env, monkeypatch):
    tmp, _, _ = env
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_INSTANCE", "prod")
    with pytest.raises(supplychain.SigningKeyMissing):
        supplychain.sign_model("JPCP", "7", _bundle(tmp / "art"), root=tmp / "art")


def test_auto_scheme_is_unchanged(env, monkeypatch):
    tmp, _, state = env
    monkeypatch.setenv("EXAMLOPS_SIGNING_SCHEME", "auto")
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "legacy-secret")
    sig = supplychain.sign_model("JPCP", "7", _bundle(tmp / "art"), root=tmp / "art")
    assert sig.algo == "hmac-sha256" and state["signed"] == 0
