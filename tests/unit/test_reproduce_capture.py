"""ADR 0038 clause 1 + 4 — the manifest is *filled* by the platform, and it feeds D1/D2.

Clause 1 lists inputs the collector used to accept but no path supplied: the model-library
commit, hardware, scheduler resources, lineage run id, feature-view versions, image digest and
a BOM. Clause 4 says bundles feed D1 technical documentation and D2 evidence. Every test here
asserts the recorded value or the verification outcome, not that a function was called.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "core.hooksPath", "/dev/null")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    for name, text in files.items():
        (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "-c", "commit.gpgsign=false", "commit", "-qm", "init")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k")
    for var in (
        "EXAMLOPS_MODELZOO_DIR",
        "EXAMLOPS_IMAGE_DIGEST",
        "EXAMLOPS_FEATURE_VIEWS",
        "EXAMLOPS_LAKEFS_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)
    from examlops import platform_db

    platform_db.init_db()
    repo = tmp_path / "repo"
    _init_repo(repo, {"uv.lock": "lock\n"})
    monkeypatch.chdir(repo)
    return repo


def _build(**kw):
    from examlops.reproducibility import build_bundle

    return build_bundle("m", "1", **kw)


def _verify():
    from examlops.reproducibility import verify_bundle

    return verify_bundle("m", "1", allow_env_drift=True)


# ── code: all relevant repositories ──────────────────────────────────────────────────────────


def test_modelzoo_commit_is_recorded_and_verified(env, tmp_path, monkeypatch):
    mz = tmp_path / "zoo"
    sha = _init_repo(mz, {"lib.py": "v = 1\n"})
    monkeypatch.setenv("EXAMLOPS_MODELZOO_DIR", str(mz))
    b = _build()
    rec = b.manifest["code_commits"]
    assert rec["modelzoo"]["commit"] == sha and rec["modelzoo"]["dirty"] is False
    assert rec["platform"]["commit"] == _git(env, "rev-parse", "HEAD")
    assert {"kind": "code", "ref": f"modelzoo@{sha}", "hash": sha, "repo": str(mz)} in b.manifest[
        "inputs"
    ]
    assert _verify().reproducible

    # The verifying host's library checkout no longer has the commit: rot is reported by name.
    other = tmp_path / "other-zoo"
    _init_repo(other, {"lib.py": "v = 2\n"})
    monkeypatch.setenv("EXAMLOPS_MODELZOO_DIR", str(other))
    v = _verify()
    assert not v.reproducible
    assert any("commit not reachable" in p and str(other) in p for p in v.problems)


def test_a_modelzoo_dir_inside_the_platform_repo_is_not_a_commit(env, monkeypatch):
    (env / "modelzoo").mkdir()
    monkeypatch.setenv("EXAMLOPS_MODELZOO_DIR", str(env / "modelzoo"))
    b = _build()
    # Its HEAD would be the platform's — which says nothing about the library.
    assert not (b.manifest["code_commits"].get("modelzoo") or {}).get("commit")
    assert not any(str(i["ref"]).startswith("modelzoo@") for i in b.manifest["inputs"])


def test_dirty_modelzoo_is_recorded_dirty(env, tmp_path, monkeypatch):
    mz = tmp_path / "zoo"
    _init_repo(mz, {"lib.py": "v = 1\n"})
    (mz / "lib.py").write_text("v = 99\n")
    monkeypatch.setenv("EXAMLOPS_MODELZOO_DIR", str(mz))
    assert _build().manifest["code_commits"]["modelzoo"]["dirty"] is True


# ── hardware ─────────────────────────────────────────────────────────────────────────────────


def _fake_nvidia(tmp_path: Path, body: str, rc: int = 0) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "nvidia-smi"
    exe.write_text(f"#!/bin/sh\nprintf '%s' '{body}'\nexit {rc}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return bindir


def test_hardware_records_gpus_from_nvidia_smi(env, tmp_path, monkeypatch):
    bindir = _fake_nvidia(tmp_path, "NVIDIA A100-SXM4-40GB, 40960, 550.54\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    hw = _build().manifest["hardware"]
    assert hw["gpu_probe"] == "ok"
    assert hw["gpus"] == [
        {"name": "NVIDIA A100-SXM4-40GB", "memory_mib": "40960", "driver": "550.54"}
    ]
    assert hw["cpu_count"] == os.cpu_count() and hw["machine"] == os.uname().machine


def test_no_nvidia_smi_is_unavailable_not_zero_gpus(env, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    hw = _build().manifest["hardware"]
    assert hw["gpu_probe"] == "unavailable" and hw["gpus"] == []


def test_a_failing_nvidia_smi_is_an_error_status(env, tmp_path, monkeypatch):
    bindir = _fake_nvidia(tmp_path, "boom", rc=9)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    assert _build().manifest["hardware"]["gpu_probe"] == "error"


def test_explicit_hardware_wins_over_collection(env):
    assert _build(hardware={"note": "given"}).manifest["hardware"] == {"note": "given"}


def test_hardware_difference_is_described():
    from examlops.reproducibility.capture import describe_hardware_difference

    a = {"machine": "x86_64", "gpu_probe": "ok", "gpus": [{"name": "A100"}]}
    assert describe_hardware_difference(a, dict(a)) is None
    b = {"machine": "aarch64", "gpu_probe": "ok", "gpus": [{"name": "H100"}]}
    diff = describe_hardware_difference(a, b)
    assert "x86_64 -> aarch64" in diff and "H100" in diff
    assert "cannot probe" in describe_hardware_difference(a, {"machine": "x86_64"})


# ── image digest ─────────────────────────────────────────────────────────────────────────────


def test_image_digest_comes_from_the_environment(env, monkeypatch):
    digest = "ghcr.io/x/examlops-agent@sha256:" + "a" * 64
    monkeypatch.setenv("EXAMLOPS_IMAGE_DIGEST", digest)
    assert _build().manifest["environment"]["image_digest"] == digest


def test_a_malformed_image_digest_is_not_recorded(env, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_IMAGE_DIGEST", "latest")
    assert _build().manifest["environment"]["image_digest"] is None


# ── scheduler resources + lineage ────────────────────────────────────────────────────────────


def test_resources_capture_keeps_only_scheduler_neutral_keys():
    from examlops.reproducibility.capture import capture_resources, resource_env

    r = capture_resources(
        {"nodes": 2, "gpus": "4", "job_name": "x", "mem": None}, scheduler="flux", job_id="f1"
    )
    assert r == {"requested": {"nodes": "2", "gpus": "4"}, "scheduler": "flux", "job_id": "f1"}
    assert resource_env(r) == {"EXAMLOPS_HPC_NODES": "2", "EXAMLOPS_HPC_GPUS": "4"}


def test_lineage_run_is_an_input_and_its_purge_is_rot(env):
    from examlops.data.events import record_lineage_event

    record_lineage_event("run-abc", "train:m", "COMPLETE", mlflow_run_id="ml-1", model="m")
    b = _build(lineage_run_id="run-abc")
    assert b.manifest["lineage_run_id"] == "run-abc"
    assert _verify().reproducible
    b2 = _build(lineage_run_id="never-emitted")
    assert b2.manifest["lineage_run_id"] == "never-emitted"
    v = _verify()
    assert not v.reproducible and any("lineage run no longer recorded" in p for p in v.problems)


def test_auto_bundle_finds_lineage_and_records_resources(env):
    from examlops import platform_db
    from examlops.data.events import record_lineage_event
    from examlops.reproducibility import auto
    from examlops.reproducibility.capture import capture_resources

    record_lineage_event("flow-run-7", "train:m", "OTHER", mlflow_run_id="ml-7", model="m")
    auto.auto_bundle(
        "m",
        "1",
        trigger="training",
        force=True,
        mlflow_run_id="ml-7",
        resources=capture_resources({"gpus": 2}, scheduler="slurm", job_id="42"),
    )
    m = platform_db.get_repro_bundle("m", "1")["manifest"]
    assert m["lineage_run_id"] == "flow-run-7"
    assert m["resources"] == {"requested": {"gpus": "2"}, "scheduler": "slurm", "job_id": "42"}
    # A slurm job ran on a compute node, not on this (submitting) host: its hardware is stated as
    # not captured, never recorded as this host's.
    assert m["hardware"]["captured"] is False and m["hardware"]["job_id"] == "42"
    assert "gpu_probe" not in m["hardware"]


def test_a_mock_scheduler_bundle_probes_the_host_that_trained(env):
    from examlops import platform_db
    from examlops.reproducibility import auto
    from examlops.reproducibility.capture import capture_resources

    auto.auto_bundle(
        "m", "1", trigger="training", force=True,
        resources=capture_resources(None, scheduler="mock", job_id="mock-1"),
    )  # fmt: skip
    hw = platform_db.get_repro_bundle("m", "1")["manifest"]["hardware"]
    assert hw["gpu_probe"] in ("ok", "unavailable", "error") and "captured" not in hw


def test_promotion_bundle_does_not_claim_the_promoting_hosts_hardware(env):
    from examlops import platform_db
    from examlops.reproducibility import auto

    auto.auto_bundle("m", "1", trigger="promote", force=True)
    hw = platform_db.get_repro_bundle("m", "1")["manifest"]["hardware"]
    assert hw["captured"] is False and "promotion" in hw["reason"]


def test_pipeline_hook_records_the_submitted_request(env, monkeypatch):
    pg = pytest.importorskip("pipelines.pipeline_generator")
    from examlops import platform_db

    monkeypatch.setattr(pg, "_lineage_model_id", lambda name: name.lower())
    monkeypatch.setenv("EXAMLOPS_REPRO_AUTO_BUNDLE", "1")
    monkeypatch.setenv("EXAMLOPS_HPC_GPUS", "2")
    monkeypatch.setenv("EXAMLOPS_HPC_PARTITION", "gpu")
    pg._auto_repro_bundle(
        "JPCP",
        "PM100Dataset",
        {"version": "3", "run_id": ""},
        {"rmse": 3.0},
        None,
        seed=None,
        is_dummy=True,
        job_id="flux-99",
        scheduler="flux",
    )
    res = platform_db.get_repro_bundle("jpcp", "3")["manifest"]["resources"]
    assert res["scheduler"] == "flux" and res["job_id"] == "flux-99"
    assert res["requested"]["gpus"] == "2" and res["requested"]["partition"] == "gpu"
    # The mock scheduler trains inline: it requested nothing, and the bundle says so.
    pg._auto_repro_bundle(
        "JPCP", "PM100Dataset", {"version": "4"}, {}, None, seed=None, is_dummy=True,
        job_id="mock-1", scheduler="mock",
    )  # fmt: skip
    res = platform_db.get_repro_bundle("jpcp", "4")["manifest"]["resources"]
    assert res == {"requested": {}, "scheduler": "mock", "job_id": "mock-1"}


# ── feature views ────────────────────────────────────────────────────────────────────────────


def test_feature_views_are_pinned_by_definition_and_drift_is_rot(env):
    from examlops.data.data_assets import upsert_feature_view

    upsert_feature_view("jobs", "job_id", ["pclass", "mbwidth"], source="pm100.parquet")
    b = _build(feature_views=["jobs"])
    fv = b.manifest["feature_views"]["jobs"]
    assert len(fv["version"]) == 64
    assert {"kind": "feature_view", "ref": "jobs", "hash": fv["version"]} in b.manifest["inputs"]
    assert _verify().reproducible
    upsert_feature_view("jobs", "job_id", ["pclass"], source="pm100.parquet")
    v = _verify()
    assert not v.reproducible and any("definition changed" in p for p in v.problems)


def test_an_unknown_feature_view_is_refused(env):
    with pytest.raises(KeyError, match="nope"):
        _build(feature_views=["nope"])


def test_feature_views_from_the_environment(env, monkeypatch):
    from examlops.data.data_assets import upsert_feature_view

    upsert_feature_view("a", "e", ["x"])
    upsert_feature_view("b", "e", ["y"])
    monkeypatch.setenv("EXAMLOPS_FEATURE_VIEWS", "a, b")
    assert sorted(_build().manifest["feature_views"]) == ["a", "b"]


# ── BOM ──────────────────────────────────────────────────────────────────────────────────────


def test_a_bom_is_generated_when_none_exists_and_its_change_is_rot(env):
    from examlops.data.registry import get_model_bom, store_model_bom
    from examlops.reproducibility.capture import canonical_sha256

    b = _build()
    stored = get_model_bom("m", "1")
    assert stored is not None and stored["bomFormat"] == "CycloneDX"
    assert b.manifest["bom"]["source"] == "bundle"
    assert b.manifest["bom"]["sha256"] == canonical_sha256(stored)
    # its components are the bundle's own recorded package set, not a hand-picked five
    names = {c["name"] for c in stored["components"] if c["type"] == "library"}
    assert set(b.manifest["environment"]["packages"]) <= names
    assert _verify().reproducible
    store_model_bom("m", "1", {**stored, "components": []})
    v = _verify()
    assert not v.reproducible and "bom: AI-BOM changed" in v.problems


def test_an_existing_bom_is_linked_not_overwritten(env):
    from examlops.data.registry import get_model_bom, store_model_bom

    original = {"bomFormat": "CycloneDX", "components": [{"type": "data", "name": "d"}]}
    store_model_bom("m", "1", original)
    b = _build()
    assert b.manifest["bom"]["source"] == "existing"
    assert get_model_bom("m", "1") == original


def test_bom_can_be_opted_out(env):
    from examlops.data.registry import get_model_bom

    b = _build(bom=False)
    assert b.manifest["bom"] is None and get_model_bom("m", "1") is None


# ── clause 4: D1 technical file + D2 coverage ────────────────────────────────────────────────


def test_d1_technical_file_carries_a_verified_bundle(env):
    from examlops.compliance import generate_technical_file

    doc = generate_technical_file("m")
    sec = next(s for s in doc.sections if s.key == "reproducibility")
    assert sec.present is False and sec.status == "missing"
    _build(metrics={"rmse": 1.0})
    sec = next(s for s in generate_technical_file("m").sections if s.key == "reproducibility")
    assert sec.present is True
    assert (
        "m/1" in sec.content and "signed" in sec.content and "All referenced inputs" in sec.content
    )
    # repro_bundles sits outside the audit hash chain: named, not passed off as tamper-evident.
    assert sec.status == "unverified"


def test_d1_flags_a_rotted_bundle_as_a_gap(env):
    from examlops.compliance import generate_technical_file
    from examlops.data.registry import store_model_bom

    _build()
    store_model_bom("m", "1", {"tampered": True})
    sec = next(s for s in generate_technical_file("m").sections if s.key == "reproducibility")
    assert sec.present is False and "NON-reproducible" in sec.content and "AI-BOM" in sec.content


def test_d1_finds_a_bundle_keyed_by_the_lowercase_mlflow_id(env):
    from examlops.compliance import generate_technical_file
    from examlops.reproducibility import build_bundle

    build_bundle("jpcp", "5")
    sec = next(s for s in generate_technical_file("JPCP").sections if s.key == "reproducibility")
    assert sec.present is True and "jpcp/5" in sec.content


def test_d2_measure_2_1_is_satisfied_only_with_a_bundle(env):
    from examlops.governance import governance_report

    def status():
        rep = governance_report(model="m")
        return next(c for c in rep.controls if c.control.id == "MEASURE-2.1").status

    assert status() == "gap"
    _build()
    assert status() == "satisfied"


def test_technical_evidence_names_commits_and_bom(env, tmp_path, monkeypatch):
    from examlops.reproducibility import technical_evidence

    mz = tmp_path / "zoo"
    sha = _init_repo(mz, {"lib.py": "1\n"})
    monkeypatch.setenv("EXAMLOPS_MODELZOO_DIR", str(mz))
    b = _build()
    ev = technical_evidence("m", "1")
    assert ev["code_commits"]["modelzoo"] == sha
    assert ev["bom_sha256"] == b.manifest["bom"]["sha256"]
    assert ev["bundle_version"] == 1 and ev["reproducible"] in (True, False)
    assert json.dumps(ev)  # serialisable for the evidence pack


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def test_cli_build_records_resources_and_refuses_unknown_view(env):
    from typer.testing import CliRunner

    from examlops import platform_db
    from examlops.cli.commands.reproduce_cmd import app

    r = CliRunner().invoke(
        app,
        ["build", "m", "1", "--resources", '{"gpus": 2, "nodes": 1}', "--scheduler", "flux"],
    )
    assert r.exit_code == 0, r.output
    res = platform_db.get_repro_bundle("m", "1")["manifest"]["resources"]
    assert res == {"requested": {"gpus": "2", "nodes": "1"}, "scheduler": "flux"}
    r = CliRunner().invoke(app, ["build", "m", "2", "--feature-view", "ghost"])
    assert r.exit_code == 1 and "ghost" in r.output
    assert platform_db.get_repro_bundle("m", "2") is None


def test_cli_build_does_not_disguise_an_unrelated_keyerror(env, monkeypatch):
    """Only an unknown feature view is a clean refusal; any other KeyError is a bug and must
    surface as one, not as "Cannot build bundle" with exit 1."""
    from typer.testing import CliRunner

    import examlops.reproducibility as repro
    from examlops.cli.commands.reproduce_cmd import app

    def boom(*a, **k):
        raise KeyError("manifest_hash")

    monkeypatch.setattr(repro, "build_bundle", boom)
    r = CliRunner().invoke(app, ["build", "m", "1"])
    assert isinstance(r.exception, KeyError) and "Cannot build bundle" not in r.output


# ── review fixes (adversarial pass) ──────────────────────────────────────────────────────────


def test_verify_warns_when_the_model_library_was_dirty(env, tmp_path, monkeypatch):
    """A dirty modelzoo means its recorded commit is not the library that ran — verify must say
    so, exactly as it does for a dirty platform checkout."""
    mz = tmp_path / "zoo"
    _init_repo(mz, {"lib.py": "v = 1\n"})
    (mz / "lib.py").write_text("v = 99\n")
    monkeypatch.setenv("EXAMLOPS_MODELZOO_DIR", str(mz))
    _build(bom=False)
    assert any("modelzoo was dirty" in w for w in _verify().warnings)


def test_a_legacy_feature_view_counter_is_kept_but_never_made_a_rotting_input(env):
    """The pre-ADR-0038 ``{"view": 3}`` form can never equal a definition hash: turning it into a
    verified input made every later verify report the bundle as rotted."""
    from examlops.data.data_assets import upsert_feature_view

    upsert_feature_view("jobs", "job_id", ["pclass"], source="pm100.parquet")
    b = _build(feature_views={"jobs": 3}, bom=False)
    assert b.manifest["feature_views"] == {"jobs": 3}
    assert not any(i["kind"] == "feature_view" for i in b.manifest["inputs"])
    assert _verify().reproducible


def test_a_promotion_bundle_does_not_record_the_promoting_hosts_image(env, monkeypatch):
    """At promotion the process's own EXAMLOPS_IMAGE_DIGEST is the promoter's image (CLI,
    control plane), not the one that trained; recording it makes --execute demand it."""
    from examlops import platform_db
    from examlops.reproducibility import auto

    digest = "sha256:" + "a" * 64
    monkeypatch.setenv("EXAMLOPS_IMAGE_DIGEST", digest)
    auto.auto_bundle("m", "1", trigger="promote", force=True)
    assert platform_db.get_repro_bundle("m", "1")["manifest"]["environment"]["image_digest"] is None
    # ...while a bundle built on the host that trained still records it.
    auto.auto_bundle("m", "2", trigger="training", force=True)
    assert platform_db.get_repro_bundle("m", "2")["manifest"]["environment"]["image_digest"] == (
        digest
    )


def test_the_newest_bundle_is_deterministic_when_created_at_ties(env):
    """created_at has one-second resolution: bundles built in the same second tied, and the
    Annex-IV reproducibility section cited whichever row the query plan returned first."""
    from examlops.data.data_assets import get_db, list_repro_bundles
    from examlops.reproducibility import build_bundle

    for ver in ("1", "2", "3"):
        build_bundle("m", ver, bom=False)
    with get_db() as conn:  # the tie the clock produces in production, made certain
        conn.execute("UPDATE repro_bundles SET created_at='2026-09-25 00:00:00'")
    assert [r["version"] for r in list_repro_bundles("m")] == ["3", "2", "1"]
