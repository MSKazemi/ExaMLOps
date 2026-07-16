"""E6 — distributed & fault-tolerant training (ADR 0032)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def test_gwt1_launch_builds_rendezvous():
    from examlops.distributed import launch_distributed

    h = launch_distributed("JPCP", nodes=2, strategy="fsdp", node_list=["node07", "node08"])
    assert h.spec.nodes == 2
    assert h.spec.rdzv_endpoint.startswith("node07:")
    cmd = h.spec.torchrun_command()
    assert "--nnodes=2" in cmd
    assert "--strategy=fsdp" in cmd


def test_launch_localhost_rendezvous_without_nodes():
    from examlops.distributed import launch_distributed

    h = launch_distributed("JPCP", nodes=1, strategy="fsdp")
    assert h.spec.rdzv_endpoint.startswith("localhost:")


def test_gwt4_strategy_selectable():
    from examlops.distributed import launch_distributed

    h = launch_distributed("JPCP", nodes=1, strategy="zero", run_id="r-zero")
    assert h.spec.strategy == "zero"


def test_invalid_strategy_rejected():
    from examlops.distributed import launch_distributed

    with pytest.raises(ValueError):
        launch_distributed("JPCP", nodes=1, strategy="bogus")


def test_gwt2_checkpoint_written_with_integrity():
    from examlops.distributed import launch_distributed, write_checkpoint
    from examlops.platform_db import list_training_checkpoints

    launch_distributed("JPCP", nodes=1, run_id="r1")
    ckpt = write_checkpoint(
        "r1", step=100, epoch=1, state={"opt": "adam", "lr": 0.01}, shard_count=4
    )
    assert ckpt.integrity_hash
    rows = list_training_checkpoints("r1")
    assert len(rows) == 1
    assert rows[0]["shard_count"] == 4


def test_gwt3_resume_from_last_valid_checkpoint():
    from examlops.distributed import launch_distributed, resume_from_checkpoint, write_checkpoint

    launch_distributed("JPCP", nodes=1, run_id="r1")
    write_checkpoint("r1", step=100, epoch=1, state={"s": 1})
    write_checkpoint("r1", step=200, epoch=2, state={"s": 2})
    ckpt = resume_from_checkpoint("r1")
    assert ckpt is not None
    assert ckpt.step == 200  # newest, not from scratch
    assert ckpt.epoch == 2
    assert ckpt.state == {"s": 2}


def test_gwt5_corrupt_checkpoint_refused_falls_back():
    from examlops import platform_db
    from examlops.distributed import launch_distributed, resume_from_checkpoint, write_checkpoint

    launch_distributed("JPCP", nodes=1, run_id="r1")
    write_checkpoint("r1", step=100, epoch=1, state={"s": 1})
    write_checkpoint("r1", step=200, epoch=2, state={"s": 2})
    # Corrupt the newest checkpoint's stored state (hash no longer matches).
    with platform_db.get_db() as conn:
        conn.execute(
            "UPDATE training_checkpoints SET state_json='{\"s\": 999}' WHERE step=200 AND run_id='r1'"
        )
    ckpt = resume_from_checkpoint("r1")
    # Newest (200) is corrupt → refused; falls back to the last valid (100).
    assert ckpt is not None
    assert ckpt.step == 100


def test_resume_none_when_all_corrupt():
    from examlops import platform_db
    from examlops.distributed import launch_distributed, resume_from_checkpoint, write_checkpoint

    launch_distributed("JPCP", nodes=1, run_id="r1")
    write_checkpoint("r1", step=100, epoch=1, state={"s": 1})
    with platform_db.get_db() as conn:
        conn.execute("UPDATE training_checkpoints SET integrity_hash='bad' WHERE run_id='r1'")
    assert resume_from_checkpoint("r1") is None


def test_gwt6_failure_and_resume_audited_and_resumes_bumped():
    from examlops.distributed import (
        launch_distributed,
        mark_failed,
        resume_from_checkpoint,
        write_checkpoint,
    )
    from examlops.platform_db import get_db, get_distributed_run

    launch_distributed("JPCP", nodes=2, run_id="r1")
    write_checkpoint("r1", step=100, epoch=1, state={"s": 1})
    mark_failed("r1", reason="preempted")
    resume_from_checkpoint("r1")
    run = get_distributed_run("r1")
    assert run["resumes"] == 1
    with get_db() as conn:
        failed = conn.execute(
            "SELECT * FROM audit_events WHERE action='distributed_failed'"
        ).fetchall()
        resumed = conn.execute(
            "SELECT * FROM audit_events WHERE action='distributed_resume'"
        ).fetchall()
    assert len(failed) == 1
    assert len(resumed) == 1


def test_complete_records_cost():
    from examlops.distributed import complete_run, launch_distributed
    from examlops.platform_db import get_distributed_run

    launch_distributed("JPCP", nodes=2, gpus_per_node=4, run_id="r1")
    complete_run("r1", cost_gpu_hours=64.0)
    run = get_distributed_run("r1")
    assert run["status"] == "complete"
    assert run["cost_gpu_hours"] == 64.0


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r = runner.invoke(
        app, ["pipeline", "distributed", "launch", "JPCP", "--nodes", "2", "--run-id", "r1"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app, ["pipeline", "distributed", "checkpoint", "r1", "--step", "100", "--epoch", "1"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["pipeline", "distributed", "resume", "r1"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["pipeline", "distributed", "list"])
    assert r.exit_code == 0, r.output
    assert "r1" in r.output
