"""ADR 0038 — automatic bundles, dataplane dataset verification, package-level env verification."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TRAIN = "import json\nprint('EXAMLOPS_REPRO_METRICS=' + json.dumps({'rmse': 5.0}))\n"


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k")
    monkeypatch.delenv("EXAMLOPS_REPRO_AUTO_BUNDLE", raising=False)
    monkeypatch.delenv("EXAMLOPS_SEED", raising=False)
    from examlops import platform_db
    from examlops.data.audit import reset_dropped_audit_events
    from examlops.reproducibility import auto

    platform_db.init_db()
    auto.reset_failures()
    reset_dropped_audit_events()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "uv.lock").write_text("lock-a\n")
    (repo / "train.py").write_text(TRAIN)
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "init")
    monkeypatch.chdir(repo)
    yield repo
    reset_dropped_audit_events()
    auto.reset_failures()


# ── automatic creation ───────────────────────────────────────────────────────────────────


def test_off_by_default_builds_nothing(env):
    from examlops import platform_db
    from examlops.reproducibility import auto

    assert auto.enabled() is False
    assert auto.auto_bundle("m", "1", trigger="training", metrics={"rmse": 1.0}) is None
    assert platform_db.get_repro_bundle("m", "1") is None


def test_armed_builds_a_complete_bundle(env, monkeypatch):
    from examlops import platform_db
    from examlops.reproducibility import auto

    monkeypatch.setenv("EXAMLOPS_REPRO_AUTO_BUNDLE", "1")
    b = auto.auto_bundle(
        "m",
        3,
        trigger="training",
        metrics={"rmse": 4.5, "bad": float("nan"), "txt": "x"},
        dataset_name="PM100",
        dataset_revision="rev1",
        seed=7,
        run_spec={"registry_model": "M", "dataset": "PM100", "backend": None},
    )
    assert b is not None
    m = platform_db.get_repro_bundle("m", "3")["manifest"]
    assert m["code_commit"] == _git(env, "rev-parse", "HEAD")
    assert m["code_dirty"] is False
    assert m["environment"]["lock_sha256"]
    assert m["environment"]["packages"] and "pytest" in m["environment"]["packages"]
    assert m["seeds"] == {"global": 7}
    assert m["metrics"] == {"rmse": 4.5}  # non-finite / non-numeric never recorded
    assert m["dataset_revision"] == "rev1"
    assert m["trigger"] == "training"
    assert m["run_spec"]["registry_model"] == "M"
    assert m["tolerance"]


def test_a_dirty_tree_is_recorded_not_hidden(env, monkeypatch):
    from examlops import platform_db
    from examlops.reproducibility import auto

    (env / "train.py").write_text(TRAIN + "# edited\n")
    monkeypatch.setenv("EXAMLOPS_REPRO_AUTO_BUNDLE", "1")
    auto.auto_bundle("m", "1", trigger="training", metrics={"rmse": 1.0})
    assert platform_db.get_repro_bundle("m", "1")["manifest"]["code_dirty"] is True


def test_no_version_means_no_bundle(env, monkeypatch):
    from examlops.reproducibility import auto

    monkeypatch.setenv("EXAMLOPS_REPRO_AUTO_BUNDLE", "1")
    assert auto.auto_bundle("m", None, trigger="training") is None
    assert auto.failures() == 0


def test_a_bundle_failure_is_counted_audited_and_never_raises(env, monkeypatch):
    from examlops import platform_db
    from examlops.reproducibility import auto

    monkeypatch.setenv("EXAMLOPS_REPRO_AUTO_BUNDLE", "1")

    def boom(*a, **k):
        raise RuntimeError("bundle store down")

    monkeypatch.setattr(platform_db, "store_repro_bundle", boom)
    assert auto.auto_bundle("m", "1", trigger="training", metrics={"rmse": 1.0}) is None
    assert auto.failures() == 1
    with platform_db.get_db() as conn:
        rows = conn.execute(
            "SELECT details FROM audit_events WHERE action='repro_auto_bundle_failed'"
        ).fetchall()
    assert len(rows) == 1 and "bundle store down" in rows[0][0]


def test_a_lost_failure_audit_is_counted_too(env, monkeypatch):
    from examlops import platform_db
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events
    from examlops.reproducibility import auto

    monkeypatch.setenv("EXAMLOPS_REPRO_AUTO_BUNDLE", "1")

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(platform_db, "store_repro_bundle", boom)
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    assert auto.auto_bundle("m", "1", trigger="training") is None  # still fails open
    assert auto.failures() == 1
    assert dropped_audit_events()


def test_a_lost_bundle_built_audit_is_counted(env, monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events
    from examlops.reproducibility import build_bundle

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    assert build_bundle("m", "1", metrics={"rmse": 1.0}).bundle_version == 1
    assert dropped_audit_events()


def test_promote_hook_is_idempotent(env, monkeypatch):
    from examlops import platform_db
    from examlops.reproducibility import auto

    monkeypatch.setenv("EXAMLOPS_REPRO_AUTO_BUNDLE", "1")
    assert auto.auto_bundle("m", "1", trigger="training", metrics={"rmse": 1.0}) is not None
    assert (
        auto.auto_bundle("m", "1", trigger="promote", metrics={"rmse": 9.0}, skip_if_exists=True)
        is None
    )
    row = platform_db.get_repro_bundle("m", "1")
    assert row["bundle_version"] == 1 and row["manifest"]["trigger"] == "training"


def test_cli_promote_helper_off_and_armed(env, monkeypatch):
    from examlops import platform_db
    from examlops.cli.commands.pipeline import _auto_bundle_on_promote

    run = {
        "run": {
            "data": {
                "tags": [
                    {"key": "dataset_revision", "value": "unknown"},
                ],
                "params": [{"key": "dataset", "value": "PM100"}],
            }
        }
    }
    _auto_bundle_on_promote("jpcp", "5", {"rmse": 2.0}, run)
    assert platform_db.get_repro_bundle("jpcp", "5") is None  # off ⇒ nothing
    monkeypatch.setenv("EXAMLOPS_REPRO_AUTO_BUNDLE", "1")
    _auto_bundle_on_promote("jpcp", "5", {"rmse": 2.0}, run)
    m = platform_db.get_repro_bundle("jpcp", "5")["manifest"]
    assert m["trigger"] == "promote" and m["dataset_revision"] is None  # 'unknown' never pinned
    assert m["dataset_name"] == "PM100"


def test_training_flow_hook_records_registry_key_and_never_raises(env, monkeypatch):
    pg = pytest.importorskip("pipelines.pipeline_generator")
    from examlops import platform_db

    monkeypatch.setattr(pg, "_lineage_model_id", lambda name: name.lower())
    monkeypatch.setenv("EXAMLOPS_REPRO_AUTO_BUNDLE", "1")
    pg._auto_repro_bundle(
        "JPCP",
        "PM100Dataset",
        {"version": "2", "run_id": ""},
        {"rmse": 3.0},
        None,
        seed=None,
        is_dummy=True,
    )
    m = platform_db.get_repro_bundle("jpcp", "2")["manifest"]
    assert m["run_spec"] == {
        "registry_model": "JPCP",
        "dataset": "PM100Dataset",
        "backend": None,
        "dummy": True,
    }
    assert m["dataset_revision"] is None  # dummy run pins nothing
    # an unregistered run: nothing to key a bundle to, and no exception
    pg._auto_repro_bundle(
        "JPCP", "PM100Dataset", {"version": None}, {}, None, seed=None, is_dummy=True
    )
    # a broken helper cannot fail the run
    monkeypatch.setattr(pg, "_lineage_model_id", lambda n: (_ for _ in ()).throw(ValueError("x")))
    pg._auto_repro_bundle(
        "JPCP", "PM100Dataset", {"version": "9"}, {}, None, seed=None, is_dummy=True
    )


def test_apply_seed(monkeypatch):
    import random

    from examlops.reproducibility import auto

    monkeypatch.delenv("EXAMLOPS_SEED", raising=False)
    assert auto.apply_seed() is None
    monkeypatch.setenv("EXAMLOPS_SEED", "nope")
    assert auto.apply_seed() is None
    monkeypatch.setenv("EXAMLOPS_SEED", "11")
    assert auto.apply_seed() == 11
    a = random.random()
    assert auto.apply_seed() == 11
    assert random.random() == a


# ── dataplane dataset verification ───────────────────────────────────────────────────────


def _snapshot(tmp_path):
    from examlops.dataplane import store as st
    from examlops.dataplane.types import Limits, TableBatch

    root = tmp_path / "dpstore"
    store = st.DatasetStore.from_url(f"file://{root}")
    w = st.SnapshotWriter(tmp_path / "stage", limits=Limits())
    w.write(
        TableBatch(
            "jobs",
            pa.RecordBatch.from_pylist([{"id": i, "p": i * 1.5} for i in range(5)]),
            {"column": "id", "value": 4, "type": "int"},
        )
    )
    manifest, _ = st.publish(
        store,
        "_global/pm100",
        staged=w.close(),
        parent=None,
        connector="sql",
        connection="lab",
        spec_hash="h",
        watermark=w.watermark,
        pull_id="p1",
        incremental=False,
    )
    return f"file://{root}", root, manifest


def _dp_bundle(url, manifest, **kw):
    from examlops.reproducibility import build_bundle

    return build_bundle(
        "M",
        "1",
        dataset_name="PM100Dataset",
        dataset_revision=manifest.revision,
        dataset_source={"kind": "dataplane", "source_key": "_global/pm100"},
        metrics={"rmse": 5.0},
        **kw,
    )


def test_dataplane_snapshot_verifies_and_rot_is_caught(env, tmp_path, monkeypatch):
    from examlops.reproducibility import verify_bundle

    url, root, manifest = _snapshot(tmp_path)
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", url)
    _dp_bundle(url, manifest)
    ok = verify_bundle("M", "1")
    ds = next(i for i in ok.inputs if i["kind"] == "dataset")
    assert ds["ok"] and ok.reproducible, ok.problems
    # the dataset_revisions table has NO row for it — verification came from the store
    from examlops import platform_db

    assert platform_db.get_dataset_revision("PM100Dataset", manifest.revision) is None
    # corrupt one stored part: the same revision id now fails, it is not claimed ok
    part = next(root.rglob("part-00000.parquet"))
    part.write_bytes(part.read_bytes() + b"x")
    bad = verify_bundle("M", "1")
    assert not bad.reproducible
    assert any("does not verify" in p for p in bad.problems), bad.problems


def test_dataplane_snapshot_missing_or_store_unreachable_fails(env, tmp_path, monkeypatch):
    from examlops.reproducibility import verify_bundle

    url, root, manifest = _snapshot(tmp_path)
    _dp_bundle(url, manifest)
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", f"file://{tmp_path / 'empty'}")
    r = verify_bundle("M", "1")
    assert not r.reproducible and any("dataset" in p for p in r.problems)


def test_execute_dataset_step_verifies_snapshot_even_under_dummy(env, tmp_path, monkeypatch):
    from examlops.reproducibility.execute import execute_reproduction

    url, root, manifest = _snapshot(tmp_path)
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", url)
    _dp_bundle(url, manifest)
    res = execute_reproduction(
        "M",
        "1",
        repo=env,
        dummy=True,
        allow_env_drift=True,
        train_cmd=[sys.executable, "train.py"],
    )
    st = {s.step: s for s in res.steps}
    assert st["dataset"].status == "ok" and "verified against manifest" in st["dataset"].detail
    assert res.ok, [(s.step, s.status, s.detail) for s in res.steps]
    part = next(root.rglob("part-00000.parquet"))
    part.write_bytes(part.read_bytes() + b"x")
    res = execute_reproduction("M", "1", repo=env, dummy=True, train_cmd=[sys.executable, "x"])
    st = {s.step: s.status for s in res.steps}
    assert st["dataset"] == "failed" and st["train"] == "not_run" and not res.ok


def test_cli_verify_exit_1_on_dataplane_mismatch(env, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    url, root, manifest = _snapshot(tmp_path)
    monkeypatch.setenv("EXAMLOPS_DATAPLANE_STORE_URL", url)
    _dp_bundle(url, manifest)
    r = CliRunner().invoke(app, ["--json", "reproduce", "verify", "M", "1"])
    assert r.exit_code == 0, r.output
    part = next(root.rglob("part-00000.parquet"))
    part.write_bytes(part.read_bytes() + b"x")
    r = CliRunner().invoke(app, ["--json", "reproduce", "verify", "M", "1"])
    assert r.exit_code == 1
    assert any("does not verify" in p for p in json.loads(r.stdout)["problems"])


# ── package-level environment verification ───────────────────────────────────────────────


def _build_with_packages(monkeypatch, pkgs):
    import examlops.reproducibility as rep

    real = rep.capture_packages
    monkeypatch.setattr(rep, "capture_packages", lambda: pkgs)
    rep.build_bundle("M", "1", metrics={"rmse": 5.0}, seeds={"global": 1})
    monkeypatch.setattr(rep, "capture_packages", real)


def test_package_set_is_recorded_and_compared(env):
    from examlops.reproducibility import capture_packages, compare_packages

    pk = capture_packages()
    assert pk and all(isinstance(v, str) for v in pk.values()) and list(pk) == sorted(pk)
    assert not compare_packages(pk)["drift"]
    cmp = compare_packages({**pk, "numpy": "0.0.1", "ghost-pkg": "1"})
    assert cmp["changed"]["numpy"][0] == "0.0.1" and cmp["missing"] == ["ghost-pkg"]
    assert cmp["drift"]
    # an extra installed package is reported, not drift
    assert compare_packages({}, {"x": "1"}) == {
        "changed": {},
        "missing": [],
        "added": ["x"],
        "drift": False,
    }


def test_verify_reports_package_drift_per_package(env, monkeypatch):
    from examlops.reproducibility import capture_packages, verify_bundle

    cur = capture_packages()
    _build_with_packages(monkeypatch, {**cur, "numpy": "0.0.1", "ghost-pkg": "9"})
    r = verify_bundle("M", "1")
    assert not r.reproducible
    msg = next(p for p in r.problems if p.startswith("env_packages"))
    assert "numpy 0.0.1->" in msg and "ghost-pkg missing" in msg
    ok = verify_bundle("M", "1", allow_env_drift=True)
    assert ok.reproducible and any("env packages" in w for w in ok.warnings)


def test_execute_env_step_fails_on_package_drift_unless_allowed(env, monkeypatch):
    from examlops.reproducibility import capture_packages
    from examlops.reproducibility.execute import execute_reproduction

    _build_with_packages(monkeypatch, {**capture_packages(), "numpy": "0.0.1"})
    res = execute_reproduction("M", "1", repo=env, train_cmd=[sys.executable, "train.py"])
    st = {s.step: s for s in res.steps}
    assert st["env"].status == "failed" and "numpy 0.0.1->" in st["env"].detail
    assert st["train"].status == "not_run"
    res = execute_reproduction(
        "M", "1", repo=env, allow_env_drift=True, train_cmd=[sys.executable, "train.py"]
    )
    st = {s.step: s.status for s in res.steps}
    assert st["env"] == "drift_allowed" and res.ok


def test_execute_env_step_ok_reports_packages_compared(env):
    from examlops.reproducibility import build_bundle
    from examlops.reproducibility.execute import execute_reproduction

    build_bundle("M", "1", metrics={"rmse": 5.0})
    res = execute_reproduction("M", "1", repo=env, train_cmd=[sys.executable, "train.py"])
    env_step = next(s for s in res.steps if s.step == "env")
    assert env_step.status == "ok" and "packages compared" in env_step.detail
    assert res.ok, [(s.step, s.detail) for s in res.steps]


def test_a_legacy_bundle_without_packages_still_verifies(env):
    from examlops import platform_db
    from examlops.reproducibility import _canonical_hash, verify_bundle

    m = {"model": "M", "version": "1", "inputs": [], "environment": {}, "metrics": {}}
    platform_db.store_repro_bundle("M", "1", m, _canonical_hash(m))
    assert verify_bundle("M", "1").reproducible


# ── dirty code ───────────────────────────────────────────────────────────────────────────


def test_execute_refuses_a_dirty_bundle_unless_allowed(env):
    from examlops.reproducibility import build_bundle
    from examlops.reproducibility.execute import execute_reproduction

    (env / "train.py").write_text(TRAIN + "# edit\n")
    b = build_bundle("M", "1", metrics={"rmse": 5.0})
    assert b.manifest["code_dirty"] is True
    (env / "train.py").write_text(TRAIN)
    res = execute_reproduction(
        "M", "1", repo=env, allow_env_drift=True, train_cmd=[sys.executable, "train.py"]
    )
    assert res.steps[0].status == "failed" and "dirty" in res.steps[0].detail
    res = execute_reproduction(
        "M",
        "1",
        repo=env,
        allow_env_drift=True,
        allow_dirty_code=True,
        train_cmd=[sys.executable, "train.py"],
    )
    assert res.ok


def test_verify_warns_on_dirty_bundle(env):
    from examlops.reproducibility import build_bundle, verify_bundle

    (env / "train.py").write_text(TRAIN + "# edit\n")
    build_bundle("M", "1", metrics={"rmse": 5.0})
    r = verify_bundle("M", "1")
    assert any("dirty" in w for w in r.warnings)


# ── default execute path ─────────────────────────────────────────────────────────────────


def test_default_train_path_calls_the_real_flow_in_isolation(env, monkeypatch):
    """Without --train-cmd the driver runs training_flow with the bundle's registry key, and the
    rebuild is pointed at a throw-away MLflow store + platform DB (never the live ones)."""
    from examlops.reproducibility import build_bundle
    from examlops.reproducibility.execute import _DRIVER, _run_train

    b = build_bundle(
        "jpcp",
        "1",
        metrics={"rmse": 5.0},
        dataset_name="PM100Dataset",
        run_spec={"registry_model": "JPCP", "dataset": "PM100Dataset", "backend": "minio"},
    )
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://live-mlflow:5000")
    monkeypatch.setenv("PLATFORM_DB", "/live/platform.db")
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0, 'EXAMLOPS_REPRO_METRICS={"rmse": 5}\n', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    step, metrics = _run_train(
        b.manifest, env, dummy=True, train_cmd=None, timeout=5, scratch=env.parent
    )
    assert step.status == "ok" and metrics == {"rmse": 5.0}
    assert seen["cmd"][2] == _DRIVER and seen["cmd"][3:] == ["JPCP", "PM100Dataset", "1", "minio"]
    assert seen["env"]["MLFLOW_TRACKING_URI"].startswith("sqlite:///")
    assert seen["env"]["PLATFORM_DB"].startswith(str(env.parent))
    assert seen["env"]["EXAMLOPS_REPRO_AUTO_BUNDLE"] == "0"


@pytest.mark.skipif(
    not __import__("os").getenv("EXAMLOPS_REPRO_LIVE"),
    reason="runs the real training_flow (~20-60 s); set EXAMLOPS_REPRO_LIVE=1",
)
def test_live_default_path_end_to_end(env, monkeypatch):
    """The real, unmocked path: bundle -> `--execute --dummy` with NO --train-cmd, against the
    repo checkout at HEAD. Opt-in because it trains a model (tens of seconds)."""
    from examlops.reproducibility import build_bundle
    from examlops.reproducibility.execute import execute_reproduction

    monkeypatch.chdir(REPO_ROOT)  # the bundle must record THIS repo's commit
    metrics = {"rmse": 145.0, "mape": 18.3, "mse": 21000.0}
    build_bundle(
        "jpcp",
        "1",
        metrics=metrics,
        dataset_name="PM100Dataset",
        seeds={"global": 1},
        run_spec={"registry_model": "JPCP", "dataset": "PM100Dataset", "backend": None},
    )
    res = execute_reproduction(
        "jpcp",
        "1",
        repo=REPO_ROOT,
        dummy=True,
        allow_env_drift=True,
        allow_dirty_code=True,
        rtol=0.25,
        timeout=600,
    )
    assert res.ok, [(s.step, s.status, s.detail) for s in res.steps]


# ── show ─────────────────────────────────────────────────────────────────────────────────


def test_show_command(env):
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.reproducibility import build_bundle

    build_bundle("M", "1", metrics={"rmse": 5.0})
    r = CliRunner().invoke(app, ["--json", "reproduce", "show", "M", "1"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["manifest"]["metrics"] == {"rmse": 5.0} and doc["signed"] is True
    assert isinstance(doc["manifest"]["environment"]["packages"], dict)
    r = CliRunner().invoke(app, ["--json", "reproduce", "show", "M", "99"])
    assert r.exit_code == 1
