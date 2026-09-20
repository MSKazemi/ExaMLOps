"""ADR 0038 clause 2 — `exa reproduce run --execute` (real rebuild, step by step)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))

TRAIN = (
    "import json, os, pathlib\n"
    "assert pathlib.Path('marker.txt').read_text().strip() == 'v1', 'wrong code'\n"
    "print('noise')\n"
    "print('EXAMLOPS_REPRO_METRICS=' + json.dumps({'rmse': 5.02, 'seed': "
    "float(os.environ['EXAMLOPS_SEED'])}))\n"
)


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k")
    from examlops import platform_db

    platform_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "uv.lock").write_text("lock-a\n")
    (repo / "marker.txt").write_text("v1\n")
    (repo / "train.py").write_text(TRAIN)
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "init")
    monkeypatch.chdir(repo)
    return repo


def _build(**kw):
    from examlops.reproducibility import build_bundle

    kw.setdefault("metrics", {"rmse": 5.0})
    kw.setdefault("seeds", {"global": 7})
    return build_bundle("M", "1", **kw)


def _run(repo, **kw):
    from examlops.reproducibility.execute import execute_reproduction

    kw.setdefault("train_cmd", [sys.executable, "train.py"])
    return execute_reproduction("M", "1", repo=repo, **kw)


def _status(res):
    return {s.step: s.status for s in res.steps}


def test_all_green_end_to_end(env):
    _build()
    # move HEAD on so the run must use the recorded sha, not HEAD
    (env / "marker.txt").write_text("v2\n")
    _git(env, "-c", "commit.gpgsign=false", "commit", "-qam", "later")
    res = _run(env)
    assert res.ok, [(s.step, s.status, s.detail) for s in res.steps]
    assert _status(res) == {
        "code": "ok",
        "dataset": "skipped",
        "env": "ok",
        "train": "ok",
        "compare": "ok",
    }
    assert res.produced_metrics["rmse"] == 5.02
    assert res.bit_exact is False
    assert _git(env, "worktree", "list").count("\n") == 0  # worktree cleaned up


def test_dataset_verified_against_real_data(env, tmp_path):
    import pandas as pd

    from examlops.data.data_assets import record_dataset_revision
    from pipelines.datasets import versioning

    d = tmp_path / "data"
    d.mkdir()
    pd.DataFrame({"a": [1, 2, 3]}).to_parquet(d / "x.parquet")
    rev = versioning.resolve_revision(None, "DS", data_path=str(d))
    record_dataset_revision(rev, declare_asset=False)
    _build(dataset_name="DS", dataset_revision=rev.revision_id)
    ok = _run(env, data_path=str(d))
    assert _status(ok)["dataset"] == "ok" and ok.ok
    pd.DataFrame({"a": [9]}).to_parquet(d / "x.parquet")  # data rotted
    bad = _run(env, data_path=str(d))
    assert _status(bad)["dataset"] == "failed"
    assert _status(bad)["train"] == "not_run" and not bad.ok


def test_dataset_revision_not_recorded_fails(env):
    _build(dataset_name="DS", dataset_revision="nope")
    res = _run(env)
    assert _status(res)["dataset"] == "failed" and not res.ok


def test_missing_commit_refuses_no_head_fallback(env):
    from examlops import platform_db
    from examlops.reproducibility import _canonical_hash

    b = _build()
    m = dict(b.manifest, code_commit="0" * 40)
    platform_db.store_repro_bundle("M", "2", m, _canonical_hash(m))
    from examlops.reproducibility.execute import execute_reproduction

    res = execute_reproduction("M", "2", repo=env, train_cmd=[sys.executable, "train.py"])
    assert _status(res)["code"] == "failed" and "not available" in res.steps[0].detail
    assert _status(res)["train"] == "not_run"


def test_no_recorded_commit_refuses(env):
    from examlops import platform_db
    from examlops.reproducibility import _canonical_hash

    b = _build()
    m = dict(b.manifest, code_commit=None)
    platform_db.store_repro_bundle("M", "3", m, _canonical_hash(m))
    from examlops.reproducibility.execute import execute_reproduction

    res = execute_reproduction("M", "3", repo=env)
    assert res.steps[0].status == "failed" and "HEAD" in res.steps[0].detail


def test_no_bundle_and_tampered_bundle_fail(env):
    from examlops.reproducibility.execute import execute_reproduction

    assert not execute_reproduction("X", "9", repo=env).ok
    _build()
    from examlops.data import get_db

    with get_db() as conn:
        conn.execute("UPDATE repro_bundles SET manifest_hash='bad' WHERE model='M'")
    res = _run(env)
    assert not res.ok and "tampered" in res.steps[0].detail


def test_env_drift_fails_unless_allowed(env):
    _build()
    (env / "uv.lock").write_text("lock-b\n")
    _git(env, "-c", "commit.gpgsign=false", "commit", "-qam", "lock")
    # recorded sha still has lock-a -> env ok; so build a bundle whose hash is wrong instead
    from examlops import platform_db
    from examlops.reproducibility import _canonical_hash

    m = dict(_build().manifest)
    m["environment"] = dict(m["environment"], lock_sha256="f" * 64)
    platform_db.store_repro_bundle("M", "1", m, _canonical_hash(m))
    res = _run(env)
    assert _status(res)["env"] == "failed" and _status(res)["train"] == "not_run"
    res2 = _run(env, allow_env_drift=True)
    assert _status(res2)["env"] == "drift_allowed" and res2.ok


def test_uncaptured_env_is_unverifiable(env):
    from examlops import platform_db
    from examlops.reproducibility import _canonical_hash

    m = dict(_build().manifest)
    m["environment"] = {"lock_sha256": None, "lock_path": None, "image_digest": None}
    platform_db.store_repro_bundle("M", "1", m, _canonical_hash(m))
    assert _status(_run(env))["env"] == "failed"


def test_training_failure_and_missing_marker(env):
    _build()
    res = _run(env, train_cmd=[sys.executable, "-c", "raise SystemExit(3)"])
    assert _status(res)["train"] == "failed" and "exit 3" in res.steps[3].detail
    res = _run(env, train_cmd=[sys.executable, "-c", "print('hi')"])
    assert _status(res)["train"] == "failed" and "no 'EXAMLOPS" in res.steps[3].detail
    res = _run(env, train_cmd=["definitely-not-a-binary"])
    assert _status(res)["train"] == "failed"


def test_metric_divergence_and_rtol(env):
    _build(metrics={"rmse": 4.0})
    bad = _run(env)
    assert _status(bad)["compare"] == "failed" and not bad.ok
    assert _run(env, rtol=0.5).ok  # 5.02 vs 4.0 = 20% < 50%


def test_missing_and_empty_metrics_fail(env):
    _build(metrics={"rmse": 5.0, "mape": 0.1})
    r = _run(env)
    assert _status(r)["compare"] == "failed" and "not produced" in r.steps[4].detail
    _build(metrics={})
    r = _run(env)
    assert _status(r)["compare"] == "failed" and "no metrics" in r.steps[4].detail


def test_cli_execute_exit_codes_and_json(env):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _build()
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["--json", "reproduce", "run", "M", "1", "--execute", "--train-cmd", "python train.py"],
    )
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["ok"] is True and [s["step"] for s in doc["steps"]][0] == "code"
    _build(metrics={"rmse": 1.0})
    r = runner.invoke(
        app,
        ["--json", "reproduce", "run", "M", "1", "--execute", "--train-cmd", "python train.py"],
    )
    assert r.exit_code == 1


def test_plan_output_unchanged_without_execute(env):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _build()
    r = CliRunner().invoke(app, ["--json", "reproduce", "run", "M", "1"])
    assert r.exit_code == 0
    assert "metric_match" in json.loads(r.stdout)


def test_a_lost_repro_audit_is_counted(env, monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    reset_dropped_audit_events()
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    _build()
    assert _run(env).ok  # fails open
    assert dropped_audit_events()
    reset_dropped_audit_events()
