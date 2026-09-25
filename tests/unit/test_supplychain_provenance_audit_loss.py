"""ADR 0013 clause 3 — lost provenance audit events are counted, and the provenance stands.

Two sites in `examlops.supplychain.provenance`: `record_provenance` anchors every stored envelope
with a `model_provenance_recorded` event, and `_envelope` records
`model_provenance_keyless_fallback` when keyless signing fails and the Ed25519 key is used
instead. An audit outage must not stop the provenance being stored, signed and verifiable
(fail open), and each loss must reach `dropped_audit_events()` — a provenance row with no anchor
event is exactly what the chain cannot see. Registered in
`tests/unit/test_audit_losses_are_recorded.py::COVERED_AUDIT_SITES`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))

from examlops.supplychain import provenance as prov  # noqa: E402
from tests.unit import _sigstore_fake  # noqa: E402

DIGEST = "sha256:" + "ab" * 32
CTX = prov.BuildContext(
    run_id="run-42",
    dataset="FData",
    dataset_revision="rev-abc",
    backend="minio",
    framework="sklearn",
    builder_id="urn:examlops:builder:test",
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in (
        "EXAMLOPS_SIGNING_KEY",
        "EXAMLOPS_SIGNING_PRIVATE_KEY_FILE",
        "EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE",
        "EXAMLOPS_SIGNING_PUBLIC_KEYS",
        "EXAMLOPS_SIGNING_SCHEME",
        "GITHUB_SHA",
        "EXAMLOPS_SOURCE_REPOSITORY",
        "CI_COMMIT_SHA",
        "EXAMLOPS_SOURCE_COMMIT",
        "EXAMLOPS_BUILDER_ID",
        "EXAMLOPS_DATASET_REVISION",
    ):
        monkeypatch.delenv(var, raising=False)
    from examlops import platform_db
    from examlops.data.audit import reset_dropped_audit_events

    platform_db.init_db()
    reset_dropped_audit_events()
    yield
    reset_dropped_audit_events()


def _break_the_audit_log(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def _keypair(tmp: Path) -> tuple[Path, Path]:
    key = Ed25519PrivateKey.generate()
    private, public = tmp / "k.pem", tmp / "k.pub.pem"
    private.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    public.write_bytes(
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    return private, public


def _verify_with(monkeypatch, public: Path) -> None:
    monkeypatch.delenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.setenv("EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE", str(public))


def test_a_lost_provenance_anchor_audit_is_counted_and_the_provenance_still_verifies(
    tmp_path, monkeypatch
):
    from examlops.data.audit import dropped_audit_events

    private, public = _keypair(tmp_path)
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(private))
    _break_the_audit_log(monkeypatch)

    rec = prov.record_provenance("JPCP", "17", DIGEST, CTX, actor="tester")

    assert rec.algo == prov.ED25519_DSSE
    _verify_with(monkeypatch, public)
    assert prov.verify_provenance("JPCP", "17", expected_digest=DIGEST).ok
    assert dropped_audit_events().get("model_provenance_recorded") == 1, dropped_audit_events()


def test_a_lost_keyless_fallback_audit_is_counted_and_the_ed25519_fallback_still_signs(
    tmp_path, monkeypatch
):
    from examlops.data.audit import dropped_audit_events

    state = _sigstore_fake.install(monkeypatch)
    state["fail_signing"] = "rekor down"
    private, public = _keypair(tmp_path)
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(private))
    monkeypatch.setenv("EXAMLOPS_SIGNING_SCHEME", "sigstore")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN", _sigstore_fake.make_token("a", "b"))
    _break_the_audit_log(monkeypatch)

    rec = prov.record_provenance("JPCP", "17", DIGEST, CTX)

    assert rec.algo == prov.ED25519_DSSE  # the documented fallback, not a failure
    _verify_with(monkeypatch, public)
    assert prov.verify_provenance("JPCP", "17").ok
    dropped = dropped_audit_events()
    assert dropped.get("model_provenance_keyless_fallback") == 1, dropped
    assert dropped.get("model_provenance_recorded") == 1, dropped
