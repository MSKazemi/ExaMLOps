"""E6 launcher logic (ADR 0032) that needs no torch: command building, classification, the
supervisor's resubmit loop / backoff / audit / no-false-resume claims, checkpoint verification."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.distributed import checkpoint_files as cf  # noqa: E402
from examlops.distributed import launch  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def _fake_checkpoint(run_dir: Path, step: int, world: int = 2, cfg: str = "c") -> None:
    d = cf.step_dir(run_dir, step)
    d.mkdir(parents=True, exist_ok=True)
    shards = []
    for r in range(world):
        data = f"shard{step}-{r}".encode()
        cf.atomic_write_bytes(d / cf.shard_name(r, world), data)
        shards.append(
            {
                "rank": r,
                "file": cf.shard_name(r, world),
                "sha256": cf.sha256_file(d / cf.shard_name(r, world)),
                "bytes": len(data),
            }
        )
    cf.write_manifest(
        run_dir, step=step, world_size=world, cfg_hash=cfg, shards=shards, resumed_from_step=None
    )


def test_command_is_real_torchrun_for_the_shipped_script():
    cmd = launch.build_torchrun_command(
        Path("/r"), nproc_per_node=2, steps=8, max_restarts=1, seed=3
    )
    assert "--standalone" in cmd and "--nproc-per-node=2" in cmd and "--max-restarts=1" in cmd
    script = next(c for c in cmd if c.endswith("train_ddp.py"))
    assert Path(script).is_file()  # not a placeholder
    assert "--run-dir=/r" in cmd and "--steps=8" in cmd and "--seed=3" in cmd


def test_multinode_command_needs_an_endpoint_and_is_string_only():
    with pytest.raises(ValueError):
        launch.build_torchrun_command(Path("/r"), nnodes=2)
    cmd = launch.build_torchrun_command(Path("/r"), nnodes=2, rdzv_endpoint="n1:29500")
    assert "--nnodes=2" in cmd and "--rdzv_endpoint=n1:29500" in cmd
    assert "--standalone" not in cmd


def test_missing_torch_is_a_clear_error_not_an_import_error(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "torch_available", lambda: False)
    with pytest.raises(launch.TorchNotInstalled, match="torch is not installed"):
        launch.supervise("r", tmp_path / "r")


def test_valid_checkpoint_then_corrupt_shard_is_refused(tmp_path):
    _fake_checkpoint(tmp_path, 2)
    _fake_checkpoint(tmp_path, 4)
    latest, skipped = cf.find_latest_valid(tmp_path, "c")
    assert latest and latest.step == 4 and not skipped
    victim = cf.step_dir(tmp_path, 4) / cf.shard_name(1, 2)
    victim.write_bytes(b"flipped!")
    latest, skipped = cf.find_latest_valid(tmp_path, "c")
    assert latest and latest.step == 2
    assert [s.step for s in skipped] == [4] and "corrupt" in skipped[0].reason


def test_missing_shard_manifestless_and_wrong_config_are_invalid(tmp_path):
    _fake_checkpoint(tmp_path, 2)
    _fake_checkpoint(tmp_path, 4)
    _fake_checkpoint(tmp_path, 6)
    (cf.step_dir(tmp_path, 6) / cf.shard_name(0, 2)).unlink()
    (cf.step_dir(tmp_path, 4) / cf.MANIFEST).unlink()  # shards written, never committed
    latest, skipped = cf.find_latest_valid(tmp_path, "c")
    assert latest.step == 2 and [s.step for s in skipped] == [6, 4]
    assert cf.find_latest_valid(tmp_path, "other-config")[0] is None


def test_tampered_manifest_is_invalid(tmp_path):
    _fake_checkpoint(tmp_path, 2)
    mp = cf.step_dir(tmp_path, 2) / cf.MANIFEST
    m = json.loads(mp.read_text())
    m["step"] = 2
    m["world_size"] = 1
    mp.write_text(json.dumps(m))
    assert not cf.verify_checkpoint_dir(cf.step_dir(tmp_path, 2)).valid


def test_metrics_line_parsing_takes_the_last_valid_one():
    text = 'noise\nEXAMLOPS_DIST_METRICS={"a":1}\nEXAMLOPS_DIST_METRICS=oops\n'
    assert cf.parse_metrics(text) == {"a": 1}
    assert cf.parse_metrics("nothing") is None


def _scripted(outcomes, run_dir, resumed=None):
    """A runner that writes each attempt's log like the script would, without any torch."""
    calls = []

    def runner(cmd, env, log, timeout):
        i = len(calls)
        calls.append(env["EXAMLOPS_DIST_ATTEMPT"])
        rc, kind = outcomes[i]
        lines = ["log"]
        if kind == "ok":
            m = {"status": "complete", "steps": 4, "config_hash": "c", "resumed_from_step": resumed}
            lines.append(cf.METRICS_MARKER + json.dumps(m))
        if kind == "fatal":
            (run_dir / cf.FATAL_MARKER).write_text("{}")
        log.write_text("\n".join(lines))
        return rc

    return runner, calls


def test_recoverable_failure_is_resubmitted_with_backoff_and_audited(tmp_path):
    from examlops import platform_db

    runner, calls = _scripted([(1, "kill"), (1, "kill"), (0, "ok")], tmp_path)
    sleeps = []
    res = launch.supervise(
        "sup1", tmp_path, runner=runner, max_attempts=3, backoff_s=2, sleep=sleeps.append
    )
    assert res.status == "complete" and calls == ["1", "2", "3"]
    assert [a.outcome for a in res.attempts] == ["recoverable", "recoverable", "success"]
    assert sleeps == [2, 4]  # exponential, and none after the last attempt
    assert res.resumed_from_step is None  # attempt 3 says nothing about resuming -> not claimed
    with platform_db.get_db() as conn:
        actions = [
            r[0] for r in conn.execute("SELECT action FROM audit_events WHERE target='sup1'")
        ]
    assert actions.count("distributed_attempt") == 3
    assert "distributed_launch" in actions and "distributed_complete" in actions
    assert actions.count("distributed_failed") == 2
    assert platform_db.get_distributed_run("sup1")["status"] == "complete"


def test_attempts_are_bounded(tmp_path):
    runner, calls = _scripted([(1, "kill")] * 5, tmp_path)
    res = launch.supervise("sup2", tmp_path, runner=runner, max_attempts=2, backoff_s=0)
    assert res.status == "failed" and len(calls) == 2


def test_a_fatal_failure_is_not_resubmitted(tmp_path):
    runner, calls = _scripted([(1, "fatal"), (0, "ok")], tmp_path)
    res = launch.supervise("sup3", tmp_path, runner=runner, max_attempts=3, backoff_s=0)
    assert res.status == "fatal" and calls == ["1"]


def test_rc0_without_the_completion_line_is_not_success(tmp_path):
    runner, _ = _scripted([(0, "silent")], tmp_path)
    res = launch.supervise("sup4", tmp_path, runner=runner, max_attempts=1)
    assert res.status == "failed" and res.attempts[0].outcome == "recoverable"


def test_resume_is_claimed_only_with_a_valid_manifest_for_that_step(tmp_path):
    runner, _ = _scripted([(0, "ok")], tmp_path, resumed=4)
    res = launch.supervise("sup5", tmp_path, runner=runner)
    assert res.resumed_from_step is None  # the metrics claim a resume; no manifest backs it
    _fake_checkpoint(tmp_path, 4)
    runner, _ = _scripted([(0, "ok")], tmp_path, resumed=4)
    res = launch.supervise("sup6", tmp_path, runner=runner)
    assert res.resumed_from_step == 4


def test_valid_checkpoints_are_recorded_once_in_the_platform_db(tmp_path):
    from examlops import platform_db

    _fake_checkpoint(tmp_path, 2)
    _fake_checkpoint(tmp_path, 4)
    runner, _ = _scripted([(0, "ok")] * 2, tmp_path)
    launch.supervise("sup7", tmp_path, runner=runner)
    launch.supervise("sup7", tmp_path, runner=runner)
    assert sorted(c["step"] for c in platform_db.list_training_checkpoints("sup7")) == [2, 4]
    assert platform_db.list_training_checkpoints("sup7")[0]["shard_count"] == 2


def test_cli_run_requires_local_and_reports_missing_torch(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    r = CliRunner().invoke(app, ["pipeline", "distributed", "run"])
    assert r.exit_code == 2 and "--local" in r.output
    monkeypatch.setattr(launch, "torch_available", lambda: False)
    r = CliRunner().invoke(app, ["pipeline", "distributed", "run", "--local"])
    assert r.exit_code == 2 and "torch is not installed" in r.output


def test_cli_run_drives_the_supervisor(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path / "data"))
    seen = {}

    def fake(run_id, run_dir, **kw):
        seen.update(kw, run_id=run_id, run_dir=run_dir)
        return launch.SupervisedRun(run_id, run_dir, "failed")

    monkeypatch.setattr(launch, "supervise", fake)
    r = CliRunner().invoke(
        app,
        [
            "pipeline",
            "distributed",
            "run",
            "--local",
            "--nproc",
            "3",
            "--steps",
            "9",
            "--max-attempts",
            "2",
            "--run-id",
            "cli-x",
        ],
    )
    assert r.exit_code == 1  # a run that did not complete is a failing exit
    assert seen["nproc_per_node"] == 3 and seen["steps"] == 9 and seen["max_attempts"] == 2
    assert Path(seen["run_dir"]) == tmp_path / "data" / "distributed" / "cli-x"


def _break_the_audit_log(monkeypatch):
    from examlops.data import audit as audit_mod

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_a_lost_distributed_audit_is_counted(monkeypatch):
    from examlops import distributed
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    reset_dropped_audit_events()
    _break_the_audit_log(monkeypatch)
    distributed._audit("r", "distributed_failed", {}, "alice")  # fails open
    assert dropped_audit_events().get("distributed_failed") == 1
    reset_dropped_audit_events()


def test_a_lost_attempt_audit_is_counted_and_the_run_goes_on(monkeypatch, tmp_path):
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    reset_dropped_audit_events()
    _break_the_audit_log(monkeypatch)
    runner, _ = _scripted([(0, "ok")], tmp_path)
    res = launch.supervise("sup-audit", tmp_path, runner=runner)
    assert res.status == "complete"  # the training outcome does not depend on the audit log
    assert dropped_audit_events().get("distributed_attempt") == 1
    reset_dropped_audit_events()
