# tests/unit/test_no_checkout_pollution.py
"""Nothing the platform runs writes into the repository it was run from (BL-074).

Found while verifying BL-073: a probe over the whole unit suite showed four tests leaving something
behind in the checkout — `mlruns/` (MLflow artifacts), `slurm_jobs/` and `flux_jobs/` (scheduler
working directories, created by merely *constructing* an adapter) and `.pytest_cache` (from the
`exa` command that runs pytest). Each was invisible only because a **machine-local** ignore file
hid it: `.git/info/exclude` is not in any repository, so a fresh clone on another machine has none
of them, and a whole-tree `add` there would publish local run data and generated job scripts —
which carry absolute local paths.

The mock scheduler's default was the worst of them: `platform/infra/slurm-adapter/mock_hpc_jobs`,
*inside the installed package*, so a mock run wrote into the checkout by design.

Three fixes, held here: a job directory is resolved once (`EXAMLOPS_HPC_WORKDIR`, else the cache
directory for the mock, else the historical relative default for a real cluster), an adapter
creates nothing until a job needs it, and MLflow artifacts follow the instance-data root.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "platform" / "cli" / "src"))
sys.path.insert(0, str(REPO_ROOT / "platform" / "infra" / "slurm-adapter"))

from examlops.scheduler_jobs import adapter_working_dir, job_dir  # noqa: E402


@pytest.fixture(autouse=True)
def _no_knob(monkeypatch):
    """Each test says for itself whether the knob is set."""
    monkeypatch.delenv("EXAMLOPS_HPC_WORKDIR", raising=False)


# ── where a job directory goes ───────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["mock", "slurm", "flux"])
def test_the_knob_decides_when_it_is_set(kind, tmp_path, monkeypatch):
    """A cluster points this at the shared filesystem its compute nodes can see; CI at tmp."""
    monkeypatch.setenv("EXAMLOPS_HPC_WORKDIR", str(tmp_path))

    assert adapter_working_dir(kind) == tmp_path / kind


def test_the_mock_defaults_outside_the_repository():
    """It used to default to a folder inside the installed package."""
    resolved = adapter_working_dir("mock")

    assert resolved == job_dir().parent / "mock_hpc_jobs"
    assert REPO_ROOT not in resolved.resolve().parents, resolved


@pytest.mark.parametrize("kind,expected", [("slurm", "slurm_jobs"), ("flux", "flux_jobs")])
def test_a_real_cluster_keeps_its_historical_default(kind, expected):
    """`sbatch --output` paths must resolve on the cluster, so the submit directory stays the
    operator's choice — moving it silently would break a live site."""
    assert adapter_working_dir(kind) == Path(expected)


# ── constructing an adapter writes nothing ───────────────────────────────────


def test_no_adapter_touches_the_filesystem_when_built(tmp_path, monkeypatch):
    """`get_adapter()` runs in `exa status`, in the dashboard and in tests — none is submitting."""
    monkeypatch.setenv("EXAMLOPS_HPC_WORKDIR", str(tmp_path / "jobs"))
    monkeypatch.chdir(tmp_path)
    from adapter import RealSlurmAdapter
    from flux_adapter import FluxAdapter
    from mock_slurm_adapter import MockSlurmAdapter

    MockSlurmAdapter()
    RealSlurmAdapter()
    FluxAdapter(executor=object())

    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_the_mock_still_creates_what_a_job_needs(tmp_path, monkeypatch):
    """Lazily, not at construction: a submitted job gets its folder."""
    monkeypatch.setenv("EXAMLOPS_HPC_WORKDIR", str(tmp_path / "jobs"))
    from mock_slurm_adapter import MockSlurmAdapter

    adapter = MockSlurmAdapter()
    job_id = adapter.submit_job(script_path=None, training_data=None)

    assert (adapter.working_dir / job_id).is_dir()
    assert adapter.working_dir.is_relative_to(tmp_path)


def test_an_adapter_honours_an_explicit_directory(tmp_path):
    from mock_slurm_adapter import MockSlurmAdapter

    assert MockSlurmAdapter(working_dir=str(tmp_path / "here")).working_dir == tmp_path / "here"


# ── MLflow artifacts follow the instance-data root ───────────────────────────


def test_a_local_store_puts_artifacts_under_the_data_root(tmp_path, monkeypatch):
    """Otherwise MLflow's default is ./mlruns — relative to whatever directory the process ran in."""
    from examlops.embeddings.mlflow_registry import _artifact_location

    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))

    location = _artifact_location("sqlite:///tmp/mlflow.db")

    assert location is not None
    assert Path(location.removeprefix("file://")).is_relative_to(tmp_path)


def test_a_tracking_server_keeps_its_own_artifact_store(tmp_path, monkeypatch):
    """The server owns it — MinIO in the platform's deployments — and a client must not override."""
    from examlops.embeddings.mlflow_registry import _artifact_location

    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))

    assert _artifact_location("http://mlflow:5000") is None
    assert _artifact_location("https://mlflow.example") is None


def test_without_a_data_root_mlflow_keeps_its_default(monkeypatch):
    """The instance-data layer's rule everywhere: unset means unchanged (ADR 0128)."""
    from examlops.embeddings.mlflow_registry import _artifact_location

    monkeypatch.delenv("EXAMLOPS_DATA_DIR", raising=False)

    assert _artifact_location("sqlite:///tmp/mlflow.db") is None


# ── the `exa` command that runs pytest leaves no cache ───────────────────────


def test_pipeline_validate_runs_pytest_without_a_cache(monkeypatch):
    from examlops.cli import _output
    from examlops.cli.commands import pipeline

    seen: list[list[str]] = []
    monkeypatch.setattr(_output, "run_external", lambda argv, **kw: seen.append(argv))

    pipeline._run_pytest(["tests/unit/test_registry_integrity.py"])

    (argv,) = seen
    assert argv[1:4] == ["-m", "pytest", "-p"] and argv[4] == "no:cacheprovider"


# ── the guard itself ─────────────────────────────────────────────────────────


def test_the_guard_blames_a_test_for_what_it_created():
    from tests.conftest import _test_authored

    before = {"platform.db", "README.md"}
    new = {"mlruns", "slurm_jobs"}

    assert _test_authored(new, before) == {"mlruns", "slurm_jobs"}


def test_the_guard_ignores_sidecars_of_files_that_were_already_there():
    """On a dev host the live stack writes to the checkout's own platform.db while the suite runs;
    blaming the test that happened to be running would make the guard fail at random."""
    from tests.conftest import _test_authored

    before = {"platform.db"}

    assert _test_authored({"platform.db-wal", "platform.db-shm"}, before) == set()
    assert _test_authored({"new.db-wal"}, before) == {"new.db-wal"}, "a new store is the test's"


def test_the_suite_sets_a_job_directory_outside_the_checkout():
    """Read from conftest, because this file's own fixture clears the variable per test."""
    from tests.conftest import _JOB_WORKDIR

    workdir = Path(_JOB_WORKDIR)

    assert workdir.is_absolute() and not workdir.is_relative_to(REPO_ROOT)


def test_the_guard_fails_a_test_that_writes_into_the_checkout(tmp_path):
    """Checked against this test's own directory. Repointing the fixture's root instead would make
    *this* test the one writing into the checkout — and under `-n auto` every test running beside
    it would then fail too. Proving a guard must not make the suite flaky."""
    from tests.conftest import assert_no_trace

    before = set()
    (tmp_path / "mlruns").mkdir()

    with pytest.raises(AssertionError, match=r"left \['mlruns'\] in the checkout"):
        assert_no_trace(before, tmp_path)


def test_the_guard_passes_a_test_that_leaves_nothing(tmp_path):
    from tests.conftest import assert_no_trace

    (tmp_path / "already-there").mkdir()
    before = {"already-there"}

    assert assert_no_trace(before, tmp_path) is None
