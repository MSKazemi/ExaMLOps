"""Review regressions for the ADR 0013 release evidence (supply-chain gate, provenance rows).

Each test pins a hole found in adversarial review of the first cut:

* the ``signature`` requirement accepted any row — a forged or edited ``model_signatures`` row
  passed the release gate, and its unverified digest was used to "bind" the BOM and provenance;
* a legacy HMAC row (shared secret, over a different digest) counted as a release signature;
* the ``bom`` requirement accepted a BOM naming no artifact digest, and read only the newest of
  the per-casing BOM rows, so a conflicting BOM could hide behind a timestamp tie;
* ``record_provenance`` read-then-upserted, so a concurrent recorder's *different* statement was
  silently overwritten without ``replace``;
* a build recorded unsigned (no key yet) could never be signed later without ``replace``;
* a keyless → Ed25519 fallback for provenance was only logged, never audited.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from examlops import supplychain
from examlops.data.registry import store_model_bom
from examlops.platform_db import get_db
from examlops.supplychain import provenance as prov
from examlops.supplychain import release
from examlops.supplychain.provenance import BuildContext
from tests.unit import _sigstore_fake

DIGEST = "sha256:" + "ab" * 32


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for var in (
        "EXAMLOPS_SIGNING_KEY",
        "EXAMLOPS_SIGNING_PRIVATE_KEY_FILE",
        "EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE",
        "EXAMLOPS_SIGNING_PUBLIC_KEYS",
        "EXAMLOPS_SIGNING_SCHEME",
        "EXAMLOPS_POLICY_GATES",
        "EXAMLOPS_DATASET_REVISION",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _key(tmp: Path, name: str = "k") -> Path:
    path = tmp / f"{name}.pem"
    path.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    return path


def _bundle(root: Path) -> list[Path]:
    (root / "model").mkdir(parents=True)
    (root / "MLmodel").write_text("flavors: {}\n")
    (root / "model" / "model.pkl").write_bytes(b"\x80\x04weights")
    return sorted(p for p in root.rglob("*") if p.is_file())


def _checks(result: release.ReleaseCheck) -> dict[str, tuple[bool, str]]:
    return {f.check: (f.ok, f.reason) for f in result.findings}


def _audit(action: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action=? ORDER BY id", (action,)
        ).fetchall()
    return [dict(r) for r in rows]


def _releasable(env, monkeypatch) -> list[Path]:
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(_key(env)))
    paths = _bundle(env / "art")
    release.attest_version(
        "JPCP", "1", paths, root=env / "art", ctx=BuildContext(dataset="FData"), sign=True
    )
    assert release.check_release("JPCP", "1").ok
    return paths


# ── signature: verified, not merely present ──────────────────────────────────────────────


def test_a_forged_signature_row_fails_the_release_gate(env, monkeypatch):
    _releasable(env, monkeypatch)
    with get_db() as conn:
        conn.execute(
            "UPDATE model_signatures SET signature=? WHERE lower(model)='jpcp'",
            ("AAAA" + "A" * 84,),
        )
    ok, reason = _checks(release.check_release("JPCP", "1"))["signature"]
    assert not ok and reason == "bad-signature"


def test_a_signature_by_an_untrusted_key_fails_the_release_gate(env, monkeypatch):
    _releasable(env, monkeypatch)
    # The verifier's trust bundle is a different key: the stored row is not by a trusted signer.
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(_key(env, "other")))
    ok, reason = _checks(release.check_release("JPCP", "1", require="signature"))["signature"]
    assert not ok and reason.startswith("untrusted-key")


def test_a_legacy_hmac_signature_is_not_release_evidence(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k" * 32)
    paths = _bundle(env / "art")
    sig = supplychain.sign_model("JPCP", "1", paths, root=env / "art")
    assert sig.algo == "hmac-sha256"
    ok, reason = _checks(release.check_release("JPCP", "1", require="signature"))["signature"]
    assert not ok and reason.startswith("legacy-hmac")


def test_a_forged_signature_digest_does_not_bind_the_bom(env, monkeypatch):
    """An edited digest must not become the reference the BOM and provenance are held to."""
    _releasable(env, monkeypatch)
    forged = "sha256:" + "f" * 64
    with get_db() as conn:
        conn.execute("UPDATE model_signatures SET digest=? WHERE lower(model)='jpcp'", (forged,))
    store_model_bom(
        "jpcp", "1", {"properties": [{"name": "examlops:artifact_digest", "value": forged}]}
    )
    result = release.check_release("JPCP", "1")
    checks = _checks(result)
    assert not result.ok
    assert checks["signature"] == (False, "bad-signature")
    assert not checks["bom"][0] and checks["bom"][1].startswith("bom-mismatch")


def test_keyless_signature_row_is_verified_by_the_gate(env, monkeypatch):
    _sigstore_fake.install(monkeypatch)
    ci, iss = "ci@example.org", "https://issuer.example"
    monkeypatch.setenv("EXAMLOPS_SIGNING_SCHEME", "sigstore")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN", _sigstore_fake.make_token(ci, iss))
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITIES", f"{ci}|{iss}")
    supplychain.sign_model("JPCP", "1", _bundle(env / "art"), root=env / "art")
    assert _checks(release.check_release("JPCP", "1", require="signature"))["signature"][0]
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITIES", f"someone-else@example.org|{iss}")
    ok, reason = _checks(release.check_release("JPCP", "1", require="signature"))["signature"]
    assert not ok and reason.startswith("bad-signature")


# ── bom: bound and unambiguous ───────────────────────────────────────────────────────────


def test_an_unbound_bom_fails_the_bom_requirement(env, monkeypatch):
    _releasable(env, monkeypatch)
    supplychain.generate_ai_bom("JPCP", "1")  # overwrites the bound BOM with an unbound one
    ok, reason = _checks(release.check_release("JPCP", "1", require="bom"))["bom"]
    assert not ok and reason.startswith("unbound")


def test_conflicting_boms_under_two_casings_fail_closed(env, monkeypatch):
    _releasable(env, monkeypatch)  # BOM written as `JPCP`, bound to the signed digest
    store_model_bom(
        "jpcp",
        "1",
        {"properties": [{"name": "examlops:artifact_digest", "value": "sha256:" + "0" * 64}]},
    )
    ok, reason = _checks(release.check_release("jpcp", "1", require="bom"))["bom"]
    assert not ok and reason.startswith("bom-mismatch")


def test_an_unsigned_version_binds_the_bom_to_verified_provenance(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(_key(env)))
    prov.record_provenance("JPCP", "1", DIGEST, BuildContext(dataset="FData"))
    supplychain.generate_ai_bom("JPCP", "1", artifact_digest="sha256:" + "0" * 64)
    ok, reason = _checks(release.check_release("JPCP", "1", require="bom,provenance"))["bom"]
    assert not ok and reason.startswith("bom-mismatch")
    supplychain.generate_ai_bom("JPCP", "1", artifact_digest=DIGEST)
    assert release.check_release("JPCP", "1", require="bom,provenance").ok


# ── provenance rows: no silent overwrite, unsigned can be signed later ───────────────────


def test_a_concurrent_different_statement_is_not_overwritten(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(_key(env)))
    first = prov.record_provenance("JPCP", "1", DIGEST, BuildContext(run_id="run-a"))
    real_get = prov._get_row
    calls = {"n": 0}

    def stale_read(model, version):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_get(model, version)  # read before the winner

    monkeypatch.setattr(prov, "_get_row", stale_read)
    with pytest.raises(prov.ProvenanceExists):
        prov.record_provenance("JPCP", "1", DIGEST, BuildContext(run_id="run-b"))
    monkeypatch.setattr(prov, "_get_row", real_get)
    assert prov.get_provenance("JPCP", "1")["statement_sha256"] == first.statement_sha256
    assert len(_audit("model_provenance_recorded")) == 1


def test_a_concurrent_identical_statement_is_a_no_op(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(_key(env)))
    ctx = BuildContext(run_id="run-a")
    first = prov.record_provenance("JPCP", "1", DIGEST, ctx)
    real_get = prov._get_row
    calls = {"n": 0}

    def stale_read(model, version):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_get(model, version)

    monkeypatch.setattr(prov, "_get_row", stale_read)
    again = prov.record_provenance("JPCP", "1", DIGEST, ctx)
    assert again.statement_sha256 == first.statement_sha256
    assert len(_audit("model_provenance_recorded")) == 1


def test_unsigned_provenance_is_signed_once_a_key_exists(env, monkeypatch):
    ctx = BuildContext(run_id="run-a", dataset="FData")
    assert prov.record_provenance("JPCP", "1", DIGEST, ctx).algo == prov.UNSIGNED
    # Still no key: re-recording stays a no-op (no second audit row).
    assert prov.record_provenance("JPCP", "1", DIGEST, ctx).algo == prov.UNSIGNED
    assert len(_audit("model_provenance_recorded")) == 1
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(_key(env)))
    upgraded = prov.record_provenance("JPCP", "1", DIGEST, ctx)  # no replace needed
    assert upgraded.algo == prov.ED25519_DSSE
    assert prov.verify_provenance("JPCP", "1", expected_digest=DIGEST).ok
    # A *different* statement over the now-signed row still needs replace.
    with pytest.raises(prov.ProvenanceExists):
        prov.record_provenance("JPCP", "1", DIGEST, BuildContext(run_id="run-b"))


def test_keyless_provenance_fallback_is_audited(env, monkeypatch):
    state = _sigstore_fake.install(monkeypatch)
    state["fail_signing"] = "rekor down"
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(_key(env)))
    monkeypatch.setenv("EXAMLOPS_SIGNING_SCHEME", "sigstore")
    monkeypatch.setenv("EXAMLOPS_SIGSTORE_IDENTITY_TOKEN", _sigstore_fake.make_token("a", "b"))
    rec = prov.record_provenance("JPCP", "1", DIGEST, BuildContext(run_id="r"), actor="ci")
    assert rec.algo == prov.ED25519_DSSE
    events = _audit("model_provenance_keyless_fallback")
    assert len(events) == 1
    assert "rekor down" in json.loads(events[0]["details"])["reason"]


# ── the autopilot's promotion road meets the same armed gate ─────────────────────────────


def _arm_supply_chain(monkeypatch, **opts):
    cfg = {"supply_chain": {"mode": "enforce", **opts}}
    monkeypatch.setattr("examlops.policy_engine.gates._from_file", lambda: cfg)


def _autopilot_cycle(staging_version: str | None = "1"):
    from unittest.mock import patch

    from examlops.cli.commands import autopilot_cmd
    from examlops.platform_db import set_autopilot_config, set_promotion_rule

    set_autopilot_config("enabled", "1")
    set_promotion_rule("JPCP", "rmse", "lt", 5.0, "Staging", "Production")
    with (
        patch.object(autopilot_cmd, "_alias_version", lambda model, alias="Production": "0"),
        patch.object(autopilot_cmd, "_staging_version", lambda model: staging_version),
        patch.object(autopilot_cmd, "_get_staging_metrics", return_value={"rmse": 3.0}),
        patch.object(autopilot_cmd, "_do_promote") as promote,
    ):
        result = autopilot_cmd.run_cycle()
    return result, promote


def test_autopilot_refuses_a_version_without_the_required_evidence(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(_key(env)))
    supplychain.sign_model("JPCP", "1", _bundle(env / "art"), root=env / "art")  # no provenance
    _arm_supply_chain(monkeypatch, require=["signature", "provenance"])
    result, promote = _autopilot_cycle()
    promote.assert_not_called()
    assert any("provenance" in b["reason"] for b in result["policy_blocks"])
    assert any(
        "supply_chain_gate" in str(e["details"]) for e in _audit("autopilot_promote_blocked")
    )


def test_autopilot_promotes_with_complete_evidence(env, monkeypatch):
    _releasable(env, monkeypatch)
    _arm_supply_chain(monkeypatch, require=["signature", "bom", "provenance"])
    _, promote = _autopilot_cycle()
    promote.assert_called_once()


def test_autopilot_armed_gate_with_no_resolvable_version_fails_closed(env, monkeypatch):
    _arm_supply_chain(monkeypatch)
    result, promote = _autopilot_cycle(staging_version=None)
    promote.assert_not_called()
    assert any("could not be resolved" in b["reason"] for b in result["policy_blocks"])


def test_autopilot_with_the_gate_off_is_unchanged(env, monkeypatch):
    monkeypatch.setattr("examlops.policy_engine.gates._from_file", lambda: {})
    _, promote = _autopilot_cycle()  # unsigned, gate off → promotes as before
    promote.assert_called_once()
