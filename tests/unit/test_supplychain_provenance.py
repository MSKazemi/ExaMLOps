"""SLSA v1 build provenance for model versions (ADR 0013 clause 3).

Pins the in-toto/SLSA shape, the DSSE signature (PAE, trusted keys, tamper evidence), the
audit-trail anchor, idempotency and the fail-closed verdicts.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from examlops import supplychain
from examlops.platform_db import get_db
from examlops.supplychain import provenance as prov
from tests.unit import _sigstore_fake

DIGEST = "sha256:" + "ab" * 32
CTX = prov.BuildContext(
    run_id="run-42",
    dataset="FData",
    dataset_revision="rev-abc",
    backend="minio",
    framework="sklearn",
    job_id="1234",
    scheduler="flux",
    source_repository="https://github.com/MSKazemi/ExaMLOps",
    source_commit="0" * 40,
    builder_id="urn:examlops:builder:test",
    parameters={"epochs": 3},
)


def _keypair(tmp: Path, name: str) -> tuple[Path, Path]:
    key = Ed25519PrivateKey.generate()
    private, public = tmp / f"{name}.pem", tmp / f"{name}.pub.pem"
    private.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    public.write_bytes(
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    return private, public


@pytest.fixture()
def env(tmp_path, monkeypatch):
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
    return tmp_path


def _sign_with(monkeypatch, private: Path) -> None:
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(private))


def _verify_with(monkeypatch, public: Path) -> None:
    monkeypatch.delenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.setenv("EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE", str(public))


def _audit(action: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action=? ORDER BY id", (action,)
        ).fetchall()
    return [dict(r) for r in rows]


def test_pae_matches_the_dsse_spec_vector():
    # DSSE v1 protocol.md test vector.
    assert prov.pae("http://example.com/HelloWorld", b"hello world") == (
        b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
    )


def test_statement_is_slsa_v1_in_toto():
    st = prov.build_statement("JPCP", "17", DIGEST, CTX)
    assert st["_type"] == "https://in-toto.io/Statement/v1"
    assert st["predicateType"] == "https://slsa.dev/provenance/v1"
    assert st["subject"] == [{"name": "models:/jpcp/17", "digest": {"sha256": "ab" * 32}}]
    bd = st["predicate"]["buildDefinition"]
    assert bd["externalParameters"]["datasetRevision"] == "rev-abc"
    assert bd["externalParameters"]["parameters"] == {"epochs": "3"}
    deps = {d["uri"]: d for d in bd["resolvedDependencies"]}
    assert deps["dataset:FData"]["annotations"]["revision"] == "rev-abc"
    assert deps["git+https://github.com/MSKazemi/ExaMLOps"]["digest"] == {"gitCommit": "0" * 40}
    rd = st["predicate"]["runDetails"]
    assert rd["builder"]["id"] == "urn:examlops:builder:test"
    assert rd["metadata"]["invocationId"] == "run-42"
    assert rd["byproducts"][0]["annotations"] == {"jobId": "1234", "scheduler": "flux"}


@pytest.mark.parametrize("bad", ["ab" * 32, "sha256:xyz", "md5:" + "a" * 64, "sha256:" + "g" * 64])
def test_a_malformed_subject_digest_is_rejected(bad):
    with pytest.raises(ValueError):
        prov.build_statement("JPCP", "1", bad, CTX)


def test_parameters_are_bounded():
    many = {f"p{i:03d}": "x" * 5000 for i in range(200)}
    st = prov.build_statement("m", "1", DIGEST, prov.BuildContext(parameters=many))
    params = st["predicate"]["buildDefinition"]["externalParameters"]["parameters"]
    assert len(params) == 64 and all(len(v) == 1024 for v in params.values())


def test_context_from_ci_env(monkeypatch, env):
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)
    monkeypatch.setenv("EXAMLOPS_SOURCE_REPOSITORY", "https://github.com/MSKazemi/ExaMLOps")
    ctx = prov.BuildContext.from_env(dataset="FData", run_id=None)
    assert ctx.source_commit == "f" * 40
    assert ctx.source_repository == "https://github.com/MSKazemi/ExaMLOps"
    assert ctx.dataset == "FData" and ctx.run_id is None


def test_signed_provenance_verifies_with_only_the_public_key(env, monkeypatch):
    private, public = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    rec = prov.record_provenance("JPCP", "17", DIGEST, CTX, actor="tester")
    assert rec.algo == "ed25519-dsse" and rec.envelope["payloadType"] == prov.PAYLOAD_TYPE
    _verify_with(monkeypatch, public)
    verdict = prov.verify_provenance("jpcp", "17", expected_digest=DIGEST)
    assert verdict.ok and verdict.reason == "verified"
    assert verdict.statement["subject"][0]["name"] == "models:/jpcp/17"


def test_provenance_is_anchored_in_the_audit_chain(env, monkeypatch):
    private, _ = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    prov.record_provenance("JPCP", "17", DIGEST, CTX, actor="tester")
    events = _audit("model_provenance_recorded")
    assert len(events) == 1 and events[0]["target"] == "JPCP@17"
    details = json.loads(events[0]["details"])
    with get_db() as conn:
        stored = conn.execute("SELECT envelope_json FROM model_provenance").fetchone()[0]
    assert details["envelope_sha256"] == hashlib.sha256(stored.encode()).hexdigest()
    assert details["algo"] == "ed25519-dsse" and details["replaced"] is False


def test_unsigned_provenance_is_recorded_but_never_verified(env):
    rec = prov.record_provenance("JPCP", "17", DIGEST, CTX)
    assert rec.algo == "none" and rec.envelope["signatures"] == []
    assert prov.verify_provenance("JPCP", "17").reason.startswith("unsigned")


def test_missing_provenance(env):
    verdict = prov.verify_provenance("JPCP", "99")
    assert not verdict.ok and verdict.reason.startswith("missing")


def test_an_edited_payload_breaks_the_signature(env, monkeypatch):
    private, public = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    rec = prov.record_provenance("JPCP", "17", DIGEST, CTX)
    st = json.loads(base64.b64decode(rec.envelope["payload"]))
    st["predicate"]["buildDefinition"]["externalParameters"]["datasetRevision"] = "forged"
    env_ = dict(rec.envelope, payload=base64.b64encode(prov.canonical(st)).decode())
    with get_db() as conn:
        conn.execute("UPDATE model_provenance SET envelope_json=?", (json.dumps(env_),))
    _verify_with(monkeypatch, public)
    assert prov.verify_provenance("JPCP", "17").reason == "bad-signature"


def test_a_rewritten_row_digest_is_caught(env, monkeypatch):
    private, public = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    prov.record_provenance("JPCP", "17", DIGEST, CTX)
    with get_db() as conn:
        conn.execute("UPDATE model_provenance SET subject_digest=?", ("sha256:" + "cd" * 32,))
    _verify_with(monkeypatch, public)
    assert prov.verify_provenance("JPCP", "17").reason.startswith("subject-mismatch")


def test_provenance_for_other_bytes_is_refused(env, monkeypatch):
    private, public = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    prov.record_provenance("JPCP", "17", DIGEST, CTX)
    _verify_with(monkeypatch, public)
    verdict = prov.verify_provenance("JPCP", "17", expected_digest="sha256:" + "cd" * 32)
    assert not verdict.ok and "different artifact bytes" in verdict.reason


def test_provenance_moved_to_another_version_is_caught(env, monkeypatch):
    private, public = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    prov.record_provenance("JPCP", "17", DIGEST, CTX)
    with get_db() as conn:
        conn.execute("UPDATE model_provenance SET version='18'")
    _verify_with(monkeypatch, public)
    assert prov.verify_provenance("JPCP", "18").reason.startswith("subject-mismatch")


def test_a_key_outside_the_trust_bundle_is_untrusted(env, monkeypatch):
    private, _ = _keypair(env, "k")
    _, other_public = _keypair(env, "other")
    _sign_with(monkeypatch, private)
    prov.record_provenance("JPCP", "17", DIGEST, CTX)
    _verify_with(monkeypatch, other_public)
    assert prov.verify_provenance("JPCP", "17").reason.startswith("untrusted-key")


def test_recording_is_idempotent_and_overwriting_is_deliberate(env, monkeypatch):
    private, _ = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    first = prov.record_provenance("JPCP", "17", DIGEST, CTX)
    again = prov.record_provenance("JPCP", "17", DIGEST, CTX)  # same build → no-op
    assert again.statement_sha256 == first.statement_sha256
    assert len(_audit("model_provenance_recorded")) == 1
    other = prov.BuildContext(run_id="run-43", dataset="FData")
    with pytest.raises(prov.ProvenanceExists):
        prov.record_provenance("JPCP", "17", DIGEST, other)
    replaced = prov.record_provenance("JPCP", "17", DIGEST, other, replace=True)
    assert replaced.statement_sha256 != first.statement_sha256
    events = _audit("model_provenance_recorded")
    assert len(events) == 2 and json.loads(events[1]["details"])["replaced"] is True


def test_model_name_is_case_insensitive(env, monkeypatch):
    private, _ = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    prov.record_provenance("JPCP", "17", DIGEST, CTX)
    row = prov.get_provenance("jpcp", "17")
    assert row is not None and row["statement"]["subject"][0]["digest"]["sha256"] == "ab" * 32


def test_keyless_provenance_is_a_verified_sigstore_bundle(env, monkeypatch):
    _sigstore_fake.install(monkeypatch)
    ci, gh = "ci@example.org", "https://issuer.example"
    monkeypatch.setenv("EXAMLOPS_SIGNING_SCHEME", "sigstore")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN", _sigstore_fake.make_token(ci, gh))
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITIES", f"{ci}|{gh}")
    rec = prov.record_provenance("JPCP", "17", DIGEST, CTX)
    assert rec.algo == "sigstore-v1" and "sigstoreBundle" in rec.envelope
    assert prov.verify_provenance("JPCP", "17", expected_digest=DIGEST).ok
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITIES", f"someone-else@example.org|{gh}")
    assert prov.verify_provenance("JPCP", "17").reason.startswith("bad-signature")


def test_keyless_failure_falls_back_to_ed25519(env, monkeypatch):
    state = _sigstore_fake.install(monkeypatch)
    state["fail_signing"] = "rekor down"
    private, public = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    monkeypatch.setenv("EXAMLOPS_SIGNING_SCHEME", "sigstore")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN", _sigstore_fake.make_token("a", "b"))
    rec = prov.record_provenance("JPCP", "17", DIGEST, CTX)
    assert rec.algo == "ed25519-dsse"
    _verify_with(monkeypatch, public)
    assert prov.verify_provenance("JPCP", "17").ok


def test_ed25519_helpers_round_trip(env, monkeypatch):
    private, public = _keypair(env, "k")
    _sign_with(monkeypatch, private)
    sig, kid = supplychain.ed25519_sign_bytes(b"data")
    _verify_with(monkeypatch, public)
    assert supplychain.ed25519_verify_bytes(b"data", sig, kid) == "verified"
    assert supplychain.ed25519_verify_bytes(b"other", sig, kid) == "bad-signature"
    assert supplychain.ed25519_sign_bytes(b"x") is None  # verifier holds no private key
