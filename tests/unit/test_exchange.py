"""NovaFabric Exchange signed packages (enterprise-readiness Phase 5, item 5.6).

Proves the cross-institution trust story: packages are signed + integrity-hashed, verify passes for
an intact package, tampering (a swapped file or forged signature) is caught, verify-before-import
refuses an unverified package, and packing without a signing key fails closed.
"""

from __future__ import annotations

import zipfile

import pytest

from examlops import exchange


@pytest.fixture
def signed(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "unit-signing-key")
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)
    (tmp_path / "model.bin").write_bytes(b"weights-v1")
    (tmp_path / "card.md").write_text("# Model Card")
    out = tmp_path / "jpcp.novapack"
    manifest = exchange.pack(
        "model", "JPCP", [tmp_path / "model.bin", tmp_path / "card.md"], str(out), version="17"
    )
    return tmp_path, out, manifest


def test_pack_and_verify_roundtrip(signed):
    _, out, manifest = signed
    assert manifest["kind"] == "model" and manifest["signature"]
    result = exchange.verify(str(out))
    assert result.ok is True and result.reason == "verified"


def test_inspect_reads_manifest(signed):
    _, out, _ = signed
    m = exchange.inspect(str(out))
    assert m["name"] == "JPCP" and m["version"] == "17" and "model.bin" in m["files"]


def test_tampered_file_is_detected(signed, tmp_path):
    _, out, _ = signed
    # Rewrite the zip with a swapped file body.
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(str(out)) as src, zipfile.ZipFile(buf, "w") as dst:
        for item in src.namelist():
            data = src.read(item)
            if item == "model.bin":
                data = b"malicious-weights"
            dst.writestr(item, data)
    (tmp_path / "jpcp.novapack").write_bytes(buf.getvalue())
    result = exchange.verify(str(out))
    assert result.ok is False and "tamper" in result.reason.lower()


def test_forged_signature_is_rejected(signed, monkeypatch):
    _, out, _ = signed
    # A different signing key can't produce a matching signature → verify fails.
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "attacker-key")
    result = exchange.verify(str(out))
    assert result.ok is False and "signature" in result.reason.lower()


def test_import_refuses_unverified(signed, tmp_path, monkeypatch):
    _, out, _ = signed
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "attacker-key")  # signature won't match
    with pytest.raises(exchange.ExchangeError, match="unverified"):
        exchange.import_pack(str(out), str(tmp_path / "dest"))


def test_import_extracts_verified_package(signed, tmp_path):
    _, out, _ = signed
    dest = tmp_path / "dest"
    manifest = exchange.import_pack(str(out), str(dest))
    assert (dest / "model.bin").read_bytes() == b"weights-v1"
    assert manifest["name"] == "JPCP"
    assert not (dest / "novapack.manifest.json").exists()  # manifest not extracted as a file


def test_pack_without_signing_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SIGNING_KEY", raising=False)
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)
    import examlops.supplychain as sc

    monkeypatch.setattr(
        sc, "_signing_key", lambda: (_ for _ in ()).throw(sc.SigningKeyMissing("x"))
    )
    (tmp_path / "f.bin").write_bytes(b"x")
    with pytest.raises(exchange.ExchangeError, match="sign"):
        exchange.pack("model", "M", [tmp_path / "f.bin"], str(tmp_path / "m.novapack"))


def test_invalid_kind_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k")
    (tmp_path / "f.bin").write_bytes(b"x")
    with pytest.raises(exchange.ExchangeError, match="kind"):
        exchange.pack("malware", "M", [tmp_path / "f.bin"], str(tmp_path / "m.novapack"))
