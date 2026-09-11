# tests/unit/test_asset_scheduler_job.py
"""ADR 0036 clause 3 — materializing "via the scheduler" has to run the build.

Found 2026-09-11 and reproduced. `SchedulerOrchestrator` called `submit_job(script_path=None, …)`
and put its command in `training_data`, which the real adapters ignore:

- Slurm and Flux both raised `JobSubmissionError("script_path is required …")`, so
  `exa assets materialize --orchestrator scheduler` failed on every real scheduler.
- The mock accepted the job but executes only in `wait_until_complete`, which was never called.
  The production function never ran, and the version was bumped anyway: a phantom build.

The earlier tests passed because their stand-in adapter accepted anything. These drive the
**real** mock, Slurm and Flux adapters. For Slurm and Flux an executor runs the submitted
`run.sh` with bash, so the generated script is executed, not just inspected.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_DIR = ROOT / "platform" / "infra" / "slurm-adapter"
for p in (str(ROOT / "platform" / "cli" / "src"), str(_ADAPTER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from executor import CompletedCommand  # noqa: E402

from examlops import assets  # noqa: E402
from examlops.assets import (  # noqa: E402
    AssetBuildError,
    SchedulerOrchestrator,
    declare_asset,
    materialize,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in (
        "EXAMLOPS_ASSET_ORCHESTRATOR",
        "EXAMLOPS_HPC_REMOTE_PYTHON",
        "EXAMLOPS_HPC_REMOTE_REPO",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EXAMLOPS_HPC_REMOTE_WORKDIR", str(tmp_path / "remote"))
    monkeypatch.setenv("EXAMLOPS_TEST_ASSET_MARKER", str(tmp_path / "marker.json"))
    monkeypatch.setenv("EXAMLOPS_ASSET_JOB_DIR", str(tmp_path / "asset-jobs"))


@pytest.fixture
def fx():
    from tests.unit import _asset_job_fixtures

    return _asset_job_fixtures


def _marker(tmp_path):
    return json.loads((tmp_path / "marker.json").read_text())


def _declare(monkeypatch, name, fn, *, deps=(), resources=None):
    declare_asset(name, kind="model", deps=list(deps), fn=fn, resources=resources)
    monkeypatch.setitem(
        assets._REGISTRY,
        name,
        assets.AssetDef(name=name, kind="model", deps=list(deps), fn=fn, resources=resources),
    )


class ScriptRunningExecutor:
    """A cluster in a box: `sbatch` / `flux batch` run the submitted script with bash, now, and
    the status commands report how it exited. Everything else is a no-op that succeeds."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.calls: list[list[str]] = []
        self.returncode: int | None = None

    def run(self, cmd, *, timeout=None, cwd=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[0] == "mkdir":
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        elif cmd[0] == "sbatch" or cmd[:2] == ["flux", "batch"]:
            self.returncode = subprocess.run(["bash", cmd[-1]], capture_output=True).returncode
            out = f"Submitted batch job {self.job_id}" if cmd[0] == "sbatch" else self.job_id
            return CompletedCommand(0, out, "")
        elif cmd[0] == "sacct" and "--format=State,ExitCode,Start,End" in cmd:
            state = "COMPLETED" if self.returncode == 0 else "FAILED"
            return CompletedCommand(0, f"{state}|{self.returncode}:0|t0|t1", "")
        elif cmd[:2] == ["flux", "jobs"]:
            result = "COMPLETED" if self.returncode == 0 else "FAILED"
            return CompletedCommand(0, f"INACTIVE {result} t0 t1 {self.returncode}", "")
        return CompletedCommand(0, "", "")

    def put(self, local, remote):
        Path(remote).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(local, remote)

    def get(self, remote, local):
        raise FileNotFoundError(remote)

    def close(self):
        pass


def _submitted_script(executor) -> str:
    submit = next(c for c in executor.calls if c[0] == "sbatch" or c[:2] == ["flux", "batch"])
    return Path(submit[-1]).read_text()


# ── the build actually runs, on each real adapter ────────────────────────────


def test_mock_scheduler_runs_the_production_function_in_a_job(monkeypatch, tmp_path, fx):
    from mock_slurm_adapter import MockSlurmAdapter

    monkeypatch.setattr(
        assets, "_scheduler_adapter", lambda: MockSlurmAdapter(working_dir=tmp_path / "mock")
    )
    declare_asset("JobUp", kind="dataset")
    materialize("JobUp", force=True)
    _declare(monkeypatch, "JobModel", fx.build_marker, deps=["JobUp"])

    materialize("JobModel", force=True, no_deps=True, orchestrator="scheduler")

    built = _marker(tmp_path)
    assert built["pid"] != os.getpid(), "the build must run in the job, not in this process"
    assert built["upstream"] == {"JobUp": 1}
    from examlops.data import get_asset

    assert get_asset("JobModel")["current_version"] == 1


@pytest.mark.parametrize("scheduler", ["slurm", "flux"])
def test_real_adapters_accept_and_run_the_job(monkeypatch, tmp_path, fx, scheduler):
    """The regression itself: both used to raise `script_path is required`."""
    executor = ScriptRunningExecutor("4242" if scheduler == "slurm" else "fAbC")
    if scheduler == "slurm":
        from adapter import RealSlurmAdapter

        adapter = RealSlurmAdapter(executor=executor, working_dir=str(tmp_path / "jobs"))
    else:
        from flux_adapter import FluxAdapter

        adapter = FluxAdapter(
            executor=executor,
            working_dir=str(tmp_path / "jobs"),
            remote_workdir=str(tmp_path / "remote"),
        )
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: adapter)
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", scheduler)

    result = SchedulerOrchestrator().run(assets.AssetDef("RealJob", fn=fx.build_marker), {"u": 2})

    assert result == {
        "orchestrator": "scheduler",
        "hpc_job_id": executor.job_id,
        "scheduler": scheduler,
    }
    assert _marker(tmp_path)["upstream"] == {"u": 2}


def test_a_failed_job_raises_and_records_no_version(monkeypatch, tmp_path, fx):
    from mock_slurm_adapter import MockSlurmAdapter

    monkeypatch.setattr(
        assets, "_scheduler_adapter", lambda: MockSlurmAdapter(working_dir=tmp_path / "mock")
    )
    _declare(monkeypatch, "JobBad", fx.build_fails)

    with pytest.raises(AssetBuildError, match=r"ended FAILED[\s\S]*partition 7 is corrupt"):
        materialize("JobBad", force=True, orchestrator="scheduler")

    from examlops.data import get_asset

    assert get_asset("JobBad")["current_version"] == 0, "a failed build is not a version"


def test_the_job_is_tracked_like_any_hpc_job(monkeypatch, tmp_path, fx):
    from mock_slurm_adapter import MockSlurmAdapter

    monkeypatch.setattr(
        assets, "_scheduler_adapter", lambda: MockSlurmAdapter(working_dir=tmp_path / "mock")
    )
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "mock")
    _declare(monkeypatch, "JobTracked", fx.build_marker)

    result = SchedulerOrchestrator().run(assets.AssetDef("JobTracked", fn=fx.build_marker), {})

    from examlops.data.hpc import get_hpc_jobs

    rows = [r for r in get_hpc_jobs("asset:JobTracked") if r["job_id"] == result["hpc_job_id"]]
    assert rows and rows[0]["model"] == "asset:JobTracked" and rows[0]["state"] == "COMPLETED"


# ── what a job may and may not be sent ───────────────────────────────────────


def test_the_job_runs_the_function_and_never_the_graph(monkeypatch, tmp_path, fx):
    """Re-entering `exa assets materialize` from the job is what made the old design either
    resubmit itself or need `platform.db` on the cluster."""
    executor = ScriptRunningExecutor("7")
    from adapter import RealSlurmAdapter

    adapter = RealSlurmAdapter(executor=executor, working_dir=str(tmp_path / "jobs"))
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: adapter)
    SchedulerOrchestrator().run(assets.AssetDef("NoGraph", fn=fx.build_marker), {})

    script = _submitted_script(executor)
    assert "-m examlops.assets.job" in script
    assert "tests.unit._asset_job_fixtures:build_marker" in script
    assert "materialize" not in script
    assert "set -euo pipefail" in script


def test_resources_reach_the_scheduler(monkeypatch, tmp_path, fx):
    executor = ScriptRunningExecutor("9")
    from adapter import RealSlurmAdapter

    adapter = RealSlurmAdapter(executor=executor, working_dir=str(tmp_path / "jobs"))
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: adapter)
    definition = assets.AssetDef(
        "Big", fn=fx.build_marker, resources={"gpus": 4, "time": "2:00:00"}
    )

    SchedulerOrchestrator().run(definition, {})

    sbatch = next(c for c in executor.calls if c[0] == "sbatch")
    assert "--job-name=asset-Big" in sbatch and "--time=2:00:00" in sbatch


def test_a_hostile_asset_name_reaches_the_job_as_data(monkeypatch, tmp_path, fx):
    """Every value in run.sh is shell-quoted: the job sees the name byte-for-byte, and nothing
    in it is executed."""
    argv_file = tmp_path / "argv.json"
    stub = tmp_path / "python-stub"
    stub.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(argv_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    stub.chmod(0o700)
    monkeypatch.setenv("EXAMLOPS_HPC_REMOTE_PYTHON", str(stub))
    pwned = tmp_path / "pwned"
    name = f'x\'; touch {pwned}; echo "$(touch {pwned})" `touch {pwned}`'
    executor = ScriptRunningExecutor("5")
    from adapter import RealSlurmAdapter

    adapter = RealSlurmAdapter(executor=executor, working_dir=str(tmp_path / "jobs"))
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: adapter)

    SchedulerOrchestrator().run(assets.AssetDef(name, fn=fx.build_marker), {})

    assert not pwned.exists()
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("--asset") + 1] == name


def test_the_script_is_private_to_its_owner(monkeypatch, tmp_path, fx):
    executor = ScriptRunningExecutor("6")
    from adapter import RealSlurmAdapter

    adapter = RealSlurmAdapter(executor=executor, working_dir=str(tmp_path / "jobs"))
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: adapter)
    monkeypatch.setenv("SOME_SECRET_TOKEN", "s3cr3t-value")

    SchedulerOrchestrator().run(assets.AssetDef("Priv", fn=fx.build_marker), {})

    local = next((tmp_path / "asset-jobs").glob("asset-*/run.sh"))
    assert stat.S_IMODE(local.stat().st_mode) == 0o700
    assert "s3cr3t-value" not in local.read_text(), "no environment value is written into a job"


def test_job_scripts_are_never_written_into_the_adapter_working_dir(monkeypatch, tmp_path, fx):
    """The mock adapter's default working directory is inside the repository, and a run.sh
    holds this host's absolute paths — one `git add -A` from being published."""
    from mock_slurm_adapter import MockSlurmAdapter

    monkeypatch.delenv("EXAMLOPS_ASSET_JOB_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    adapter_wd = tmp_path / "adapter-wd"
    monkeypatch.setattr(
        assets, "_scheduler_adapter", lambda: MockSlurmAdapter(working_dir=adapter_wd)
    )

    SchedulerOrchestrator().run(assets.AssetDef("Placed", fn=fx.build_marker), {})

    assert not list(adapter_wd.rglob("run.sh"))
    assert list((tmp_path / "cache" / "examlops" / "asset-jobs").glob("asset-*/run.sh"))


def test_a_closure_builds_locally_and_says_why(monkeypatch):
    submitted = []
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: submitted.append(1))
    calls = []

    result = SchedulerOrchestrator().run(
        assets.AssetDef("Closure", fn=lambda **kw: calls.append(1)), {}
    )

    assert calls == [1] and not submitted
    assert "not importable" in result["fallback"]


def test_the_job_runs_the_submitters_examlops_not_a_stale_install(monkeypatch, tmp_path, fx):
    """Found replaying BL-049: with the submitter's examlops on a sys.path insert and a stale
    copy installed in the interpreter, the job imported the stale one. A shadow package on the
    job's inherited PYTHONPATH stands in for it here."""
    from mock_slurm_adapter import MockSlurmAdapter

    shadow = tmp_path / "shadow"
    (shadow / "examlops" / "assets").mkdir(parents=True)
    (shadow / "examlops" / "__init__.py").write_text("")
    (shadow / "examlops" / "assets" / "__init__.py").write_text("")
    (shadow / "examlops" / "assets" / "job.py").write_text("raise SystemExit('stale examlops')\n")
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    monkeypatch.setattr(
        assets, "_scheduler_adapter", lambda: MockSlurmAdapter(working_dir=tmp_path / "mock")
    )

    result = SchedulerOrchestrator().run(assets.AssetDef("Fresh", fn=fx.build_marker), {"u": 1})

    assert result["hpc_job_id"] and _marker(tmp_path)["upstream"] == {"u": 1}


def test_another_interpreters_site_packages_is_not_exported(monkeypatch, tmp_path):
    """Putting the submitter's site-packages in front of a different Python's own would mix two
    sets of compiled libraries; to the same interpreter it is harmless and is kept."""
    import click

    fn = click.echo  # resolves from site-packages
    site = str(assets._import_root(fn))
    assert site.endswith("-packages")

    monkeypatch.setenv("EXAMLOPS_HPC_REMOTE_PYTHON", "/opt/cluster/python3.13")
    other = assets._job_script("a", "click.utils:echo", fn, {})
    assert site not in other

    monkeypatch.setenv("EXAMLOPS_HPC_REMOTE_PYTHON", sys.executable)
    same = assets._job_script("a", "click.utils:echo", fn, {})
    assert site in same


def test_a_wrapper_borrowing_a_name_is_not_sent_to_a_job(monkeypatch, fx):
    """`functools.wraps` copies the target's module and qualname. A job importing that name
    would run the target, not the wrapper — so it is resolved and compared, not trusted."""
    import functools

    submitted, calls = [], []
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: submitted.append(1))
    wrapper = functools.wraps(fx.build_marker)(lambda **kw: calls.append(1))

    result = SchedulerOrchestrator().run(assets.AssetDef("Wrapped", fn=wrapper), {})

    assert calls == [1] and not submitted
    assert "different object" in result["fallback"]


def test_no_production_function_submits_nothing(monkeypatch):
    submitted = []
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: submitted.append(1))

    result = SchedulerOrchestrator().run(assets.AssetDef("Declared", fn=None), {})

    assert not submitted and "no production function" in result["fallback"]


def test_a_refused_job_raises_instead_of_building_on_this_host(monkeypatch, tmp_path, fx):
    class Refusing:
        working_dir = tmp_path / "jobs"

        def submit_job(self, **kw):
            raise RuntimeError("sbatch: error: invalid account")

    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: Refusing())
    _declare(monkeypatch, "Refused", fx.build_marker)

    with pytest.raises(AssetBuildError, match="refused the job.*invalid account"):
        materialize("Refused", force=True, orchestrator="scheduler")

    assert not (tmp_path / "marker.json").exists(), "the build must not quietly run here instead"
    from examlops.data import get_asset

    assert get_asset("Refused")["current_version"] == 0


# ── the job runner ───────────────────────────────────────────────────────────


def _job(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "examlops.assets.job", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
        cwd=str(ROOT),
    )


def test_the_job_needs_no_datastore(tmp_path):
    """Importing a module re-runs its `@asset` decorators; in a job that must not reach for a
    platform.db the cluster may not have."""
    r = _job(
        "--asset", "job_fixture_decorated",
        "--entrypoint", "tests.unit._asset_job_fixtures:decorated",
        "--upstream", '{"a": 3}',
        env={
            "PLATFORM_DB": "/proc/examlops-no-such-dir/platform.db",
            "EXAMLOPS_TEST_ASSET_MARKER": str(tmp_path / "marker.json"),
        },
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert _marker(tmp_path)["upstream"] == {"a": 3}


@pytest.mark.parametrize(
    "args,code",
    [
        (["--entrypoint", "os:system; rm -rf /"], 2),
        (["--entrypoint", "tests.unit._asset_job_fixtures:build_marker", "--upstream", "[1]"], 2),
        (["--entrypoint", "tests.unit._asset_job_fixtures:nope"], 3),
        (["--entrypoint", "tests.unit._asset_job_fixtures:build_fails"], 1),
    ],
)
def test_the_job_exit_code_says_what_went_wrong(args, code, tmp_path):
    r = _job("--asset", "x", *args, env={"EXAMLOPS_TEST_ASSET_MARKER": str(tmp_path / "m")})
    assert r.returncode == code, r.stderr
