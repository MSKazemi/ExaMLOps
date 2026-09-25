"""The supply-chain release gate (ADR 0013 clauses 2, 4, 5) — CLI, promotion and registration.

``check_release`` is only worth something if the platform paths it governs call it, so besides
the function these tests drive the real ``exa models attest|provenance|release-check`` commands,
``exa pipeline promote`` with an armed ``supply_chain`` gate, and the training pipeline's
registration hook.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from typer.testing import CliRunner

from examlops import supplychain
from examlops.cli.main import app
from examlops.platform_db import get_db
from examlops.supplychain import release
from examlops.supplychain.provenance import BuildContext, get_provenance

runner = CliRunner()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for var in (
        "EXAMLOPS_SIGNING_KEY",
        "EXAMLOPS_SIGNING_PRIVATE_KEY_FILE",
        "EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE",
        "EXAMLOPS_SIGNING_PUBLIC_KEYS",
        "EXAMLOPS_SIGNING_SCHEME",
        "EXAMLOPS_POLICY_GATES",
        "EXAMLOPS_POLICY_ENGINE",
        "EXAMLOPS_PROVENANCE_AT_REGISTRATION",
        "EXAMLOPS_SIGN_AT_REGISTRATION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.setattr("examlops.policy.POLICY_YAML", tmp_path / "policy.yaml")
    key = tmp_path / "signing.pem"
    key.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    monkeypatch.setenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", str(key))
    return tmp_path


def _bundle(root: Path) -> list[Path]:
    (root / "model").mkdir(parents=True)
    (root / "MLmodel").write_text("flavors: {}\n")
    (root / "model" / "model.pkl").write_bytes(b"\x80\x04weights")
    return sorted(p for p in root.rglob("*") if p.is_file())


def _checks(result: release.ReleaseCheck) -> dict[str, bool]:
    return {f.check: f.ok for f in result.findings}


# ── check_release ────────────────────────────────────────────────────────────────────────


def test_a_bare_version_fails_every_requirement(env):
    result = release.check_release("JPCP", "1")
    assert not result.ok
    assert _checks(result) == {"signature": False, "bom": False, "provenance": False}
    assert result.failures()[0].startswith("signature: unsigned")


def test_attest_with_sign_makes_a_version_releasable(env):
    paths = _bundle(env / "art")
    out = release.attest_version(
        "JPCP", "1", paths, root=env / "art", ctx=BuildContext(dataset="FData"), sign=True
    )
    assert out.signature_algo == "ed25519-v2" and out.provenance_algo == "ed25519-dsse"
    result = release.check_release("jpcp", "1")
    assert result.ok, result.failures()
    assert _checks(result) == {"signature": True, "bom": True, "provenance": True}


def test_a_bom_for_other_bytes_fails_integrity(env):
    paths = _bundle(env / "art")
    release.attest_version("JPCP", "1", paths, root=env / "art", sign=True)
    supplychain.generate_ai_bom("JPCP", "1", artifact_digest="sha256:" + "0" * 64)
    result = release.check_release("JPCP", "1", require=["bom"])
    assert not result.ok and "bom-mismatch" in result.failures()[0]


def test_attest_refuses_bytes_other_than_the_signed_ones(env):
    paths = _bundle(env / "art")
    supplychain.sign_model("JPCP", "1", paths, root=env / "art")
    (env / "art" / "model" / "model.pkl").write_bytes(b"swapped")
    with pytest.raises(ValueError, match="do not match the signed digest"):
        release.attest_version("JPCP", "1", paths, root=env / "art")
    assert get_provenance("JPCP", "1") is None


def test_attest_with_no_files_is_refused(env):
    with pytest.raises(ValueError, match="no artifact files"):
        release.attest_version("JPCP", "1", [env / "nothing"])


def test_unsigned_provenance_fails_the_gate(env, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE")
    paths = _bundle(env / "art")
    release.attest_version("JPCP", "1", paths, root=env / "art")
    result = release.check_release("JPCP", "1", require="bom,provenance")
    assert _checks(result) == {"bom": True, "provenance": False}


@pytest.mark.parametrize("bad", ["signature,sbom", "", ["nope"]])
def test_requirements_are_validated(bad):
    with pytest.raises(ValueError):
        release.normalize_requirements(bad)


def test_gate_decision_denies_incomplete_and_fails_closed_on_typos(env):
    denied = release.release_gate_decision("JPCP", "1", ["signature", "provenance"])
    assert not denied.allow and any("provenance" in r for r in denied.reasons)
    typo = release.release_gate_decision("JPCP", "1", "signatur")
    assert not typo.allow and "misconfigured" in typo.reasons[0]


def test_gate_decision_allows_complete_evidence(env):
    release.attest_version("JPCP", "1", _bundle(env / "art"), root=env / "art", sign=True)
    assert release.release_gate_decision("JPCP", "1", ["signature", "bom", "provenance"]).allow


# ── exa pipeline promote with an armed supply_chain gate ──────────────────────────────────

_ALIAS = {"registered_model": {"aliases": [{"alias": "Staging", "version": "19"}]}}
_VER = {"model_version": {"run_id": "run-abc", "version": "19"}}
_RUN = {"run": {"data": {"metrics": {"rmse": 4.5}, "params": {}, "tags": []}}}


def _get(url, **_):
    if "registered-models/get" in url:
        return _ALIAS
    if "model-versions/get" in url:
        return _VER
    if "runs/get" in url:
        return _RUN
    return {}


def _promote():
    args = ["--yes", "pipeline", "promote", "jpcp", "--if-rmse-lt", "5.0"]
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}) as post,
    ):
        res = runner.invoke(app, args)
    return res, post


_GATE = "gates:\n  supply_chain:\n    mode: enforce\n    require: [signature, bom, provenance]\n"


def test_promotion_refuses_a_signed_version_without_provenance(env):
    (env / "policy.yaml").write_text(_GATE)
    supplychain.sign_model("jpcp", "19", _bundle(env / "art"), root=env / "art")
    res, post = _promote()
    assert res.exit_code == 1, res.output
    assert "provenance" in res.output and not post.called


def test_promotion_proceeds_with_complete_evidence(env):
    (env / "policy.yaml").write_text(_GATE)
    release.attest_version("jpcp", "19", _bundle(env / "art"), root=env / "art", sign=True)
    res, post = _promote()
    assert res.exit_code == 0, res.output
    assert post.called


def test_promotion_gate_without_require_keeps_its_old_meaning(env):
    (env / "policy.yaml").write_text("gates:\n  supply_chain: enforce\n")
    supplychain.sign_model("jpcp", "19", _bundle(env / "art"), root=env / "art")
    res, post = _promote()  # signed, no provenance: the historical gate allows it
    assert res.exit_code == 0, res.output and post.called


# ── CLI ──────────────────────────────────────────────────────────────────────────────────


def test_cli_attest_provenance_release_check(env):
    art = env / "art"
    _bundle(art)
    res = runner.invoke(
        app,
        [
            "--json",
            "models",
            "attest",
            "JPCP",
            "3",
            "--path",
            str(art),
            "--sign",
            "--dataset",
            "FData",
            "--dataset-revision",
            "r1",
        ],
    )
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["provenance_algo"] == "ed25519-dsse" and doc["subject_digest"].startswith("sha256:")

    out = env / "evidence" / "jpcp-3.json"
    res = runner.invoke(app, ["models", "provenance", "JPCP", "3", "--output", str(out)])
    assert res.exit_code == 0, res.output
    assert json.loads(out.read_text())["payloadType"] == "application/vnd.in-toto+json"

    res = runner.invoke(app, ["--json", "models", "release-check", "JPCP", "3"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["ok"] is True


def test_cli_release_check_fails_ci_on_missing_evidence(env):
    res = runner.invoke(app, ["models", "release-check", "JPCP", "4"])
    assert res.exit_code == 1
    assert "not releasable" in res.output


def test_cli_release_check_rejects_unknown_requirement(env):
    res = runner.invoke(app, ["models", "release-check", "JPCP", "4", "--require", "sbom"])
    assert res.exit_code == 1 and "unknown release requirement" in res.output


def test_cli_provenance_missing_and_tampered(env):
    res = runner.invoke(app, ["models", "provenance", "JPCP", "5"])
    assert res.exit_code == 1 and "No provenance" in res.output
    art = env / "art"
    _bundle(art)
    assert runner.invoke(app, ["models", "attest", "JPCP", "5", "--path", str(art)]).exit_code == 0
    with get_db() as conn:
        conn.execute("UPDATE model_provenance SET subject_digest=?", ("sha256:" + "1" * 64,))
    res = runner.invoke(app, ["models", "provenance", "JPCP", "5"])
    assert res.exit_code == 1 and "subject-mismatch" in res.output


def test_cli_attest_twice_needs_replace(env):
    art = env / "art"
    _bundle(art)
    base = ["models", "attest", "JPCP", "6", "--path", str(art)]
    assert runner.invoke(app, [*base, "--run-id", "a"]).exit_code == 0
    res = runner.invoke(app, [*base, "--run-id", "b"])
    assert res.exit_code == 1 and "--replace" in res.output
    assert runner.invoke(app, [*base, "--run-id", "b", "--replace"]).exit_code == 0


# ── registration hook (pipelines/pipeline_generator.py) ─────────────────────────────────────


@pytest.fixture()
def generator():
    import os

    repo = Path(__file__).resolve().parents[2]
    mz = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or (repo / "modelzoo"))
    if not (mz / "seanergys_modelzoo").is_dir():
        pytest.skip("seanergys_modelzoo not present — set EXAMLOPS_MODELZOO_DIR to a checkout")
    for p in (str(repo), str(repo / "pipelines")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import pipelines.pipeline_generator as gen

    return gen


def test_registration_records_evidence_bound_to_the_signed_digest(env, monkeypatch, generator):
    root = env / "registry-copy"
    _bundle(root)
    monkeypatch.setattr(supplychain, "registered_artifacts", lambda m, v, dst: root)
    sig = generator._sign_registered("jpcp", "21")
    generator._record_release_evidence(
        "jpcp", "21", sig, run_id="run-1", dataset="FData", job_id="77", scheduler="flux"
    )
    assert release.check_release("jpcp", "21").ok
    st = get_provenance("jpcp", "21")["statement"]
    assert st["predicate"]["runDetails"]["metadata"]["invocationId"] == "run-1"


def test_registration_auto_skips_without_a_manifest_digest(env, monkeypatch, generator):
    generator._record_release_evidence("jpcp", "22", None, dataset="FData")
    assert get_provenance("jpcp", "22") is None


def test_registration_required_downloads_and_records(env, monkeypatch, generator):
    root = env / "registry-copy"
    _bundle(root)
    monkeypatch.setattr(supplychain, "registered_artifacts", lambda m, v, dst: root)
    monkeypatch.setenv("EXAMLOPS_PROVENANCE_AT_REGISTRATION", "required")
    generator._record_release_evidence("jpcp", "23", None, dataset="FData")
    assert get_provenance("jpcp", "23")["algo"] == "ed25519-dsse"


def test_registration_required_fails_the_run_when_it_cannot_record(env, monkeypatch, generator):
    def boom(*_a, **_k):
        raise ConnectionError("mlflow down")

    monkeypatch.setattr(supplychain, "registered_artifacts", boom)
    monkeypatch.setenv("EXAMLOPS_PROVENANCE_AT_REGISTRATION", "required")
    with pytest.raises(RuntimeError, match="cannot record provenance"):
        generator._record_release_evidence("jpcp", "24", None)


def test_registration_off_records_nothing(env, monkeypatch, generator):
    root = env / "registry-copy"
    _bundle(root)
    monkeypatch.setattr(supplychain, "registered_artifacts", lambda m, v, dst: root)
    monkeypatch.setenv("EXAMLOPS_PROVENANCE_AT_REGISTRATION", "off")
    sig = generator._sign_registered("jpcp", "25")
    generator._record_release_evidence("jpcp", "25", sig)
    assert get_provenance("jpcp", "25") is None
