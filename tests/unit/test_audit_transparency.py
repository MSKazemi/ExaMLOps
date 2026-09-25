"""Transparency-log anchoring of audit checkpoints (ADR 0028 decisions 2 and 3).

Tested against ``FakeRekor``, an in-memory Rekor v1 that behaves like the real one where the
anchor depends on it: it accepts only ``hashedrekord`` entries whose signature verifies over the
**pre-hashed** digest with the supplied public key (exactly Rekor's own check, so a signature the
fake accepts is one Rekor accepts), answers a duplicate with ``409`` + ``Location``, serves
entries by uuid, and signs a Signed Entry Timestamp over the canonical ``{body, integratedTime,
logID, logIndex}`` with its own ECDSA key.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils
from typer.testing import CliRunner

from examlops import audit_transparency as at
from examlops import audit_worm
from examlops.audit_transparency import HttpResponse, TransparencyError
from examlops.cli.main import app

runner = CliRunner()
REKOR = "https://rekor.example.test"


def _pem_private(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _pem_public(key) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


class FakeRekor:
    def __init__(self):
        self.log_key = ec.generate_private_key(ec.SECP256R1())
        self.entries: dict[str, dict] = {}
        self.posts = 0
        self.down = False
        self.lose_first_response = False

    def _entry(self, body_b64: str, index: int) -> dict:
        entry = {
            "body": body_b64,
            "integratedTime": 1_790_000_000 + index,
            "logID": "c0d23d6ad406973f9559f3ba2d1ca01f84147d8ffc5b8445c224f98b9591801d",
            "logIndex": index,
        }
        payload = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        set_sig = self.log_key.sign(payload, ec.ECDSA(hashes.SHA256()))
        entry["verification"] = {"signedEntryTimestamp": base64.b64encode(set_sig).decode()}
        return entry

    def __call__(self, method, url, body, headers, timeout):
        assert timeout > 0
        if self.down:
            raise ConnectionError("rekor unreachable")
        path = url[len(REKOR) :]
        if method == "POST" and path == "/api/v1/log/entries":
            self.posts += 1
            proposed = json.loads(body)
            if proposed.get("kind") != "hashedrekord":
                return HttpResponse(400, {}, b'{"message":"unsupported kind"}')
            spec = proposed["spec"]
            digest = bytes.fromhex(spec["data"]["hash"]["value"])
            pub = serialization.load_pem_public_key(
                base64.b64decode(spec["signature"]["publicKey"]["content"])
            )
            try:
                pub.verify(
                    base64.b64decode(spec["signature"]["content"]),
                    digest,
                    ec.ECDSA(utils.Prehashed(hashes.SHA256())),
                )
            except Exception:
                return HttpResponse(400, {}, b'{"message":"signature does not verify"}')
            canonical = json.dumps(proposed, sort_keys=True, separators=(",", ":")).encode()
            uuid = hashlib.sha256(canonical).hexdigest()
            if uuid in self.entries:
                return HttpResponse(409, {"location": f"/api/v1/log/entries/{uuid}"}, b"{}")
            self.entries[uuid] = self._entry(
                base64.b64encode(canonical).decode(), len(self.entries)
            )
            if self.lose_first_response:
                self.lose_first_response = False
                raise TimeoutError("response lost after the entry was committed")
            return HttpResponse(201, {}, json.dumps({uuid: self.entries[uuid]}).encode())
        if method == "GET" and path.startswith("/api/v1/log/entries/"):
            uuid = path.rsplit("/", 1)[-1]
            if uuid not in self.entries:
                return HttpResponse(404, {}, b'{"message":"not found"}')
            return HttpResponse(200, {}, json.dumps({uuid: self.entries[uuid]}).encode())
        return HttpResponse(404, {}, b"{}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-signing-key")
    monkeypatch.delenv("EXAMLOPS_AUDIT_WORM_PATH", raising=False)
    monkeypatch.delenv("EXAMLOPS_AUDIT_TRANSPARENCY", raising=False)
    monkeypatch.delenv("EXAMLOPS_AUDIT_REKOR_PUBLIC_KEY_FILE", raising=False)
    key = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "tlog.pem").write_bytes(_pem_private(key))
    monkeypatch.setenv("EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE", str(tmp_path / "tlog.pem"))
    monkeypatch.setenv("EXAMLOPS_AUDIT_REKOR_URL", REKOR)
    fake = FakeRekor()
    at.set_transport(fake)
    at.reset_transparency_failures()
    from examlops.platform_db import init_db

    init_db()
    yield fake
    at.set_transport(None)
    at.set_sigstore_signer(None)


def _event(action: str = "model_approved") -> None:
    from examlops.data.audit import write_audit_event

    write_audit_event("cli", "alice", action, "JPCP", {})


def _receipts() -> list[dict]:
    return at.list_receipts(100)


def _head() -> dict:
    """The chain's real head: verification binds every receipt to the audit trail itself."""
    from examlops.data.audit import audit_chain_head

    h = audit_chain_head()
    assert h is not None
    return {"head_id": h["id"], "head_hash": h["hash"]}


def _real_head(action: str = "model_approved") -> dict:
    _event(action)
    return _head()


# ── configuration ────────────────────────────────────────────────────────────────────────────


def test_backend_is_implied_by_the_rekor_url_and_off_without_it(env, monkeypatch):
    assert at.backend() == "rekor"
    monkeypatch.delenv("EXAMLOPS_AUDIT_REKOR_URL")
    assert at.backend() == "off"
    assert at.anchor_checkpoint({"head_id": 1, "head_hash": "h"}) is None


def test_an_unknown_backend_is_an_error_not_off(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUDIT_TRANSPARENCY", "blockchain")
    with pytest.raises(TransparencyError, match="must be one of"):
        at.backend()
    assert at.enabled() is True  # misconfigured must be reported, never silently skipped


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://rekor.sigstore.dev", True),
        ("http://127.0.0.1:3000", True),
        ("http://localhost:3000", True),
        ("http://rekor.internal:3000", False),
        ("ftp://rekor.example", False),
    ],
)
def test_plain_http_is_refused_off_the_loopback(env, monkeypatch, url, ok):
    monkeypatch.setenv("EXAMLOPS_AUDIT_REKOR_URL", url)
    if ok:
        assert at.rekor_url() == url
    else:
        with pytest.raises(TransparencyError, match="only https"):
            at.rekor_url()


# ── anchoring ────────────────────────────────────────────────────────────────────────────────


def test_a_checkpoint_is_logged_with_a_signature_rekor_accepts(env):
    res = at.anchor_checkpoint({"head_id": 7, "head_hash": "abc"})
    assert res["status"] == "logged" and res["log_index"] == 0
    (rec,) = _receipts()
    stmt = at.checkpoint_statement(7, "abc")
    assert rec["statement_sha256"] == hashlib.sha256(stmt).hexdigest()
    assert rec["entry_uuid"] in env.entries and rec["backend"] == "rekor"
    assert json.loads(rec["receipt"])["logIndex"] == 0


def test_logging_is_idempotent_per_head(env):
    at.anchor_checkpoint({"head_id": 7, "head_hash": "abc"})
    again = at.anchor_checkpoint({"head_id": 7, "head_hash": "abc"})
    assert again["status"] == "already-logged"
    assert env.posts == 1 and len(_receipts()) == 1


def test_a_lost_response_is_recovered_through_the_409_location(env):
    env.lose_first_response = True
    with pytest.raises(TransparencyError, match="response lost"):
        at.anchor_checkpoint({"head_id": 3, "head_hash": "h3"})
    assert _receipts() == []  # nothing recorded for a failed call...
    res = at.anchor_checkpoint({"head_id": 3, "head_hash": "h3"})
    # ...and the retry finds the entry the log already committed, instead of failing or doubling.
    assert res["status"] == "logged" and len(env.entries) == 1
    assert _receipts()[0]["entry_uuid"] == next(iter(env.entries))


def test_no_signing_key_fails_closed_and_is_counted(env, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE")
    with pytest.raises(TransparencyError, match="no transparency signing key"):
        at.anchor_checkpoint({"head_id": 1, "head_hash": "h"})
    assert at.transparency_failures() == {"rekor": 1}
    assert env.posts == 0 and _receipts() == []


def test_a_non_p256_key_is_refused(env, tmp_path, monkeypatch):
    (tmp_path / "ed.pem").write_bytes(_pem_private(ed25519.Ed25519PrivateKey.generate()))
    monkeypatch.setenv("EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE", str(tmp_path / "ed.pem"))
    with pytest.raises(TransparencyError, match="ECDSA P-256"):
        at.anchor_checkpoint({"head_id": 1, "head_hash": "h"})


def test_a_log_that_is_down_is_an_error_not_a_receipt(env):
    env.down = True
    with pytest.raises(TransparencyError, match="unreachable"):
        at.anchor_checkpoint({"head_id": 1, "head_hash": "h"})
    assert _receipts() == []


def test_an_oversized_response_is_refused(env):
    def huge(method, url, body, headers, timeout):
        return HttpResponse(201, {}, b" " * ((1 << 20) + 10))

    at.set_transport(huge)
    with pytest.raises(TransparencyError, match="larger than"):
        at.anchor_checkpoint({"head_id": 1, "head_hash": "h"})


def test_a_log_that_records_a_different_digest_is_rejected(env):
    real = env.__call__

    def lying(method, url, body, headers, timeout):
        resp = real(method, url, body, headers, timeout)
        if method == "POST" and resp.status == 201:
            doc = json.loads(resp.body)
            uuid = next(iter(doc))
            logged = json.loads(base64.b64decode(doc[uuid]["body"]))
            logged["spec"]["data"]["hash"]["value"] = "00" * 32
            doc[uuid]["body"] = base64.b64encode(json.dumps(logged).encode()).decode()
            return HttpResponse(201, {}, json.dumps(doc).encode())
        return resp

    at.set_transport(lying)
    with pytest.raises(TransparencyError, match="does not record this checkpoint"):
        at.anchor_checkpoint({"head_id": 1, "head_hash": "h"})
    assert _receipts() == []


# ── verification ─────────────────────────────────────────────────────────────────────────────


def test_verify_passes_including_the_signed_entry_timestamp(env, tmp_path, monkeypatch):
    for i in range(3):
        _event(f"e{i}")
        at.anchor_checkpoint(_head())
    (tmp_path / "log.pub").write_bytes(_pem_public(env.log_key))
    monkeypatch.setenv("EXAMLOPS_AUDIT_REKOR_PUBLIC_KEY_FILE", str(tmp_path / "log.pub"))
    res = at.verify_transparency()
    assert res["ok"] is True and res["checked"] == 3 and res["set_checked"] is True


def test_verify_fails_on_a_set_from_another_log(env, tmp_path, monkeypatch):
    at.anchor_checkpoint(_real_head())
    other = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "log.pub").write_bytes(_pem_public(other))
    monkeypatch.setenv("EXAMLOPS_AUDIT_REKOR_PUBLIC_KEY_FILE", str(tmp_path / "log.pub"))
    res = at.verify_transparency()
    assert res["ok"] is False
    assert "Signed Entry Timestamp" in res["failures"][0]["reason"]


def test_verify_fails_when_the_receipt_index_was_edited(env):
    at.anchor_checkpoint(_real_head())
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute("UPDATE audit_transparency_entries SET log_index = 99")
    res = at.verify_transparency()
    assert res["ok"] is False and "index" in res["failures"][0]["reason"]


def test_verify_fails_when_the_checkpoint_hash_was_edited(env):
    at.anchor_checkpoint(_real_head())
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute("UPDATE audit_transparency_entries SET head_hash = 'forged'")
    res = at.verify_transparency()
    assert res["ok"] is False
    assert "statement digest" in res["failures"][0]["reason"]


def test_verify_fails_when_the_entry_is_gone_from_the_log(env):
    at.anchor_checkpoint(_real_head())
    env.entries.clear()
    res = at.verify_transparency()
    assert res["ok"] is False and "HTTP 404" in res["failures"][0]["reason"]


def test_verify_fails_when_the_chain_was_rewritten_after_logging(env):
    # The log's whole purpose: an operator who can rewrite the DB (and re-sign) cannot make the
    # chain disagree with what was logged without verify-transparency saying so.
    at.anchor_checkpoint(_real_head())
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_events'"
        ).fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("UPDATE audit_events SET hash = 'rewritten'")
    res = at.verify_transparency()
    assert res["ok"] is False and "rewritten" in res["failures"][0]["reason"]


def test_verify_fails_when_the_logged_head_is_missing_from_the_chain(env):
    at.anchor_checkpoint({"head_id": 4242, "head_hash": "never-in-this-chain"})
    res = at.verify_transparency()
    assert res["ok"] is False and "missing from the audit chain" in res["failures"][0]["reason"]


def test_verify_rejects_a_receipt_swapped_for_a_self_signed_entry(env, tmp_path, monkeypatch):
    # An attacker with DB write access logs its own entry (its own key) and points the receipt at
    # it. The entry is internally consistent; only the pinned key can tell it apart.
    head = _real_head()
    platform_pub = _pem_public(
        serialization.load_pem_private_key((tmp_path / "tlog.pem").read_bytes(), None)
    )
    attacker = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "attacker.pem").write_bytes(_pem_private(attacker))
    monkeypatch.setenv("EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE", str(tmp_path / "attacker.pem"))
    at.anchor_checkpoint(head)
    # The verifier holds only the platform's public key.
    (tmp_path / "platform.pub").write_bytes(platform_pub)
    monkeypatch.setenv(
        "EXAMLOPS_AUDIT_TRANSPARENCY_PUBLIC_KEY_FILE", str(tmp_path / "platform.pub")
    )
    res = at.verify_transparency()
    assert res["ok"] is False and "different key" in res["failures"][0]["reason"]


def test_verify_without_any_trusted_key_fails_closed(env, monkeypatch):
    at.anchor_checkpoint(_real_head())
    monkeypatch.delenv("EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE")
    monkeypatch.delenv("EXAMLOPS_AUDIT_TRANSPARENCY_PUBLIC_KEY_FILE", raising=False)
    res = at.verify_transparency()
    assert (
        res["ok"] is False and "no trusted transparency public key" in res["failures"][0]["reason"]
    )


def test_verify_with_nothing_configured_is_ok_and_says_so(env, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUDIT_REKOR_URL")
    res = at.verify_transparency()
    assert res == {
        "ok": True,
        "backend": "off",
        "checked": 0,
        "reason": "no transparency log configured",
        "failures": [],
    }


# ── sigstore (keyless) ───────────────────────────────────────────────────────────────────────


def _bundle(payload: bytes, *, digest: bytes | None = None) -> dict:
    d = digest if digest is not None else hashlib.sha256(payload).digest()
    return {
        "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        "messageSignature": {
            "messageDigest": {"algorithm": "SHA2_256", "digest": base64.b64encode(d).decode()},
            "signature": "c2ln",
        },
        "verificationMaterial": {"tlogEntries": [{"logIndex": "42", "integratedTime": "1790"}]},
    }


def test_sigstore_backend_records_the_bundle(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUDIT_TRANSPARENCY", "sigstore")
    at.set_sigstore_signer(_bundle)
    res = at.anchor_checkpoint(_real_head())
    assert res["status"] == "logged" and res["log_index"] == 42
    v = at.verify_transparency()
    # ok, but honest that the certificate chain / identity was not checked
    assert v["ok"] is True and v["sigstore_structural_only"] == 1
    assert "certificate chain" in v["warning"]


def test_sigstore_bundle_over_another_digest_is_rejected(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUDIT_TRANSPARENCY", "sigstore")
    at.set_sigstore_signer(lambda p: _bundle(p, digest=b"\x00" * 32))
    with pytest.raises(TransparencyError, match="different digest"):
        at.anchor_checkpoint({"head_id": 5, "head_hash": "h5"})
    assert _receipts() == []


def test_sigstore_without_the_library_is_refused_not_faked(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUDIT_TRANSPARENCY", "sigstore")
    import sys

    monkeypatch.setitem(sys.modules, "sigstore", None)  # make the import fail deterministically
    with pytest.raises(TransparencyError, match="audit-sigstore"):
        at.anchor_checkpoint({"head_id": 5, "head_hash": "h5"})
    assert _receipts() == []


# ── wired into the checkpoint path ───────────────────────────────────────────────────────────


def test_checkpoint_and_anchor_logs_the_new_checkpoint(env):
    _event()
    out = audit_worm.checkpoint_and_anchor()
    assert out["status"] == "checkpointed" and out["transparency_logged"] is True
    (rec,) = _receipts()
    assert rec["head_hash"] == out["head_hash"]


def test_a_transparency_outage_does_not_lose_the_checkpoint(env):
    _event()
    env.down = True
    out = audit_worm.checkpoint_and_anchor()
    assert out["status"] == "checkpointed"
    assert out["transparency_logged"] is False and "unreachable" in out["transparency_error"]
    from examlops.data.audit import list_audit_checkpoints

    assert len(list_audit_checkpoints(10)) == 1  # the signed checkpoint stands
    # The cheap periodic call does not treat the head as done while the log lacks it...
    env.down = False
    again = audit_worm.checkpoint_and_anchor(skip_if_unchanged=True)
    assert again["transparency_logged"] is True and len(_receipts()) == 1
    # ...and once it has it, the next call is a no-op.
    assert audit_worm.checkpoint_and_anchor(skip_if_unchanged=True)["status"] == "unchanged"
    assert env.posts == 1


def test_unlogged_checkpoints_are_reported(env):
    _event()
    audit_worm.checkpoint_and_anchor()
    env.down = True
    _event("second")
    audit_worm.checkpoint_and_anchor()
    env.down = False
    res = at.verify_transparency()
    assert res["ok"] is True and res["unlogged"] == 1 and "warning" in res


def test_cli_checkpoint_and_verify_transparency(env):
    _event()
    r = runner.invoke(app, ["--json", "audit", "checkpoint"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["transparency"]["status"] == "logged"
    r = runner.invoke(app, ["--json", "audit", "verify-transparency"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["checked"] == 1
    env.entries.clear()
    r = runner.invoke(app, ["--json", "audit", "verify-transparency"])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["ok"] is False
