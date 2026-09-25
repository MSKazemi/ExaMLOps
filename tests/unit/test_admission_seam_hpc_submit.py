"""ADR 0116 — HPC-job submission is admitted through the seam, and its quota follows the job.

``examlops.scheduler_jobs.submit`` is where every platform job (asset builds, embedding reindexes)
reaches the phase-23 scheduler. With the dispatch kill-switch on it now asks ``decide()`` first,
holds the job's GPUs from admission, binds that reservation to the scheduler job id the moment it
exists, and gives it back at the terminal-state chokepoint (``update_hpc_job``) — including when
the job fails. With the switch off nothing about submission changes.
"""

from __future__ import annotations

import pytest

from examlops import scheduler_jobs
from examlops.admission_seam import completion, dispatch
from examlops.data import quota_reservations as store


class FakeAdapter:
    """The execution seam's submit verb, recording what it was asked."""

    def __init__(self, job_id="777", fail=False):
        self.working_dir = "/wd"
        self.job_id = job_id
        self.fail = fail
        self.submitted: list[dict] = []

    def submit_job(self, script_path=None, resources=None, training_data=None, remote_dir=None):
        if self.fail:
            raise RuntimeError("sbatch: invalid partition")
        self.submitted.append({"script": script_path, "resources": resources})
        return self.job_id


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "slurm")
    for var in (
        dispatch.ENABLED_ENV,
        "EXAMLOPS_ADMISSION_POLICY",
        "EXAMLOPS_ADMISSION_QUOTAS",
        "EXAMLOPS_ADMISSION_GATES",
        "EXAMLOPS_PROJECT",
        "EXAMLOPS_HPC_REMOTE_WORKDIR",
    ):
        monkeypatch.delenv(var, raising=False)
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def _count(pdb, table):
    with pdb.get_db() as conn:
        return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


def _actions(pdb):
    with pdb.get_db() as conn:
        return [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE source='admission-seam' ORDER BY id"
            )
        ]


def test_disabled_submission_is_the_direct_call_and_writes_nothing(db, tmp_path):
    a = FakeAdapter()
    job = scheduler_jobs.submit(a, tmp_path / "run.sh", "k1", {"gpus": 2})
    assert job == "777" and len(a.submitted) == 1
    assert _count(db, "quota_reservations") == 0
    assert _count(db, "audit_events") == 0


def test_admitted_job_holds_its_gpus_until_its_terminal_state(db, tmp_path, monkeypatch):
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    monkeypatch.setenv("EXAMLOPS_PROJECT", "proj")
    a = FakeAdapter(job_id="4242")
    job = scheduler_jobs.submit(a, tmp_path / "run.sh", "k1", {"gpus": "3", "nodes": 1})
    assert job == "4242"

    held = store.held_by_holder(completion.holder_for_job("slurm", "4242"))
    assert len(held) == 1 and held[0]["state"] == "committed" and held[0]["gpus"] == 3
    assert held[0]["project"] == "proj"
    assert store.held_totals("proj")["gpus"] == 3

    # The job is recorded and later fails: the one chokepoint returns the quota.
    scheduler_jobs.record_job(job, "slurm", "asset:x", {"gpus": 3})
    scheduler_jobs.finish_job(job, "slurm", {"state": "FAILED", "exit_code": 1})
    assert store.held_totals("proj")["gpus"] == 0
    assert _actions(db)[-1] == "quota_released"
    assert "quota_committed" in _actions(db)


def test_a_committed_job_reservation_is_not_swept_by_ttl(db, tmp_path, monkeypatch):
    """A running job outlives the admission TTL; its quota must not lapse under it."""
    import time

    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    scheduler_jobs.submit(FakeAdapter(job_id="9"), tmp_path / "run.sh", "k", {"gpus": 1})
    later = time.time() + 10 * 3600
    assert store.expire_due(now=later) == []
    assert store.held_totals("default", now=later)["gpus"] == 1


def test_a_scheduler_that_refuses_the_job_returns_the_quota(db, tmp_path, monkeypatch):
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    with pytest.raises(RuntimeError, match="invalid partition"):
        scheduler_jobs.submit(FakeAdapter(fail=True), tmp_path / "run.sh", "k", {"gpus": 2})
    assert store.held_totals("default")["gpus"] == 0
    rows = store.list_reservations(limit=10)
    assert len(rows) == 1 and rows[0]["state"] == "released"


def test_a_refused_admission_submits_nothing(db, tmp_path, monkeypatch):
    from examlops.data.projects import create_project

    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    monkeypatch.setenv("EXAMLOPS_PROJECT", "small")
    create_project("small", gpu_limit=2)
    a = FakeAdapter()
    with pytest.raises(dispatch.AdmissionRefused, match="gpu concurrency"):
        scheduler_jobs.submit(a, tmp_path / "run.sh", "k", {"gpus": 4})
    assert a.submitted == [], "nothing may reach the scheduler after a refusal"
    assert "admission_refused" in _actions(db)


def test_a_gate_refusal_also_blocks_submission(db, tmp_path, monkeypatch):
    from examlops import project_finops

    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    monkeypatch.setenv("EXAMLOPS_ADMISSION_GATES", "budget")
    monkeypatch.setattr(
        project_finops,
        "budget_status",
        lambda project, **k: {"budget": {"gpu_hours_budget": 1}, "breaches": ["b"]},
    )
    a = FakeAdapter()
    with pytest.raises(dispatch.AdmissionRefused, match="gate budget"):
        scheduler_jobs.submit(a, tmp_path / "run.sh", "k", {"gpus": 1})
    assert a.submitted == []


def test_a_lapsed_reservation_is_recorded_not_silently_unbound(db, tmp_path, monkeypatch):
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    job = dispatch.submit_admitted(
        dispatch.request_for_hpc_job({"gpus": 1}),
        scheduler="slurm",
        submit=lambda: "55",
        ttl_s=-1.0,  # already lapsed by the time submit() returns
    )
    assert job == "55"
    assert "quota_bind_lapsed" in _actions(db)
    assert store.held_by_holder(completion.holder_for_job("slurm", "55")) == []


@pytest.mark.parametrize(
    "scheduler,resources,expected",
    [
        ("slurm", {"gpus": 4, "cpus_per_task": "8", "nodes": 2}, (4, 8, 2)),
        (None, None, (0, 0, 1)),
        # Slurm's typed spelling is a count too; reading it as 0 would admit a GPU job for free.
        ("slurm", {"gpus": "a100:2"}, (2, 0, 1)),
        ("slurm", {"gpus": "a100:2,v100:1"}, (3, 0, 1)),
        # --gpus-per-node is per node: the job holds per_node x nodes.
        ("slurm", {"gpus_per_node": 4, "nodes": 2}, (8, 0, 2)),
        # a node range holds quota for its upper bound
        ("slurm", {"gpus_per_node": 2, "nodes": "2-4"}, (8, 0, 4)),
        # Flux's -g is per slot, one slot per node without ntasks
        ("flux", {"gpus": 4, "nodes": 2}, (8, 0, 2)),
        ("flux", {"gpus": 2, "nodes": 1, "ntasks": 3}, (6, 0, 1)),
    ],
)
def test_request_for_hpc_job_counts_gpus_as_the_backend_allocates(scheduler, resources, expected):
    r = dispatch.request_for_hpc_job(resources, project="p", scheduler=scheduler).resources
    assert (r.gpus, r.cpus, r.nodes) == expected


@pytest.mark.parametrize("bad", [{"gpus": "lots"}, {"gpus_per_node": "a100"}, {"nodes": "x"}])
def test_an_unreadable_gpu_or_node_count_is_refused_not_admitted_as_zero(bad):
    with pytest.raises(ValueError, match="cannot read"):
        dispatch.request_for_hpc_job(bad, project="p", scheduler="slurm")


def test_gpus_per_node_is_held_against_the_project_quota(db, tmp_path, monkeypatch):
    """The fail-open this closes: a per-node GPU ask used to be admitted as 0 GPUs."""
    from examlops.data.projects import create_project

    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    monkeypatch.setenv("EXAMLOPS_PROJECT", "capped")
    create_project("capped", gpu_limit=4)
    a = FakeAdapter()
    with pytest.raises(dispatch.AdmissionRefused, match="gpu concurrency"):
        scheduler_jobs.submit(a, tmp_path / "run.sh", "k", {"gpus_per_node": 4, "nodes": 2})
    assert a.submitted == []


def test_an_unreadable_gpu_value_submits_nothing(db, tmp_path, monkeypatch):
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    a = FakeAdapter()
    with pytest.raises(ValueError, match="refusing to admit"):
        scheduler_jobs.submit(a, tmp_path / "run.sh", "k", {"gpus": "several"})
    assert a.submitted == []


def test_bind_and_commit_is_atomic_and_single_use(db):
    out = store.reserve("r1", "p", gpus=1, ttl_s=100.0, holder="run:x")
    assert out["ok"]
    assert store.bind_and_commit("r1", "hpc:slurm:1") is True
    assert store.get("r1")["holder"] == "hpc:slurm:1"
    assert store.bind_and_commit("r1", "hpc:slurm:2") is False, "a committed row is not rebound"
    assert store.get("r1")["holder"] == "hpc:slurm:1"


# ── serving allocations (HPC-launched LLM endpoints) ────────────────────────────────────────
class FakeServeAdapter(FakeAdapter):
    def __init__(self, job_id="900"):
        super().__init__(job_id=job_id)
        self.cancelled: list[str] = []

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)


def _launcher(monkeypatch, adapter):
    from examlops import llm_endpoints as le

    monkeypatch.setattr(le, "_scheduler_adapter", lambda scheduler: adapter)
    return le, le.HpcLauncher(scheduler="slurm")


def test_a_serving_allocation_is_admitted_and_released_on_stop(db, tmp_path, monkeypatch):
    from examlops.data.serving import upsert_llm_endpoint

    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    adapter = FakeServeAdapter(job_id="900")
    le, launcher = _launcher(monkeypatch, adapter)
    spec = le.EndpointSpec(model="qwen", hf_model_id="hf", nodes=2, gpus=4, work_dir=str(tmp_path))

    handle = launcher.start(spec)
    assert handle.job_id == "900"
    held = store.held_by_holder(completion.holder_for_job("slurm", "900"))
    assert [r["gpus"] for r in held] == [8], "GPUs per node x nodes are held for the server"

    upsert_llm_endpoint("qwen", hf_model_id="hf", launcher="slurm", job_id="900")
    launcher.stop("qwen")
    assert adapter.cancelled == ["900"]
    assert store.held_totals("default")["gpus"] == 0, "stopping the server returns its GPUs"


def test_a_refused_serving_allocation_is_never_submitted(db, tmp_path, monkeypatch):
    from examlops.data.projects import create_project

    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    monkeypatch.setenv("EXAMLOPS_PROJECT", "tiny")
    create_project("tiny", gpu_limit=2)
    adapter = FakeServeAdapter()
    le, launcher = _launcher(monkeypatch, adapter)
    spec = le.EndpointSpec(model="big", hf_model_id="hf", nodes=1, gpus=4, work_dir=str(tmp_path))
    with pytest.raises(le.LauncherError, match="gpu concurrency"):
        launcher.start(spec)
    assert adapter.submitted == []


def test_serving_with_admission_off_is_unchanged(db, tmp_path, monkeypatch):
    adapter = FakeServeAdapter(job_id="901")
    le, launcher = _launcher(monkeypatch, adapter)
    spec = le.EndpointSpec(model="m", hf_model_id="hf", nodes=1, gpus=1, work_dir=str(tmp_path))
    assert launcher.start(spec).job_id == "901"
    assert _count(db, "quota_reservations") == 0


# ── reconciling committed job reservations (no terminal state was ever recorded) ────────────
def _commit_job(job_id, gpus=2, scheduler="slurm"):
    return dispatch.submit_admitted(
        dispatch.request_for_hpc_job({"gpus": gpus}),
        scheduler=scheduler,
        submit=lambda: job_id,
    )


def test_reconcile_releases_a_job_that_ended_without_a_recorded_state(db, monkeypatch):
    """A server that hit its wall time: nothing called update_hpc_job, stop() never ran."""
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    _commit_job("100")  # ended (TIMEOUT)
    _commit_job("101")  # still running
    _commit_job("102")  # the scheduler cannot say
    _commit_job("103")  # sacct spelling
    states = {"100": "TIMEOUT", "101": "RUNNING", "103": "CANCELLED by 0"}

    def status_of(scheduler, job_id):
        if job_id == "102":
            raise RuntimeError("ssh: connection refused")
        return states[job_id]

    assert store.held_totals("default")["gpus"] == 8
    out = completion.reconcile_job_reservations(status_of)
    assert sorted(r["holder"] for r in out["released"]) == ["hpc:slurm:100", "hpc:slurm:103"]
    assert [r["holder"] for r in out["still_running"]] == ["hpc:slurm:101"]
    assert [r["holder"] for r in out["unverified"]] == ["hpc:slurm:102"]
    assert store.held_totals("default")["gpus"] == 4, "only the two ended jobs gave GPUs back"
    # idempotent: a second pass releases nothing more
    again = completion.reconcile_job_reservations(status_of)
    assert again["released"] == []


def test_reconcile_never_releases_on_an_unknown_state(db, monkeypatch):
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    _commit_job("7", scheduler="mock")
    out = completion.reconcile_job_reservations(lambda s, j: "UNKNOWN")
    assert out["released"] == [] and store.held_totals("default")["gpus"] == 2


def test_reconcile_dry_run_changes_nothing(db, monkeypatch):
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    _commit_job("8")
    out = completion.reconcile_job_reservations(lambda s, j: "COMPLETED", dry_run=True)
    assert [r["holder"] for r in out["released"]] == ["hpc:slurm:8"]
    assert store.held_totals("default")["gpus"] == 2


def test_the_chokepoint_reads_sacct_spellings_as_terminal():
    assert completion.normalize_outcome("CANCELLED by 1234") == "cancelled"
    assert completion.normalize_outcome("CANCELLED+") == "cancelled"
    assert completion.normalize_outcome("RUNNING") is None


def test_exa_admission_reconcile_releases_through_the_real_command(db, monkeypatch):
    import json

    from typer.testing import CliRunner

    from examlops.cli import _output
    from examlops.cli.commands import admission_cmd

    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")
    _commit_job("300")

    class Ended:
        def get_job_status(self, job_id):
            return {"state": "COMPLETED"}

    monkeypatch.setattr(scheduler_jobs, "scheduler_adapter", lambda name=None: Ended())
    monkeypatch.setattr(_output, "json_mode", True)
    result = CliRunner().invoke(admission_cmd.app, ["reconcile"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert [r["holder"] for r in out["released"]] == ["hpc:slurm:300"]
    assert store.held_totals("default")["gpus"] == 0


def test_a_bookkeeping_failure_after_submit_does_not_orphan_the_job(db, tmp_path, monkeypatch):
    """The scheduler accepted the job; a failing bind must not report the submission as failed."""
    monkeypatch.setenv(dispatch.ENABLED_ENV, "1")

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "bind_and_commit", broken)
    a = FakeAdapter(job_id="606")
    assert scheduler_jobs.submit(a, tmp_path / "run.sh", "k", {"gpus": 1}) == "606"
    assert len(a.submitted) == 1
    assert "quota_bind_failed" in _actions(db)
