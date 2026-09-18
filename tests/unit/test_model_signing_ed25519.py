"""Models are signed with Ed25519 at registration and verified with a public key (plan P4.10).

The legacy signature was an HMAC: every serving replica that verified held the signing key, so any
of them could also forge one. It covered a digest of file *names* and bytes run together, so
``a``+``bc`` and ``ab``+``c`` collided and moving a file inside the bundle went unnoticed. And it
signed the digest alone, so a signature on version 3 was equally valid on version 4 if 4 pointed
at 3's bytes — a rollback no check would catch. These tests pin each of those, plus rotation,
the legacy rows that must still verify, and signing at registration.
"""

from __future__ import annotations

import base64
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

from examlops import supplychain
from examlops.data.registry import get_model_signature, store_model_signature


def _write_keypair(directory: Path, name: str) -> tuple[Path, Path, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    private = directory / f"{name}.pem"
    public = directory / f"{name}.pub.pem"
    private.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    public.write_bytes(
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    return private, public, key


@pytest.fixture()
def keys(tmp_path, monkeypatch):
    for var in (
        "EXAMLOPS_SIGNING_KEY",
        "EXAMLOPS_SIGNING_PRIVATE_KEY_FILE",
        "EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(supplychain, "_audit", lambda *a, **k: None)
    return tmp_path


def _bundle(root: Path) -> list[Path]:
    (root / "model").mkdir(parents=True)
    (root / "MLmodel").write_text("flavors: {}\n")
    (root / "model" / "model.pkl").write_bytes(b"\x80\x04weights")
    return sorted(p for p in root.rglob("*") if p.is_file())


def _signer(monkeypatch, private: Path) -> None:
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(private))


def _verifier(monkeypatch, public_bundle: Path) -> None:
    """A serving replica: the public trust bundle and nothing else."""
    monkeypatch.delenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY", raising=False)
    monkeypatch.setenv("EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE", str(public_bundle))


def test_a_replica_with_only_the_public_key_verifies_and_cannot_sign(keys, monkeypatch):
    private, public, key = _write_keypair(keys, "k1")
    root = keys / "art"
    paths = _bundle(root)

    _signer(monkeypatch, private)
    sig = supplychain.sign_model("JPCP", "17", paths, root=root)
    assert sig.algo == "ed25519-v2" and sig.digest.startswith("sha256:")
    row = get_model_signature("jpcp", "17")
    assert row["cert"] == supplychain.key_id(key.public_key())

    _verifier(monkeypatch, public)
    assert supplychain.verify_model("jpcp", "17", paths, root=root).reason == "verified"
    assert supplychain.signing_configured() is None
    with pytest.raises(supplychain.SigningKeyMissing):
        supplychain.sign_model("JPCP", "18", paths, root=root)


def test_changed_bytes_are_tampering(keys, monkeypatch):
    private, public, _ = _write_keypair(keys, "k1")
    root = keys / "art"
    paths = _bundle(root)
    _signer(monkeypatch, private)
    supplychain.sign_model("JPCP", "1", paths, root=root)
    (root / "model" / "model.pkl").write_bytes(b"\x80\x04evil")
    _verifier(monkeypatch, public)
    assert supplychain.verify_model("JPCP", "1", paths, root=root).reason.startswith("tampered")


def test_moving_a_file_inside_the_bundle_is_tampering(keys, monkeypatch):
    private, public, _ = _write_keypair(keys, "k1")
    root = keys / "art"
    paths = _bundle(root)
    _signer(monkeypatch, private)
    supplychain.sign_model("JPCP", "1", paths, root=root)
    moved = root / "model.pkl"
    (root / "model" / "model.pkl").rename(moved)
    now = sorted(p for p in root.rglob("*") if p.is_file())
    _verifier(monkeypatch, public)
    assert supplychain.verify_model("JPCP", "1", now, root=root).reason.startswith("tampered")


def test_a_signature_cannot_be_moved_to_another_version(keys, monkeypatch):
    """The rollback: version 4 points at version 3's bytes and borrows its signature row."""
    private, public, _ = _write_keypair(keys, "k1")
    root = keys / "art"
    paths = _bundle(root)
    _signer(monkeypatch, private)
    supplychain.sign_model("JPCP", "3", paths, root=root)
    row = get_model_signature("JPCP", "3")
    store_model_signature(
        "JPCP", "4", row["digest"], row["signature"], algo=row["algo"], cert=row["cert"]
    )
    _verifier(monkeypatch, public)
    assert supplychain.verify_model("JPCP", "4", paths, root=root).reason == "bad-signature"
    assert supplychain.verify_model("JPCP", "3", paths, root=root).ok


def test_a_key_outside_the_trust_bundle_is_refused(keys, monkeypatch):
    private, _, _ = _write_keypair(keys, "rogue")
    _, trusted_public, _ = _write_keypair(keys, "trusted")
    root = keys / "art"
    paths = _bundle(root)
    _signer(monkeypatch, private)
    supplychain.sign_model("JPCP", "1", paths, root=root)
    _verifier(monkeypatch, trusted_public)
    assert supplychain.verify_model("JPCP", "1", paths, root=root).reason.startswith(
        "untrusted-key"
    )


def test_rotation_keeps_versions_signed_by_the_retiring_key_valid(keys, monkeypatch):
    old_private, old_public, _ = _write_keypair(keys, "old")
    new_private, new_public, _ = _write_keypair(keys, "new")
    root = keys / "art"
    paths = _bundle(root)
    _signer(monkeypatch, old_private)
    supplychain.sign_model("JPCP", "1", paths, root=root)
    _signer(monkeypatch, new_private)
    supplychain.sign_model("JPCP", "2", paths, root=root)

    bundle = keys / "trust.pem"
    bundle.write_bytes(old_public.read_bytes() + new_public.read_bytes())
    _verifier(monkeypatch, bundle)
    assert supplychain.verify_model("JPCP", "1", paths, root=root).ok
    assert supplychain.verify_model("JPCP", "2", paths, root=root).ok


def test_the_manifest_digest_does_not_collide_where_the_legacy_digest_did(keys):
    one, two = keys / "one", keys / "two"
    one.mkdir()
    two.mkdir()
    (one / "a").write_bytes(b"bc")
    (two / "ab").write_bytes(b"c")
    first, second = [one / "a"], [two / "ab"]
    assert supplychain.artifact_digest(first) == supplychain.artifact_digest(second)  # why v2
    assert supplychain.manifest_digest(first, root=one) != supplychain.manifest_digest(
        second, root=two
    )


def test_legacy_hmac_rows_still_verify(keys, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "-".join(("legacy", "hmac", "test", "value")))
    root = keys / "art"
    paths = _bundle(root)
    sig = supplychain.sign_model("JPCP", "1", paths)
    assert sig.algo == "hmac-sha256"
    assert supplychain.verify_model("JPCP", "1", paths).reason == "verified"


def test_an_unknown_algorithm_is_refused():
    record = {"algo": "md5-v0", "digest": "x", "signature": "y"}
    assert supplychain.verify_record(record, "JPCP", "1", []).reason.startswith("unknown")


def test_a_snapshot_carried_record_verifies_without_the_datastore(keys, monkeypatch):
    private, public, key = _write_keypair(keys, "k1")
    root = keys / "art"
    paths = _bundle(root)
    digest = supplychain.manifest_digest(paths, root=root)
    record = {
        "algo": "ed25519-v2",
        "digest": digest,
        "cert": supplychain.key_id(key.public_key()),
        "signature": base64.b64encode(
            key.sign(supplychain.statement("JPCP", "9", digest))
        ).decode(),
    }
    _verifier(monkeypatch, public)
    assert get_model_signature("JPCP", "9") is None
    assert supplychain.verify_before_load(
        "JPCP", "9", paths, mode="enforce", root=root, record=record
    )
    record["signature"] = base64.b64encode(b"\0" * 64).decode()
    assert not supplychain.verify_before_load(
        "JPCP", "9", paths, mode="enforce", root=root, record=record
    )


# ─── signing at registration ─────────────────────────────────────────────────


@pytest.fixture()
def generator(monkeypatch):
    repo = Path(__file__).resolve().parents[2]
    for p in (str(repo), str(repo / "pipelines")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import pipelines.pipeline_generator as gen

    return gen


def test_registration_signs_the_registered_bytes(keys, monkeypatch, generator):
    private, public, _ = _write_keypair(keys, "k1")
    _signer(monkeypatch, private)
    root = keys / "registry-copy"
    paths = _bundle(root)
    monkeypatch.setattr(supplychain, "registered_artifacts", lambda m, v, dst: root)
    monkeypatch.delenv("EXAMLOPS_SIGN_AT_REGISTRATION", raising=False)

    generator._sign_registered("jpcp", "21")

    _verifier(monkeypatch, public)
    assert supplychain.verify_model("jpcp", "21", paths, root=root).reason == "verified"


def test_required_signing_fails_the_run_without_a_key(keys, monkeypatch, generator):
    monkeypatch.setenv("EXAMLOPS_SIGN_AT_REGISTRATION", "required")
    with pytest.raises(RuntimeError, match="no signing key"):
        generator._sign_registered("jpcp", "21")


def test_auto_signing_without_a_key_leaves_the_run_alone(keys, monkeypatch, generator):
    monkeypatch.setenv("EXAMLOPS_SIGN_AT_REGISTRATION", "auto")
    generator._sign_registered("jpcp", "21")  # no key, no error
    assert get_model_signature("jpcp", "21") is None


# ─── the serving snapshot carries each version's signature ───────────────────


class _Mlflow:
    """Two registered models; m000 has Production v1 and Canary v2."""

    class _R:
        def __init__(self, body):
            self.body, self.status_code = body, 200

        def json(self):
            return self.body

        def raise_for_status(self):
            return None

    def get(self, url, params=None):
        params = params or {}
        if url.endswith("registered-models/search"):
            return self._R(
                {
                    "registered_models": [
                        {
                            "name": "m000",
                            "aliases": [
                                {"alias": "Production", "version": "1"},
                                {"alias": "Canary", "version": "2"},
                            ],
                        },
                        {"name": "m001", "aliases": [{"alias": "Production", "version": "1"}]},
                    ]
                }
            )
        return self._R({"model_version": {"version": params["version"], "tags": []}})


def test_the_snapshot_carries_signatures_and_a_new_signature_is_a_new_generation(keys):
    from examlops import serving_snapshot as snap

    snap._version_cache.clear()
    unsigned = snap.compile_snapshot(client=_Mlflow(), mlflow_url="http://mlflow")
    assert unsigned["models"]["m000"]["aliases"]["Production"]["signature"] is None

    store_model_signature("M000", "1", "sha256:d", "c2ln", algo="ed25519-v2", cert="kid-1")
    signed = snap.compile_snapshot(client=_Mlflow(), mlflow_url="http://mlflow")
    record = signed["models"]["m000"]["aliases"]["Production"]["signature"]
    assert record == {
        "algo": "ed25519-v2",
        "digest": "sha256:d",
        "signature": "c2ln",
        "cert": "kid-1",
    }
    assert signed["models"]["m000"]["aliases"]["Canary"]["signature"] is None  # v2 unsigned
    assert signed["digest"] != unsigned["digest"]  # replicas see the change as a new generation


def test_a_replica_verifies_a_snapshot_version_with_the_snapshots_record(tmp_path, monkeypatch):
    """The record travels snapshot → _StubMV → _verified_uri → the verifier."""
    repo = Path(__file__).resolve().parents[2]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from serving.ray_serving import app as rs_app

    record = {"algo": "ed25519-v2", "digest": "sha256:d", "signature": "s", "cert": "k"}
    (tmp_path / "model.pkl").write_bytes(b"x")
    seen: dict = {}

    def _verify(name, version, paths, *, mode, root=None, record=None):
        seen.update(name=name, version=version, record=record, root=root)
        return False  # enforce: refused, so nothing is loaded

    monkeypatch.setattr(rs_app, "_VERIFY_MODE", "enforce")
    monkeypatch.setattr(rs_app, "_ARTIFACT_CACHE", None)
    monkeypatch.setattr(rs_app.mlflow.artifacts, "download_artifacts", lambda **_k: str(tmp_path))
    monkeypatch.setattr("examlops.supplychain.verify_before_load", _verify)

    server = object.__new__(rs_app.MultiModelServer.func_or_class)
    mv = rs_app._StubMV("7", {"framework": "sklearn"}, signature=record)
    with pytest.raises(RuntimeError, match="refusing to load"):
        server._load_by_flavour("jpcp", None, mv)
    assert seen == {"name": "jpcp", "version": "7", "record": record, "root": tmp_path}


def test_applying_a_snapshot_hands_each_version_its_record(monkeypatch):
    import threading
    from collections import OrderedDict
    from unittest.mock import MagicMock

    from serving.ray_serving import app as rs_app

    record = {"algo": "ed25519-v2", "digest": "sha256:d", "signature": "s", "cert": "k"}
    server = object.__new__(rs_app.MultiModelServer.func_or_class)
    server._cache_lock = threading.RLock()
    server._hot, server._version_cache = {}, OrderedDict()
    server._replica_id, server._snapshot_reader = "r", None
    server._models_gauge, server._snapshot_gauge = MagicMock(), MagicMock()
    loaded: list = []
    server._load_by_flavour = lambda name, alias, mv: loaded.append(mv) or object()
    snapshot = {
        "generation": 3,
        "models": {
            "jpcp": {
                "name": "jpcp",
                "aliases": {"Production": {"version": "7", "signature": record}},
            }
        },
    }
    server._apply_snapshot(snapshot)
    assert [mv.signature for mv in loaded] == [record]


def test_a_one_line_public_key_variable_is_a_trust_bundle(keys, monkeypatch):
    private, _, key = _write_keypair(keys, "k1")
    root = keys / "art"
    paths = _bundle(root)
    _signer(monkeypatch, private)
    supplychain.sign_model("JPCP", "1", paths, root=root)
    one_line = supplychain.signer_public_key()
    assert one_line == supplychain.public_key_b64(key.public_key())
    monkeypatch.delenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE")
    monkeypatch.setenv("EXAMLOPS_SIGNING_PUBLIC_KEYS", f" {one_line} ,")
    assert supplychain.verify_model("JPCP", "1", paths, root=root).ok


def test_the_cli_signs_a_registered_version_and_prints_the_public_key(keys, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    private, _, key = _write_keypair(keys, "k1")
    root = keys / "registry-copy"
    _bundle(root)
    _signer(monkeypatch, private)
    monkeypatch.setattr(supplychain, "registered_artifacts", lambda m, v, dst: root)
    result = CliRunner().invoke(app, ["models", "sign", "JPCP", "5"])
    assert result.exit_code == 0, result.output
    assert supplychain.public_key_b64(key.public_key()) in result.output
    result = CliRunner().invoke(app, ["models", "verify", "JPCP", "5"])
    assert result.exit_code == 0, result.output
    assert "verified" in result.output
