"""ADR 0032 review findings, each pinned by the behaviour it broke.

* a Slurm job that ended NODE_FAIL / PREEMPTED / ``CANCELLED by <uid>`` was polled as still
  running until the 24 h wait ceiling — the very failures distributed training exists to survive;
* an operator's ``scancel`` was answered by a resubmission;
* a failed attempt's job was not cancelled before its replacement was submitted;
* ``exa pipeline run --distributed`` admitted and placed the run on ``--gpus`` (default 0), not on
  the ``nodes × gpus_per_node`` it actually submits;
* two runs started in the same second shared a run id — and so a run directory and store key;
* ``--checkpoint-store`` reached the dashboard's CLI console uncontained, and the durable store
  opened any fsspec protocol (http, ftp, …);
* ``--local --run-id ../x`` placed the run directory outside the data root;
* a malformed ``EXAMLOPS_DIST_MAX_WAIT_S`` (0 cancels every job at once) was accepted;
* ``exa pipeline distributed run --scheduler`` submitted real scheduler jobs around both the
  admission gate and the budget gate that ``exa pipeline run`` honours;
* an unknown model name resolved to the default plan, and its cost/lineage were recorded under it;
* ``exa pipeline run --distributed`` on a model with no ``entrypoint`` trained the synthetic
  reference script and recorded its cost, lineage and MLflow tags as that model's training;
* an unreachable durable store read as an empty one: the attempt silently restarted from step 0.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
_ADAPTER_DIR = Path(__file__).resolve().parents[2] / "platform" / "infra" / "slurm-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

from examlops.distributed import durable, scheduled  # noqa: E402
from examlops.distributed.strategy import DistributedPlan  # noqa: E402
from tests.unit.test_distributed_scheduled import (  # noqa: E402
    ScriptedAdapter,
    _actions,
    _pack,
    _sup,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_JOB_SCRIPT_DIR", str(tmp_path / "jobs"))
    for k in (
        "EXAMLOPS_DIST_CHECKPOINT_STORE",
        "EXAMLOPS_SUSPEND_PREEMPTION_GATE",
        scheduled.MAX_WAIT_ENV,
    ):
        monkeypatch.delenv(k, raising=False)
    from examlops import platform_db

    platform_db.init_db()


def _plan(**kw) -> DistributedPlan:
    base = {"model": "Demo", "strategy": "ddp", "nproc_per_node": 2, "steps": 6}
    return DistributedPlan(**{**base, **kw})


# ── the scheduler wait loop ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state", ["NODE_FAIL", "PREEMPTED", "OUT_OF_MEMORY", "CANCELLED by 4242", "BOOT_FAIL"]
)
def test_slurm_failure_end_states_end_the_wait(tmp_path, monkeypatch, state):
    import adapter as slurm_adapter

    a = slurm_adapter.RealSlurmAdapter(working_dir=str(tmp_path / "jobs"))
    monkeypatch.setattr(a, "get_job_status", lambda _j: {"state": state})
    # Before the fix these states were not terminal: the loop polled until the wait ceiling.
    assert a.wait_until_complete("7", poll_interval=0, max_wait_s=2).endswith("7.out")


def test_running_states_still_do_not_end_the_wait(tmp_path, monkeypatch):
    import adapter as slurm_adapter

    a = slurm_adapter.RealSlurmAdapter(working_dir=str(tmp_path / "jobs"))
    monkeypatch.setattr(a, "get_job_status", lambda _j: {"state": "COMPLETING"})
    with pytest.raises(slurm_adapter.JobTimeoutError):
        a.wait_until_complete("7", poll_interval=0, max_wait_s=0)


# ── supervisor decisions ──────────────────────────────────────────────────────


class _Adapter(ScriptedAdapter):
    """ScriptedAdapter plus an operator-cancelled outcome."""

    def submit_job(self, script_path=None, resources=None, training_data=None, remote_dir=None):
        if self.outcomes[len(self.submitted)] != "scancelled":
            return super().submit_job(script_path, resources, training_data, remote_dir)
        self.submitted.append({"script": Path(script_path).read_text(), "resources": resources})
        job = f"job-{len(self.submitted)}"
        self._status[job] = {
            "state": "CANCELLED by 4242",
            "exit_code": 0,
            "start_time": "2026-09-25T10:00:00+00:00",
            "end_time": "2026-09-25T10:10:00+00:00",
        }
        self._logs[job] = ""
        return job


def test_an_operator_cancel_is_not_answered_with_a_resubmission(tmp_path):
    run = tmp_path / "run"
    adapter = _Adapter(run, ["scancelled", "ok-resumed"], tmp_path)
    res = _sup(_plan(max_attempts=3), "rv-cancel", run, adapter)
    assert res.status == "cancelled", res
    assert len(adapter.submitted) == 1
    assert res.attempts[0].outcome == "cancelled"
    acts = _actions("rv-cancel")
    assert "distributed_cancelled" in acts and "distributed_resubmit" not in acts


def test_a_failed_job_is_cancelled_before_its_replacement_is_submitted(tmp_path):
    run = tmp_path / "run"
    adapter = _Adapter(run, ["preempted", "ok-resumed"], tmp_path)
    res = _sup(_plan(), "rv-requeue", run, adapter)
    assert res.status == "complete"
    # job-1 failed; had the scheduler requeued it, it would run next to job-2 on the same run dir.
    assert adapter.cancelled == ["job-1"]


def test_a_bad_max_wait_is_refused_before_anything_is_submitted(tmp_path, monkeypatch):
    adapter = _Adapter(tmp_path / "run", ["ok-resumed"], tmp_path)
    for bad in ("0", "soon", "-5"):
        monkeypatch.setenv(scheduled.MAX_WAIT_ENV, bad)
        with pytest.raises(ValueError, match=scheduled.MAX_WAIT_ENV):
            scheduled.supervise_scheduled(
                _plan(),
                "rv-wait",
                tmp_path / "run",
                adapter=adapter,
                scheduler="slurm",
                skip_preflight=True,
            )
    assert adapter.submitted == []


# ── CLI: admission, placement, run ids ────────────────────────────────────────


def test_pipeline_run_distributed_is_admitted_on_the_plans_gpus(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli import _admission_gate
    from examlops.cli.main import app

    monkeypatch.setenv(
        "RAY_MODELS_DIR",
        str(_pack(tmp_path, "strategy: ddp\nnodes: 4\ngpus_per_node: 8\nentrypoint: pk.train")),
    )
    gate: dict = {}

    @contextlib.contextmanager
    def fake_gate(*, project=None, model=None, gpus=0):
        gate["gpus"] = gpus
        yield

    def fake(plan, run_id, run_dir=None, **kw):
        return scheduled.ScheduledRun(run_id, tmp_path, "slurm", plan.to_dict(), "complete")

    monkeypatch.setattr(_admission_gate, "pipeline_run_gate", fake_gate)
    monkeypatch.setattr(scheduled, "supervise_scheduled", fake)
    r = CliRunner().invoke(app, ["pipeline", "run", "--model", "Demo", "--distributed"])
    assert r.exit_code == 0, r.output
    assert gate["gpus"] == 32  # 4 nodes × 8 GPUs, not --gpus' default of 0


def test_distributed_ask_never_asks_for_less_than_given(monkeypatch):
    from examlops.cli.commands.pipeline import _distributed_ask
    from examlops.hpc_placement import ResourceAsk

    monkeypatch.delenv("RAY_MODELS_DIR", raising=False)
    # No YAML block ⇒ defaults (1 node, 0 GPUs): an explicit --gpus still wins.
    assert _distributed_ask("NoSuchModel", None, 3).gpus == 3
    ask = _distributed_ask("NoSuchModel", ResourceAsk(gpus=2, cpus=16, nodes=2), 0)
    assert (ask.gpus, ask.cpus, ask.nodes) == (2, 16, 2)


def test_same_second_runs_get_distinct_run_ids(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setenv("RAY_MODELS_DIR", str(_pack(tmp_path, "strategy: ddp\nentrypoint: pk.t")))
    monkeypatch.setattr("time.time", lambda: 1_790_000_000.0)
    ids: list[str] = []

    def fake(plan, run_id, run_dir=None, **kw):
        ids.append(run_id)
        return scheduled.ScheduledRun(run_id, tmp_path, "mock", plan.to_dict(), "complete")

    monkeypatch.setattr(scheduled, "supervise_scheduled", fake)
    for _ in range(2):
        r = CliRunner().invoke(app, ["pipeline", "run", "--model", "Demo", "--distributed"])
        assert r.exit_code == 0, r.output
    assert len(set(ids)) == 2, ids
    for rid in ids:
        durable._safe_run_id(rid)


def test_local_run_id_cannot_climb_out_of_the_data_root(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.distributed import launch

    called: list = []
    monkeypatch.setattr(launch, "supervise", lambda *a, **k: called.append(a))
    r = CliRunner().invoke(
        app, ["pipeline", "distributed", "run", "--local", "--run-id", "../../escape"]
    )
    assert r.exit_code == 2 and "safe store key" in r.output
    assert called == []


# ── durable store + dashboard console surface ─────────────────────────────────


@pytest.mark.parametrize("url", ["http://169.254.169.254/x", "ftp://h/x", "sftp://h/x"])
def test_the_durable_store_refuses_arbitrary_fsspec_protocols(url):
    with pytest.raises(durable.DurableStoreUnavailable, match="not supported"):
        durable.open_store(url)


def test_the_console_contains_a_checkpoint_store_path_but_passes_s3(tmp_path):
    from examlops.cli import surface as s

    by_path = {c["path"]: c for c in s.build_catalog()["commands"]}
    cmd = by_path["pipeline distributed run"]
    inv = s.build_argv(
        cmd,
        {"scheduler": True, "model": "Demo", "checkpoint_store": "s3://ckpt/dist"},
        workspace=tmp_path,
    )
    assert "--checkpoint-store=s3://ckpt/dist" in inv.argv
    with pytest.raises(s.SurfaceError):
        s.build_argv(
            cmd,
            {"scheduler": True, "model": "Demo", "checkpoint_store": "/etc/ckpt"},
            workspace=tmp_path,
        )
    with pytest.raises(s.SurfaceError):  # climbs out of the workspace
        s.build_argv(
            cmd,
            {"scheduler": True, "model": "Demo", "checkpoint_store": "../outside"},
            workspace=tmp_path,
        )
    inv = s.build_argv(
        cmd, {"scheduler": True, "model": "Demo", "checkpoint_store": "ckpt"}, workspace=tmp_path
    )
    assert f"--checkpoint-store={tmp_path.resolve() / 'ckpt'}" in inv.argv
    # the chat image keeps its own schemes: s3:// is not a URL there
    chat = by_path["serve llm chat"]
    with pytest.raises(s.SurfaceError):
        s.build_argv(
            chat, {"model": "m", "message": "hi", "image": ["/abs/x.png"]}, workspace=tmp_path
        )


# ── gates on the scheduler path, model identity, unreadable store ─────────────


def _invoke(args):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    return CliRunner().invoke(app, args)


def _never_supervise(monkeypatch):
    called: list = []
    monkeypatch.setattr(scheduled, "supervise_scheduled", lambda *a, **k: called.append(a))
    return called


def test_distributed_run_scheduler_is_held_by_the_admission_gate(tmp_path, monkeypatch):
    from examlops.cli import _admission_gate

    monkeypatch.setenv(
        "RAY_MODELS_DIR", str(_pack(tmp_path, "strategy: ddp\nnodes: 2\ngpus_per_node: 4"))
    )
    gate: dict = {}
    order: list = []

    @contextlib.contextmanager
    def fake_gate(*, project=None, model=None, gpus=0):
        gate.update(model=model, gpus=gpus)
        order.append("admit")
        yield
        order.append("release")

    def fake(plan, run_id, run_dir=None, **kw):
        order.append("run")
        return scheduled.ScheduledRun(run_id, tmp_path, "slurm", plan.to_dict(), "complete")

    monkeypatch.setattr(_admission_gate, "pipeline_run_gate", fake_gate)
    monkeypatch.setattr(scheduled, "supervise_scheduled", fake)
    r = _invoke(["pipeline", "distributed", "run", "--scheduler", "--model", "Demo"])
    assert r.exit_code == 0, r.output
    assert gate == {"model": "Demo", "gpus": 8} and order == ["admit", "run", "release"]


def test_distributed_run_scheduler_consults_the_budget_gate(tmp_path, monkeypatch):
    import typer

    from examlops.cli.commands import pipeline as pipeline_cmd

    monkeypatch.setenv("RAY_MODELS_DIR", str(_pack(tmp_path, "strategy: ddp")))
    seen: list = []

    def over_budget(project, model):
        seen.append(model)
        raise typer.Exit(1)

    monkeypatch.setattr(pipeline_cmd, "_enforce_budget_gate", over_budget)
    called = _never_supervise(monkeypatch)
    r = _invoke(["pipeline", "distributed", "run", "--scheduler", "--model", "Demo"])
    assert r.exit_code == 1 and seen == ["Demo"] and called == []


def test_an_unknown_model_is_refused_not_trained_on_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("RAY_MODELS_DIR", str(_pack(tmp_path, "strategy: ddp")))
    called = _never_supervise(monkeypatch)
    r = _invoke(["pipeline", "distributed", "run", "--scheduler", "--model", "Demmo"])
    assert r.exit_code == 2 and "No model YAML named 'Demmo'" in r.output
    r = _invoke(["pipeline", "run", "--model", "Demmo", "--distributed"])
    assert r.exit_code == 2 and "No model YAML" in r.output
    assert called == []


def test_pipeline_run_distributed_refuses_to_train_the_reference_as_the_model(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RAY_MODELS_DIR", str(_pack(tmp_path, "strategy: ddp")))
    called = _never_supervise(monkeypatch)
    r = _invoke(["pipeline", "run", "--model", "Demo", "--distributed"])
    assert r.exit_code == 2 and "entrypoint" in r.output
    assert called == []


class _DownStore(durable.CheckpointStore):
    def list_steps(self, run_id):
        raise ConnectionError("store unreachable")


class _DownFs:
    def ls(self, path, detail=False):
        raise PermissionError("access denied")


def test_an_unreachable_store_is_recorded_not_read_as_empty(tmp_path):
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    reset_dropped_audit_events()
    run = tmp_path / "run"
    assert durable.restore_latest(_DownStore("s3://b/p", "b/p"), "rv-down", run) is None
    assert "checkpoint_store_unreadable" in _actions("rv-down")
    assert not dropped_audit_events()
    # The listing itself no longer turns a denied/unreachable store into "no checkpoints".
    with pytest.raises(PermissionError):
        durable.CheckpointStore("s3://b/p", "b/p", _DownFs()).list_steps("rv-down")
    assert durable.CheckpointStore(str(tmp_path / "s"), str(tmp_path / "s")).list_steps("x") == []
