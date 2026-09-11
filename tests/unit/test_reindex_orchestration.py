# tests/unit/test_reindex_orchestration.py
"""ADR 0043 clause 4 — reindex as a scheduler job, with progress, invoked by B5's hook.

The recorded finding: "clause 4's scheduler orchestration is absent — reindex runs inline in the
calling process, invoked only by the CLI, with no progress or cost tracking and no B5 hook."

Three separate gaps, and the B5 hook is the interesting one: the vector store detected a
cross-encoder mismatch and raised, which is right, but the mismatch led nowhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import embeddings  # noqa: E402
from examlops.embeddings import (  # noqa: E402
    _reindex_mode,
    recommend_reindex,
    register_encoder,
    reindex,
)
from examlops.platform_db import list_reindex_jobs  # noqa: E402


class _Adapter:
    """A queuing scheduler that records the submission and never runs it (Slurm-like: the job
    would run later, elsewhere). It refuses a job with no script, as the real adapters do — the
    earlier stand-in accepted one, which is how a submission that failed on every real
    scheduler passed here. `test_reindex_scheduler_job.py` drives the real adapters."""

    def __init__(self):
        import tempfile

        self.working_dir = Path(tempfile.mkdtemp())
        self.submitted: list[dict] = []

    def submit_job(self, script_path=None, resources=None, training_data=None, remote_dir=None):
        assert script_path, "a real adapter refuses a job with no script"
        self.submitted.append({"resources": resources, "script": Path(script_path).read_text()})
        return "reindex-job-7"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("EXAMLOPS_REINDEX_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "slurm")  # `_Adapter` queues, like Slurm
    monkeypatch.setenv("EXAMLOPS_JOB_SCRIPT_DIR", str(tmp_path / "jobs"))


@pytest.fixture
def encoder():
    return register_encoder("e5", "2", 384)


# ── mode selection ────────────────────────────────────────────────────────────


def test_inline_is_the_default():
    """A reindex that silently became a cluster submission would strand every caller."""
    assert _reindex_mode() == "inline"


def test_the_environment_selects_the_scheduler(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_REINDEX_ORCHESTRATOR", "scheduler")
    assert _reindex_mode() == "scheduler"


def test_an_explicit_mode_beats_the_environment(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_REINDEX_ORCHESTRATOR", "scheduler")
    assert _reindex_mode("inline") == "inline"


def test_an_unrecognised_mode_is_inline(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_REINDEX_ORCHESTRATOR", "schedular")
    assert _reindex_mode() == "inline"


# ── the scheduler path ────────────────────────────────────────────────────────


def test_a_scheduler_reindex_submits_and_returns_the_job_id(monkeypatch, encoder):
    adapter = _Adapter()
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: adapter)

    result = reindex("corpusA", encoder, orchestrator="scheduler")

    assert result.switched is False
    assert "reindex-job-7" in result.reason
    assert adapter.submitted[0]["resources"]["job_name"] == "reindex-corpusA"


def test_the_job_continues_the_same_row_and_cannot_resubmit(monkeypatch, encoder):
    """The job used to re-enter `exa embedding reindex --inline`: a second row, the first left
    `submitted` forever. It now runs `examlops.embeddings.job` on this row — which never submits
    anything — and the collection and encoder names never reach the shell."""
    adapter = _Adapter()
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: adapter)

    reindex("corpusB", encoder, orchestrator="scheduler", recall=0.97, recall_floor=0.95)

    script = adapter.submitted[0]["script"]
    row = list_reindex_jobs("corpusB")[0]
    assert f"-m examlops.embeddings.job --job-id {row['id']}" in script
    assert "--recall 0.97" in script and "--recall-floor 0.95" in script
    exec_line = next(line for line in script.splitlines() if line.startswith("exec "))
    assert "corpusB" not in exec_line and encoder not in exec_line
    assert "examlops.cli" not in exec_line and " reindex " not in exec_line
    assert len(list_reindex_jobs("corpusB")) == 1


def test_a_submitted_reindex_records_no_recall(monkeypatch, encoder):
    """Nothing has been measured yet, and 0.0 would read as 'verified and terrible'."""
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: _Adapter())

    assert reindex("corpusC", encoder, orchestrator="scheduler").recall is None


def test_the_job_row_records_where_it_ran(monkeypatch, encoder):
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: _Adapter())

    reindex("corpusD", encoder, orchestrator="scheduler")

    job = list_reindex_jobs("corpusD")[0]
    assert job["orchestrator"] == "scheduler"
    assert job["hpc_job_id"] == "reindex-job-7"
    assert job["status"] == "submitted"


def test_no_scheduler_falls_back_to_running_here(monkeypatch, encoder):
    """An unreachable scheduler is an environment fact; not reindexing at all is worse."""
    monkeypatch.setattr(
        embeddings, "_scheduler_adapter", lambda: (_ for _ in ()).throw(RuntimeError("no sbatch"))
    )

    result = reindex("corpusE", encoder, orchestrator="scheduler", recall=1.0)

    assert result.switched is True
    assert list_reindex_jobs("corpusE")[0]["orchestrator"] == "inline-fallback"


def test_a_recall_function_cannot_travel_so_it_runs_here(monkeypatch, encoder):
    """Only a value reaches a job; a callable lives in this process (the asset-closure rule)."""
    adapter = _Adapter()
    monkeypatch.setattr(embeddings, "_scheduler_adapter", lambda: adapter)

    result = reindex("corpusE2", encoder, orchestrator="scheduler", recall_fn=lambda: 0.99)

    assert result.switched is True and not adapter.submitted
    assert list_reindex_jobs("corpusE2")[0]["orchestrator"] == "inline-fallback"


# ── progress ──────────────────────────────────────────────────────────────────


def test_a_completed_reindex_records_its_duration(encoder):
    reindex("corpusF", encoder, corpus_size=10, recall_fn=lambda: 1.0)

    job = list_reindex_jobs("corpusF")[0]

    assert job["duration_s"] is not None and job["duration_s"] >= 0
    assert job["docs_reindexed"] == 10


def test_an_aborted_reindex_records_its_duration_too(encoder):
    """Time was spent whether or not the switch happened."""
    reindex("corpusG", encoder, recall_fn=lambda: 0.1, recall_floor=0.9)

    job = list_reindex_jobs("corpusG")[0]

    assert job["status"] == "aborted"
    assert job["duration_s"] is not None


def test_an_inline_reindex_is_recorded_as_inline(encoder):
    reindex("corpusH", encoder, recall_fn=lambda: 1.0)
    assert list_reindex_jobs("corpusH")[0]["orchestrator"] == "inline"


# ── B5's hook ─────────────────────────────────────────────────────────────────


def test_a_recommendation_is_recorded():
    recommend_reindex("corpusI", "default", from_encoder="old", to_encoder="new")

    jobs = [j for j in list_reindex_jobs("corpusI") if j["status"] == "recommended"]

    assert len(jobs) == 1
    assert jobs[0]["to_encoder"] == "new"


def test_recommendations_do_not_pile_up():
    """A mismatched collection is queried many times; one row per query buries the signal."""
    for _ in range(5):
        recommend_reindex("corpusJ", "default", from_encoder="old", to_encoder="new")

    assert len([j for j in list_reindex_jobs("corpusJ") if j["status"] == "recommended"]) == 1


def test_a_different_target_encoder_is_a_different_recommendation():
    recommend_reindex("corpusK", "default", from_encoder="old", to_encoder="new1")
    recommend_reindex("corpusK", "default", from_encoder="old", to_encoder="new2")

    assert len([j for j in list_reindex_jobs("corpusK") if j["status"] == "recommended"]) == 2


def test_the_recommendation_is_audited():
    from examlops.data.audit import export_audit_events

    recommend_reindex("corpusL", "default", from_encoder="old", to_encoder="new")

    assert [e for e in export_audit_events() if e["action"] == "reindex_recommended"]


def test_a_failing_recommendation_never_breaks_the_caller(monkeypatch):
    """Swallowed, and nothing half-written — the caller was doing a search, not a reindex."""
    from examlops.data.audit import export_audit_events

    monkeypatch.setattr(
        embeddings.platform_db,
        "list_reindex_jobs",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    recommend_reindex("corpusM", "default", from_encoder="old", to_encoder="new")

    assert not [
        e
        for e in export_audit_events()
        if e["action"] == "reindex_recommended" and e["target"] == "corpusM"
    ]


def test_the_vector_store_records_a_recommendation_on_a_mismatch():
    """The hook: the store already refused, and the refusal led nowhere."""
    from examlops.vector_store import EncoderMismatch, SqliteVectorStore

    store = SqliteVectorStore()
    store.create_collection("corpusN", dim=3, metric="cosine", encoder_id="enc-old")

    with pytest.raises(EncoderMismatch) as exc:
        store.search("corpusN", [0.1, 0.2, 0.3], k=1, encoder_id="enc-new")

    assert "exa embedding reindex corpusN enc-new" in str(exc.value)
    assert [j for j in list_reindex_jobs("corpusN") if j["status"] == "recommended"]


def test_the_store_still_refuses_rather_than_reindexing():
    """A search that quietly re-embedded a large corpus would turn one query into a job."""
    from examlops.vector_store import EncoderMismatch, SqliteVectorStore

    store = SqliteVectorStore()
    store.create_collection("corpusO", dim=3, metric="cosine", encoder_id="enc-old")

    with pytest.raises(EncoderMismatch):
        store.search("corpusO", [0.1, 0.2, 0.3], k=1, encoder_id="enc-new")

    switched = [j for j in list_reindex_jobs("corpusO") if j["status"] == "switched"]
    assert switched == [], "no reindex may have run"
