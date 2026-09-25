"""ADR 0109 — `exa pipeline distributed suspend …` and the seam's line in `exa status`.

Real platform.db, real provider registry, real ADR 0032 checkpoints written with the shipped
manifest writer; the CLI is driven end to end through Typer.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from examlops import platform_db
from examlops.cli.main import app
from examlops.distributed import checkpoint_files as cf
from examlops.suspend.tiers import LOCAL_DIR_ENV

runner = CliRunner()
BASE = ["pipeline", "distributed", "suspend"]


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv("EXAMLOPS_SUSPEND_BACKEND_PROVIDER", raising=False)
    monkeypatch.setenv(LOCAL_DIR_ENV, str(tmp_path / "local"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "cli-tester")
    platform_db.init_db()


@pytest.fixture
def run_dir(tmp_path) -> Path:
    rd = tmp_path / "run-a"
    d = cf.step_dir(rd, 2)
    d.mkdir(parents=True)
    name = cf.shard_name(0, 1)
    (d / name).write_bytes(b"W" * 4096)
    cf.write_manifest(
        rd,
        step=2,
        world_size=1,
        cfg_hash="h",
        shards=[{"rank": 0, "file": name, "sha256": cf.sha256_file(d / name), "bytes": 4096}],
        resumed_from_step=None,
    )
    return rd


def _json(args: list[str]):
    res = runner.invoke(app, ["--json", *args])
    return res, (json.loads(res.stdout) if res.stdout.strip() else None)


def test_backends_lists_every_backend_with_its_verdict():
    res, body = _json([*BASE, "backends"])
    assert res.exit_code == 0, res.output
    by = {b["backend"]: b for b in body["backends"]}
    assert body["selected_backend"] == "checkpoint-only"
    assert by["vllm-sleep"]["preemption"]["can_promise"] is False
    assert by["tiered-training-checkpoint"]["capability"]["tiers"][-1] == "persistent_storage"


def test_capability_of_unknown_backend_exits_1():
    res = runner.invoke(app, [*BASE, "capability", "criu"])
    assert res.exit_code == 1


def test_snapshot_resume_discard_round_trip(run_dir):
    res, snap = _json(
        [*BASE, "snapshot", "run-a", "--backend", "tiered-training-checkpoint",
         "--run-dir", str(run_dir), "--tenant", "t1"]
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    sid = snap["snapshot_id"]
    assert snap["pointer"]["local_tier"]["staged"] is True
    res, shown = _json([*BASE, "show", sid])
    assert shown["status"] == "suspended" and shown["actor"] == "cli-tester"
    res, rep = _json([*BASE, "resume", sid])
    assert res.exit_code == 0 and rep["restored"] is True and rep["state_transfer_s"] > 0
    res = runner.invoke(app, [*BASE, "discard", sid])
    assert res.exit_code == 0
    res, shown = _json([*BASE, "show", sid])
    assert shown["status"] == "discarded"
    assert cf.verify_checkpoint_dir(cf.step_dir(run_dir, 2)).valid  # checkpoint untouched


def test_refused_snapshot_exits_1_with_the_backend_reason(tmp_path):
    res, body = _json(
        [*BASE, "snapshot", "ghost", "--backend", "training-checkpoint",
         "--run-dir", str(tmp_path / "nothing")]
    )  # fmt: skip
    assert res.exit_code == 1
    assert "no valid checkpoint" in body["error"]
    with platform_db.get_db() as c:
        acts = [r[0] for r in c.execute("SELECT action FROM audit_events")]
    assert "suspend_refused" in acts


def test_resume_of_a_resumed_snapshot_exits_1(run_dir):
    _, snap = _json(
        [*BASE, "snapshot", "run-a", "--backend", "training-checkpoint", "--run-dir", str(run_dir)]
    )
    assert runner.invoke(app, [*BASE, "resume", snap["snapshot_id"]]).exit_code == 0
    assert runner.invoke(app, [*BASE, "resume", snap["snapshot_id"]]).exit_code == 1


def test_list_filters_by_tenant_before_the_limit(run_dir):
    for tenant in ("a", "a", "b", "b", "b"):
        res = runner.invoke(
            app,
            [*BASE, "snapshot", "run-a", "--backend", "training-checkpoint",
             "--run-dir", str(run_dir), "--tenant", tenant],
        )  # fmt: skip
        assert res.exit_code == 0, res.output
    # Newest three are all tenant b: a filter applied after LIMIT would return nothing for a.
    _, rows = _json([*BASE, "list", "--tenant", "a", "--limit", "3"])
    assert [r["tenant"] for r in rows] == ["a", "a"]
    _, rows = _json([*BASE, "list", "--limit", "2"])
    assert len(rows) == 2


def test_status_json_carries_the_seam_even_when_the_control_plane_is_down():
    with patch("examlops.cli.commands.status._client.get", side_effect=Exception("down")):
        res = runner.invoke(app, ["--json", "status"])
    body = json.loads(res.stdout)
    assert "suspend" in body
    names = {b["backend"] for b in body["suspend"]["backends"]}
    assert {"training-checkpoint", "tiered-training-checkpoint", "vllm-sleep"} <= names


def test_status_table_renders_the_seam():
    payload = {
        "status": "ok",
        "pending_approvals": 0,
        "services": {k: {"ok": True} for k in ("control_plane", "mlflow", "dashboard")},
    }
    with patch("examlops.cli.commands.status._client.get", return_value=payload):
        res = runner.invoke(app, ["status"], terminal_width=200)
    assert res.exit_code == 0, res.output
    assert "Suspend/Resume Seam" in res.output and "vllm-sleep" in res.output


def test_status_survives_a_broken_seam():
    with (
        patch("examlops.suspend.report.seam_report", side_effect=RuntimeError("boom")),
        patch("examlops.cli.commands.status._client.get", side_effect=Exception("down")),
    ):
        res = runner.invoke(app, ["--json", "status"])
    body = json.loads(res.stdout)
    assert body["suspend"]["error"].startswith("suspend seam unavailable")
