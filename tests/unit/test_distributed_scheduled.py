"""ADR 0032 through the scheduler abstraction, without torch: job-script rendering for mock /
Slurm / Flux (single node, multi-node, elastic, NCCL), the rendezvous host parser, and the
scheduler-level supervisor — resubmission, fatal/submit failures, lost jobs, preflight, cost,
lineage, MLflow linkage and durable mirroring — driven by a scripted scheduler adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.distributed import checkpoint_files as cf  # noqa: E402
from examlops.distributed import rendezvous, scheduled  # noqa: E402
from examlops.distributed.strategy import DistributedPlan, StrategyUnavailable  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_JOB_SCRIPT_DIR", str(tmp_path / "jobs"))
    for k in ("EXAMLOPS_DIST_CHECKPOINT_STORE", "EXAMLOPS_SUSPEND_PREEMPTION_GATE"):
        monkeypatch.delenv(k, raising=False)
    from examlops import platform_db

    platform_db.init_db()


# ── rendezvous ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hostlist,host",
    [
        ("gpu[07-09,11],login2", "gpu07"),
        ("node01", "node01"),
        ("a,b,c", "a"),
        ("r[3]x", "r3x"),
        ("nid[001234-001240]", "nid001234"),
    ],
)
def test_first_host_of_a_scheduler_hostlist(hostlist, host):
    assert rendezvous.first_host(hostlist) == host


@pytest.mark.parametrize("bad", ["", "n[a-b]", "$(reboot)", "a b", "-x"])
def test_malformed_or_hostile_hostlists_are_refused(bad):
    with pytest.raises(ValueError):
        rendezvous.first_host(bad)
    assert rendezvous.main([bad]) == 2


# ── script rendering ──────────────────────────────────────────────────────────


def _plan(**kw) -> DistributedPlan:
    base = {"model": "Demo", "strategy": "ddp", "nproc_per_node": 2, "steps": 6}
    return DistributedPlan(**{**base, **kw})


def _bash_ok(text: str, tmp_path: Path) -> None:
    f = tmp_path / "check.sh"
    f.write_text(text)
    r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_single_node_script_is_standalone_torchrun_under_the_job_python(tmp_path):
    text = scheduled.render_job_script(
        _plan(strategy="fsdp"), "r1", tmp_path / "run", scheduler="mock", attempt=2, seed=5
    )
    _bash_ok(text, tmp_path)
    exec_line = text.strip().splitlines()[-1]
    assert exec_line.startswith(f"exec {sys.executable} -m torch.distributed.run --standalone")
    assert "--nproc-per-node=2" in exec_line and "--strategy=fsdp" in exec_line
    assert "--seed=5" in exec_line and "train_ddp.py" in exec_line
    assert "export EXAMLOPS_DIST_ATTEMPT=2" in text
    assert "srun" not in text and "NCCL" not in text  # CPU plan: no NCCL defaults


def test_slurm_multinode_elastic_script_reads_the_rendezvous_host_at_run_time(tmp_path):
    plan = _plan(
        nodes=4,
        min_nodes=2,
        gpus_per_node=4,
        nproc_per_node=4,
        max_restarts=2,
        nccl={"NCCL_SOCKET_IFNAME": "ib0", "NCCL_IB_HCA": "mlx5_0:1"},
        entrypoint="mypack.train_dist",
    )
    text = scheduled.render_job_script(plan, "big-run", tmp_path / "run", scheduler="slurm")
    _bash_ok(text, tmp_path)
    assert 'rendezvous "${SLURM_JOB_NODELIST:?}"' in text
    last = text.strip().splitlines()[-1]
    assert last.startswith("exec srun --nodes=4 --ntasks-per-node=1")
    assert "--nnodes=2:4" in last and "--max-restarts=2" in last
    assert '"--rdzv_endpoint=${HEAD}:29500"' in last and "--rdzv_id=big-run" in last
    assert "-m mypack.train_dist" in last and "--standalone" not in last
    assert "export NCCL_SOCKET_IFNAME=ib0" in text and "export NCCL_IB_HCA=mlx5_0:1" in text
    # GPU plan: a hung collective must fail the attempt; a site value still wins.
    assert 'TORCH_NCCL_ASYNC_ERROR_HANDLING="${TORCH_NCCL_ASYNC_ERROR_HANDLING:-1}"' in text


def test_flux_multinode_script_uses_flux_run_and_the_instance_hostlist(tmp_path, monkeypatch):
    monkeypatch.setenv(scheduled.RDZV_PORT_ENV, "29600")
    text = scheduled.render_job_script(
        _plan(nodes=2), "fx", tmp_path / "run", scheduler="flux", dataset_revision="rev-9"
    )
    _bash_ok(text, tmp_path)
    assert '"$(flux getattr hostlist)"' in text
    assert text.strip().splitlines()[-1].startswith("exec flux run -N 2 -n 2")
    assert "${HEAD}:29600" in text and "export EXAMLOPS_DATASET_REVISION=rev-9" in text


def test_the_rendered_rendezvous_line_really_picks_the_first_node(tmp_path):
    text = scheduled.render_job_script(_plan(nodes=2), "r", tmp_path / "run", scheduler="slurm")
    head_line = next(line for line in text.splitlines() if line.startswith("HEAD="))
    pp = next(line for line in text.splitlines() if line.startswith("export PYTHONPATH="))
    script = "\n".join([pp, head_line, 'echo "$HEAD"'])
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"SLURM_JOB_NODELIST": "gpu[12-15]", "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "gpu12"


def test_multinode_on_the_mock_scheduler_is_refused(tmp_path):
    with pytest.raises(scheduled.SchedulerSubmitError, match="mock scheduler runs one node"):
        scheduled.render_job_script(_plan(nodes=2), "r", tmp_path, scheduler="mock")


def test_resources_are_scheduler_neutral_one_task_per_node():
    res = scheduled.job_resources(_plan(nodes=3, gpus_per_node=4), "run-x")
    assert res == {
        "nodes": 3,
        "ntasks": 3,
        "job_name": "exa-dist-run-x",
        "gpus_per_node": 4,
        "gpus": 12,
    }


# ── supervisor, with a scripted adapter ───────────────────────────────────────


def _ckpt(run_dir: Path, step: int, cfg: str = "c") -> None:
    d = cf.step_dir(run_dir, step)
    d.mkdir(parents=True, exist_ok=True)
    shards = []
    for r in range(2):
        data = f"w{step}{r}".encode()
        cf.atomic_write_bytes(d / cf.shard_name(r, 2), data)
        shards.append(
            {
                "rank": r,
                "file": cf.shard_name(r, 2),
                "sha256": cf.sha256_file(d / cf.shard_name(r, 2)),
                "bytes": len(data),
            }
        )
    cf.write_manifest(
        run_dir, step=step, world_size=2, cfg_hash=cfg, shards=shards, resumed_from_step=None
    )


class ScriptedAdapter:
    """Behaves like a phase-23 adapter; each job's effect on the run dir is scripted."""

    def __init__(self, run_dir: Path, outcomes: list[str], tmp: Path):
        self.working_dir = tmp / "adapter"
        self.run_dir = run_dir
        self.outcomes = outcomes
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self._status: dict[str, dict] = {}
        self._logs: dict[str, str] = {}

    def submit_job(self, script_path=None, resources=None, training_data=None, remote_dir=None):
        n = len(self.submitted)
        self.submitted.append(
            {"script": Path(script_path).read_text(), "resources": resources, "dir": remote_dir}
        )
        job = f"job-{n + 1}"
        kind = self.outcomes[n]
        if kind == "submit-refused":
            raise RuntimeError("sbatch: error: invalid partition")
        start, end = "2026-09-25T10:00:00+00:00", "2026-09-25T10:30:00+00:00"
        if kind == "preempted":  # wrote checkpoint 2 and 4, then the node went away
            _ckpt(self.run_dir, 2)
            _ckpt(self.run_dir, 4)
            self._status[job] = {"state": "FAILED", "exit_code": 1}
            self._logs[job] = "[rank 1] recoverable failure"
        elif kind == "ok-resumed":
            _ckpt(self.run_dir, 6)
            m = {"status": "complete", "steps": 6, "config_hash": "c", "resumed_from_step": 4}
            self._status[job] = {"state": "COMPLETED", "exit_code": 0}
            self._logs[job] = "train\n" + cf.METRICS_MARKER + json.dumps(m)
        elif kind == "fatal":
            (self.run_dir / cf.FATAL_MARKER).write_text("{}")
            self._status[job] = {"state": "FAILED", "exit_code": 1}
            self._logs[job] = "FATAL: non-finite loss"
        elif kind == "lost":
            self._status[job] = {"state": "RUNNING", "exit_code": None}
            self._logs[job] = ""
        self._status[job].update(start_time=start, end_time=end)
        return job

    def wait_until_complete(self, job_id, poll_interval=10, max_wait_s=None):
        if self._status[job_id]["state"] == "RUNNING":
            raise TimeoutError("did not reach a terminal state")
        return "done"

    def get_job_status(self, job_id):
        return dict(self._status[job_id])

    def get_job_logs(self, job_id):
        return self._logs[job_id]

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)


def _actions(target: str) -> list[str]:
    from examlops import platform_db

    with platform_db.get_db() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE target=? ORDER BY id", (target,)
            )
        ]


def _sup(plan, run_id, run_dir, adapter, **kw):
    return scheduled.supervise_scheduled(
        plan,
        run_id,
        run_dir,
        adapter=adapter,
        scheduler="slurm",
        backoff_s=3,
        sleep=kw.pop("sleep", lambda s: None),
        skip_preflight=True,
        **kw,
    )


def test_a_preempted_job_is_resubmitted_through_the_scheduler_and_resumes(tmp_path):
    from examlops import platform_db

    run = tmp_path / "run"
    adapter = ScriptedAdapter(run, ["preempted", "ok-resumed"], tmp_path)
    tags: dict = {}
    sleeps: list = []
    plan = _plan(nodes=2, gpus_per_node=4, nproc_per_node=4, checkpoint_every=2)
    res = _sup(
        plan,
        "sched-1",
        run,
        adapter,
        sleep=sleeps.append,
        checkpoint_store=str(tmp_path / "nfs"),
        dataset_revision="rev-42",
        mlflow_run_id="mlf-1",
        mlflow_tagger=lambda rid, t: tags.update(t, _run=rid),
    )
    assert res.status == "complete"
    assert [a.outcome for a in res.attempts] == ["recoverable", "success"]
    assert len(adapter.submitted) == 2  # the scheduler, not a local loop, ran both attempts
    assert "EXAMLOPS_DIST_ATTEMPT=2" in adapter.submitted[1]["script"]
    assert adapter.submitted[0]["resources"]["nodes"] == 2 and sleeps == [3]
    assert res.resumed_from_step == 4
    # Durable: every valid step was mirrored, and the recorded URIs are the durable ones.
    assert sorted(res.mirrored) == [2, 4, 6]
    rows = platform_db.list_training_checkpoints("sched-1")
    assert {r["step"] for r in rows} == {2, 4, 6}
    assert all(r["uri"].startswith(str(tmp_path / "nfs")) for r in rows)
    assert all(r["mlflow_run_id"] == "mlf-1" for r in rows)
    # Cost: 2 attempts x 30 min x 2 nodes x 4 GPUs = 8 GPU-hours, in distributed_runs + model_costs.
    assert res.cost["gpu_hours"] == pytest.approx(8.0) and res.cost["recorded"] is True
    run_row = platform_db.get_distributed_run("sched-1")
    assert run_row["cost_gpu_hours"] == pytest.approx(8.0) and run_row["status"] == "complete"
    assert run_row["dataset_revision"] == "rev-42" and run_row["resumes"] == 1
    with platform_db.get_db() as conn:
        cost = conn.execute(
            "SELECT model_name, version, job_id, gpu_hours FROM model_costs WHERE run_id=?",
            ("sched-1",),
        ).fetchone()
        jobs = conn.execute(
            "SELECT job_id, state FROM hpc_jobs WHERE model='distributed:Demo' ORDER BY job_id"
        ).fetchall()
        lineage = conn.execute(
            "SELECT event_type, dataset_revision, mlflow_run_id FROM lineage_events "
            "WHERE run_id=? ORDER BY id",
            ("sched-1",),
        ).fetchall()
    assert tuple(cost) == ("Demo", 0, "job-2", pytest.approx(8.0))
    assert [tuple(j) for j in jobs] == [("job-1", "FAILED"), ("job-2", "COMPLETED")]
    assert [tuple(r) for r in lineage] == [
        ("START", "rev-42", "mlf-1"),
        ("COMPLETE", "rev-42", "mlf-1"),
    ]
    # MLflow: the run is tagged with the distributed run and its newest (durable) checkpoint.
    assert tags["_run"] == "mlf-1" and tags["examlops.checkpoint.step"] == "6"
    assert tags["examlops.checkpoint.uri"].endswith("sched-1/ckpt/step-00000006")
    assert tags["hpc_job_id"] == "job-2" and res.mlflow_linked
    actions = _actions("sched-1")
    for a in (
        "distributed_launch",
        "distributed_submit",
        "distributed_failed",
        "distributed_resubmit",
        "distributed_resume",
        "distributed_complete",
        "checkpoint_mirrored",
    ):
        assert a in actions, a


def test_the_durable_copy_is_restored_before_a_resubmission_on_a_fresh_node(tmp_path):
    run = tmp_path / "run"
    adapter = ScriptedAdapter(run, ["preempted", "ok-resumed"], tmp_path)
    real_submit = adapter.submit_job
    seen_before_attempt_2: list = []

    def submit(*a, **k):
        if adapter.submitted:  # attempt 2 lands on a node whose scratch was wiped …
            seen_before_attempt_2.append(cf.find_latest_valid(run)[0])
        return real_submit(*a, **k)

    def wipe_then_sleep(_s):
        shutil.rmtree(run)
        run.mkdir()

    adapter.submit_job = submit
    res = _sup(
        _plan(),
        "sched-2",
        run,
        adapter,
        sleep=wipe_then_sleep,
        checkpoint_store=str(tmp_path / "nfs"),
    )
    assert res.status == "complete" and res.restored_from_store == [4]
    assert seen_before_attempt_2[0] is not None and seen_before_attempt_2[0].step == 4
    assert "checkpoint_restored" in _actions("sched-2")


def test_a_fatal_job_is_not_resubmitted(tmp_path):
    run = tmp_path / "run"
    adapter = ScriptedAdapter(run, ["fatal", "ok-resumed"], tmp_path)
    res = _sup(_plan(), "sched-3", run, adapter)
    assert res.status == "fatal" and len(adapter.submitted) == 1
    assert "distributed_fatal" in _actions("sched-3")


def test_a_refused_submission_stops_without_looping(tmp_path):
    adapter = ScriptedAdapter(tmp_path / "run", ["submit-refused"], tmp_path)
    res = _sup(_plan(max_attempts=5), "sched-4", tmp_path / "run", adapter)
    assert res.status == "fatal" and "invalid partition" in (res.error or "")
    assert res.attempts == [] and res.cost is None
    assert "distributed_submit_failed" in _actions("sched-4")


def test_a_lost_job_is_cancelled_and_counted_recoverable(tmp_path):
    from examlops import platform_db

    run = tmp_path / "run"
    adapter = ScriptedAdapter(run, ["lost", "lost"], tmp_path)
    res = _sup(_plan(max_attempts=2), "sched-5", run, adapter)
    assert res.status == "failed"
    assert [a.state for a in res.attempts] == ["TIMEOUT", "TIMEOUT"]
    assert adapter.cancelled == ["job-1", "job-2"]
    assert _actions("sched-5").count("distributed_job_lost") == 2
    assert "distributed_gave_up" in _actions("sched-5")
    assert platform_db.get_distributed_run("sched-5")["status"] == "failed"
    with platform_db.get_db() as conn:
        ev = conn.execute(
            "SELECT event_type FROM lineage_events WHERE run_id='sched-5' ORDER BY id"
        ).fetchall()
    assert [r[0] for r in ev] == ["START", "FAIL"]


def test_preflight_refuses_before_anything_is_submitted(tmp_path, monkeypatch):
    from examlops.distributed import strategy as st

    monkeypatch.setattr(st, "_importable", lambda m: m != "deepspeed")
    adapter = ScriptedAdapter(tmp_path / "run", ["ok-resumed"], tmp_path)
    with pytest.raises(StrategyUnavailable, match="deepspeed"):
        scheduled.supervise_scheduled(
            _plan(strategy="zero"), "sched-6", tmp_path / "run", adapter=adapter, scheduler="mock"
        )
    assert adapter.submitted == []


def test_an_mlflow_outage_does_not_fail_a_finished_run(tmp_path):
    run = tmp_path / "run"
    adapter = ScriptedAdapter(run, ["ok-resumed"], tmp_path)

    def down(rid, tags):
        raise ConnectionError("mlflow unreachable")

    res = _sup(_plan(), "sched-7", run, adapter, mlflow_run_id="m", mlflow_tagger=down)
    assert res.status == "complete" and res.mlflow_linked is False
    assert "mlflow_link_failed" in _actions("sched-7")


def test_bounds_are_enforced(tmp_path):
    adapter = ScriptedAdapter(tmp_path / "run", [], tmp_path)
    with pytest.raises(ValueError, match="max_attempts"):
        _sup(_plan(max_attempts=99), "sched-8", tmp_path / "run", adapter)
    with pytest.raises(ValueError, match="safe store key"):
        _sup(_plan(), "../evil", tmp_path / "run", adapter)


def test_cpu_plans_are_costed_in_cpu_hours():
    cost = scheduled.run_cost(_plan(nodes=1, gpus_per_node=0, nproc_per_node=4), 1800)
    assert cost["gpu_hours"] == 0 and cost["cpu_hours"] == pytest.approx(2.0)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _pack(tmp_path: Path, block: str) -> Path:
    d = tmp_path / "models"
    d.mkdir(exist_ok=True)
    (d / "demo.yaml").write_text(
        "name: Demo\nmodel_class: X\ndistributed:\n"
        + "".join(f"  {line}\n" for line in block.splitlines())
    )
    return d


def test_pipeline_run_distributed_routes_the_yaml_plan_to_the_scheduler(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setenv(
        "RAY_MODELS_DIR",
        str(_pack(tmp_path, "strategy: ddp\nnodes: 1\nentrypoint: mypack.train_dist")),
    )
    seen: dict = {}

    def fake(plan, run_id, run_dir=None, **kw):
        seen.update(plan=plan, run_id=run_id, **kw)
        return scheduled.ScheduledRun(run_id, tmp_path, "mock", plan.to_dict(), "complete")

    monkeypatch.setattr(scheduled, "supervise_scheduled", fake)
    r = CliRunner().invoke(app, ["pipeline", "run", "--model", "Demo", "--distributed"])
    assert r.exit_code == 0, r.output
    assert seen["plan"].strategy == "ddp" and seen["plan"].source == "yaml"
    assert seen["run_id"].startswith("dist-demo-")


def test_pipeline_run_distributed_needs_a_model_and_refuses_ir():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    r = CliRunner().invoke(app, ["pipeline", "run", "--distributed"])
    assert r.exit_code == 2 and "--model" in r.output
    r = CliRunner().invoke(app, ["pipeline", "run", "--distributed", "--ir", "x.json", "-m", "A"])
    assert r.exit_code == 2 and "cannot be combined" in r.output


def test_distributed_run_scheduler_flags_override_yaml_and_bad_yaml_exits_2(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setenv("RAY_MODELS_DIR", str(_pack(tmp_path, "strategy: ddp\nnodes: 2")))
    seen: dict = {}

    def fake(plan, run_id, run_dir=None, **kw):
        seen.update(plan=plan, **kw)
        return scheduled.ScheduledRun(run_id, tmp_path, "slurm", plan.to_dict(), "failed")

    monkeypatch.setattr(scheduled, "supervise_scheduled", fake)
    r = CliRunner().invoke(
        app,
        [
            "pipeline",
            "distributed",
            "run",
            "--scheduler",
            "--model",
            "Demo",
            "--strategy",
            "fsdp",
            "--min-nodes",
            "1",
            "--checkpoint-store",
            str(tmp_path / "nfs"),
        ],
    )
    assert r.exit_code == 1  # the (fake) run failed → failing exit
    assert seen["plan"].strategy == "fsdp" and seen["plan"].nnodes_spec == "1:2"
    assert seen["checkpoint_store"] == str(tmp_path / "nfs")
    monkeypatch.setenv("RAY_MODELS_DIR", str(_pack(tmp_path, "nodes: zero")))
    r = CliRunner().invoke(
        app, ["pipeline", "distributed", "run", "--scheduler", "--model", "Demo"]
    )
    assert r.exit_code == 2 and "must be an integer" in r.output


def test_distributed_run_mode_flags_are_exclusive():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    r = CliRunner().invoke(app, ["pipeline", "distributed", "run", "--local", "--scheduler"])
    assert r.exit_code == 2 and "exactly one" in r.output
    r = CliRunner().invoke(app, ["pipeline", "distributed", "run", "--scheduler"])
    assert r.exit_code == 2 and "--model" in r.output
    r = CliRunner().invoke(app, ["pipeline", "distributed", "run", "--local", "--nodes", "2"])
    assert r.exit_code == 2 and "one node" in r.output


def test_a_lost_scheduled_audit_is_counted(tmp_path, monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    def mlflow_down(rid, tags):
        raise ConnectionError("mlflow unreachable")

    reset_dropped_audit_events()
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    run = tmp_path / "run"
    adapter = ScriptedAdapter(run, ["preempted", "ok-resumed"], tmp_path)
    res = _sup(_plan(), "sched-9", run, adapter, mlflow_run_id="m", mlflow_tagger=mlflow_down)
    assert res.status == "complete"  # the training outcome does not depend on the audit log
    dropped = dropped_audit_events()
    assert dropped.get("distributed_submit") == 2  # _run_attempt
    assert dropped.get("distributed_launch") == 1 and dropped.get("distributed_complete") == 1
    assert dropped.get("mlflow_link_failed") == 1  # link_mlflow
    reset_dropped_audit_events()
