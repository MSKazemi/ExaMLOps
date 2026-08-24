# tests/unit/test_supplychain.py
"""D3 — ML supply-chain security (ADR 0013, spec D3).

GWT-1 sign→verify round-trip · GWT-2 tamper detection · GWT-3 enforce refuses ·
GWT-4 AI-BOM contents · GWT-5 unsigned model · GWT-6 warn mode may load.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import supplychain  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-test-signing-key")
    init_db()


def _artifacts(tmp_path: Path) -> list[Path]:
    a = tmp_path / "model.pkl"
    a.write_bytes(b"weights-v1")
    b = tmp_path / "MLmodel"
    b.write_text("flavor: sklearn\n")
    return [a, b]


def test_gwt1_sign_verify_roundtrip(tmp_path):
    paths = _artifacts(tmp_path)
    sig = supplychain.sign_model("JPCP", "17", paths, actor="alice")
    assert sig.algo == "hmac-sha256"
    assert len(sig.digest) == 64
    result = supplychain.verify_model("JPCP", "17", paths)
    assert result.ok is True
    assert result.reason == "verified"


def test_gwt2_tamper_detected(tmp_path):
    paths = _artifacts(tmp_path)
    supplychain.sign_model("JPCP", "17", paths)
    # Mutate an artifact after signing.
    paths[0].write_bytes(b"weights-v2-EVIL")
    result = supplychain.verify_model("JPCP", "17", paths)
    assert result.ok is False
    assert "tamper" in result.reason


def test_gwt3_enforce_refuses_on_failure(tmp_path):
    paths = _artifacts(tmp_path)
    supplychain.sign_model("JPCP", "17", paths)
    paths[0].write_bytes(b"tampered")
    assert supplychain.verify_before_load("JPCP", "17", paths, mode="enforce") is False


def test_gwt6_warn_may_load(tmp_path):
    paths = _artifacts(tmp_path)
    supplychain.sign_model("JPCP", "17", paths)
    paths[0].write_bytes(b"tampered")
    # warn mode records the failure but does not block the load
    assert supplychain.verify_before_load("JPCP", "17", paths, mode="warn") is True


def test_gwt5_unsigned_model_fails(tmp_path):
    paths = _artifacts(tmp_path)
    result = supplychain.verify_model("Ghost", "1", paths)
    assert result.ok is False
    assert "unsigned" in result.reason
    # enforce refuses an unsigned model
    assert supplychain.verify_before_load("Ghost", "1", paths, mode="enforce") is False


def test_gwt4_ai_bom_contents(tmp_path):
    doc = supplychain.generate_ai_bom(
        "JPCP",
        "17",
        dataset="FData",
        dataset_revision="abc123",
        framework="sklearn",
        eval_summary={"rmse": 4.2},
    )
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["metadata"]["component"]["name"] == "JPCP"
    data_comp = [c for c in doc["components"] if c["type"] == "data"][0]
    assert data_comp["name"] == "FData"
    assert data_comp["version"] == "abc123"
    props = {p["name"]: p["value"] for p in doc["properties"]}
    assert props["examlops:dataset_revision"] == "abc123"
    assert props["examlops:framework"] == "sklearn"


def test_bom_persisted_and_reloadable(tmp_path):
    from examlops.platform_db import get_model_bom

    supplychain.generate_ai_bom("JPCP", "18", dataset="FData")
    stored = get_model_bom("JPCP", "18")
    assert stored is not None
    assert stored["bomFormat"] == "CycloneDX"


def test_every_verification_is_a_fresh_one(tmp_path):
    """This test used to assert the second call came back `cached` (spec R8).

    It does not any more, because the cache is gone: keyed by artifact digest it answered a
    different question than the gate asks, and no correct key was worth the 4 microseconds it
    saved. See `test_verify_cache_answers_the_right_question.py` for the failure that ended it.
    """
    paths = _artifacts(tmp_path)
    supplychain.sign_model("JPCP", "17", paths)
    assert supplychain.verify_model("JPCP", "17", paths).reason == "verified"
    assert supplychain.verify_model("JPCP", "17", paths).reason == "verified"

    # And the second call is a real one: tamper between the two and it is caught.
    paths[0].write_bytes(b"weights-swapped")
    third = supplychain.verify_model("JPCP", "17", paths)
    assert third.ok is False
    assert "tamper" in third.reason


def test_missing_signing_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY", raising=False)
    paths = _artifacts(tmp_path)
    with pytest.raises(supplychain.SigningKeyMissing):
        supplychain.sign_model("JPCP", "17", paths)


def test_signature_persisted(tmp_path):
    from examlops.platform_db import get_model_signature

    paths = _artifacts(tmp_path)
    supplychain.sign_model("JPCP", "17", paths, actor="bob")
    row = get_model_signature("JPCP", "17")
    assert row is not None
    assert row["algo"] == "hmac-sha256"
    assert row["signed_by"] == "bob"
