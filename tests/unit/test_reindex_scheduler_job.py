# tests/unit/test_reindex_scheduler_job.py
"""ADR 0043 clause 4 — a reindex "runs as a scheduler job" has to run, once, with its gate.

Found 2026-09-11 while fixing the same defect in the asset scheduler path (ADR 0036 clause 3):
`submit_reindex` called `submit_job(script_path=None, …)` with the command in `training_data`.

* Slurm and Flux refused it (`script_path is required`), so `exa embedding reindex --scheduler`
  crashed on every real scheduler; the mock accepted it and never ran it.
* The command re-entered `exa embedding reindex` **without** `--recall` / `--recall-floor`, so
  where it could run it verified against the defaults and switched regardless — the recall gate
  the operator set was not applied.
* It opened a second `reindex_jobs` row, leaving the first `submitted` forever.

These drive the real adapters; Slurm and Flux through an executor that runs the submitted script.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT / "platform" / "cli" / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from examlops import embeddings  # noqa: E402
from examlops.embeddings import (  # noqa: E402
    ReindexSubmissionError,
    register_encoder,
    reindex,
    set_collection_encoder,
)
from examlops.platform_db import get_collection, list_reindex_jobs  # noqa: E402
from tests.unit._scheduler_fakes import ScriptRunningExecutor  # noqa: E402

_SRC = str(ROOT / "platform" / "cli" / "src")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for var in ("EXAMLOPS_REINDEX_ORCHESTRATOR", "EXAMLOPS_HPC_REMOTE_PYTHON"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EXAMLOPS_JOB_SCRIPT_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("EXAMLOPS_HPC_REMOTE_WORKDIR", str(tmp_path / "remote"))


@pytest.fixture
def encoders():
    old = register_encoder("e5", "1", 384)
    new = register_encoder("e5", "2", 384)
    return old, new


def _mock(monkeypatch, tmp_path):
    from mock_slurm_adapter import MockSlurmAdapter

    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "mock")
    monkeypatch.setattr(
        embeddings, "_scheduler_adapter", lambda: MockSlurmAdapter(working_dir=tmp_path / "mock")
    )


# ── it runs, and the operator's gate is applied where it runs ────────────────


def test_a_mock_scheduler_reindex_runs_and_switches(monkeypatch, tmp_path, encoders):
    old, new = encoders
    set_collection_encoder("docsA", old)
    _mock(monkeypatch, tmp_path)

    result = reindex("docsA", new, orchestrator="scheduler", recall=0.97, corpus_size=12)

    assert result.switched is True and result.recall == 0.97
    rows = list_reindex_jobs("docsA")
    assert len(rows) == 1, "one reindex, one row"
    assert rows[0]["status"] == "switched" and rows[0]["orchestrator"] == "scheduler"
    assert rows[0]["hpc_job_id"] and rows[0]["docs_reindexed"] == 12
    assert get_collection("docsA", "default")["active_encoder_id"] == new


def test_the_recall_gate_is_applied_in_the_job(monkeypatch, tmp_path, encoders):
    """The regression: the job ran with the default recall and switched anyway."""
    old, new = encoders
    set_collection_encoder("docsB", old)
    _mock(monkeypatch, tmp_path)

    result = reindex("docsB", new, orchestrator="scheduler", recall=0.5, recall_floor=0.9)

    assert result.switched is False
    assert list_reindex_jobs("docsB")[0]["status"] == "aborted"
    assert get_collection("docsB", "default")["active_encoder_id"] == old, "old index kept"


@pytest.mark.parametrize("scheduler", ["slurm", "flux"])
def test_real_adapters_accept_the_job(monkeypatch, tmp_path, encoders, scheduler):
    """They used to raise `script_path is required`. This executor runs the script at submit
    time — earlier than a real cluster ever could — which also proves the row is `submitted`
    before the job exists."""
    old, new = encoders
    set_collection_encoder("docsC", old)
    executor = ScriptRunningExecutor("4242" if scheduler == "slurm" else "fAbC")
    if scheduler == "slurm":
        from adapter import RealSlurmAdapter

        adapter = RealSlurmAdapter(executor=executor, working_dir=str(tmp_path / "wd"))
    else:
        from flux_adapter import FluxAdapter

        adapter = FluxAdapter(
            executor=executor, working_dir=str(tmp_path / "wd"), remote_workdir=str(tmp_path / "r")
        )
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", scheduler)
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: adapter)

    result = reindex("docsC", new, orchestrator="scheduler", recall=0.99)

    assert executor.returncode == 0
    row = list_reindex_jobs("docsC")[0]
    assert row["hpc_job_id"] == executor.job_id and row["status"] == "switched"
    assert result.switched is True


def test_a_refused_job_marks_the_row_failed(monkeypatch, tmp_path, encoders):
    class Refusing:
        working_dir = tmp_path / "wd"

        def submit_job(self, **kw):
            raise RuntimeError("sbatch: error: invalid partition")

    old, new = encoders
    set_collection_encoder("docsD", old)
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "slurm")
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: Refusing())

    with pytest.raises(ReindexSubmissionError, match="refused the job.*invalid partition"):
        reindex("docsD", new, orchestrator="scheduler", recall=1.0)

    assert list_reindex_jobs("docsD")[0]["status"] == "failed"
    assert get_collection("docsD", "default")["active_encoder_id"] == old


def test_a_job_that_never_reaches_the_reindex_fails_the_row(monkeypatch, tmp_path, encoders):
    """A job that cannot start its interpreter must not leave the row `submitted` forever."""
    old, new = encoders
    set_collection_encoder("docsE", old)
    _mock(monkeypatch, tmp_path)
    monkeypatch.setenv("EXAMLOPS_HPC_REMOTE_PYTHON", str(tmp_path / "no-such-python"))

    result = reindex("docsE", new, orchestrator="scheduler", recall=1.0)

    assert result.switched is False
    assert list_reindex_jobs("docsE")[0]["status"] == "failed"


# ── the job runner ───────────────────────────────────────────────────────────


def _job(*args):
    return subprocess.run(
        [sys.executable, "-m", "examlops.embeddings.job", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join([_SRC, str(ROOT)])},
        cwd=str(ROOT),
    )


def test_the_job_runs_only_a_submitted_row(encoders):
    """A replayed job must not switch an index a second time."""
    old, new = encoders
    set_collection_encoder("docsF", old)
    reindex("docsF", new, recall=1.0)  # inline: the row is already `switched`
    row = list_reindex_jobs("docsF")[0]

    r = _job("--job-id", str(row["id"]))

    assert r.returncode == 4, r.stderr
    assert list_reindex_jobs("docsF")[0]["status"] == "switched"


def test_a_reindex_that_raises_in_the_job_fails_its_row():
    from examlops.platform_db import create_reindex_job, update_reindex_job

    job_id = create_reindex_job("docsG", "default", None, "enc-never-registered")
    update_reindex_job(job_id, status="submitted")

    r = _job("--job-id", str(job_id))

    assert r.returncode == 1 and "unknown encoder" in r.stderr
    assert list_reindex_jobs("docsG")[0]["status"] == "failed"


@pytest.mark.parametrize("args,code", [(["--job-id", "999999"], 3), (["--job-id", "x"], 2)])
def test_the_job_exit_code_says_what_went_wrong(args, code):
    assert _job(*args).returncode == code
