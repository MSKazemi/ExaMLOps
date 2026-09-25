# tests/unit/test_finetune_scheduler_mlflow.py
"""ADR 0044 clauses 1-2: a fine-tune submitted to the scheduler, and the adapter in MLflow.

The scheduler tests run the **real** phase-23 mock adapter: it executes the generated ``run.sh``,
which runs the shipped training script, so an adapter registered here was trained inside a
scheduler job — its row carries that job id and ``hpc_jobs`` lists it. The MLflow tests use a real
MLflow client against a throwaway SQLite tracking store, so a "logged" result means a run, its
metrics and the bundle files exist in MLflow, and a full fine-tune is a registered model version.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("torch")

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.finetuning import artifacts, train_lora  # noqa: E402
from examlops.finetuning.runner import run_finetune  # noqa: E402
from examlops.finetuning.scheduler import scheduler_runner  # noqa: E402

SEED = 13


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "mock")
    monkeypatch.setenv("EXAMLOPS_HPC_WORKDIR", str(tmp_path / "hpc"))
    monkeypatch.setenv("EXAMLOPS_JOB_SCRIPT_DIR", str(tmp_path / "jobs"))
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("EXAMLOPS_FINETUNE_FAULT", raising=False)
    from examlops import platform_db

    platform_db.init_db()


def _actions(target: str) -> list[str]:
    from examlops.platform_db import get_db

    with get_db() as conn:
        return [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE target=? ORDER BY id", (target,)
            ).fetchall()
        ]


# ── scheduler (clause 1, E6 path) ─────────────────────────────────────────────


def test_a_fine_tune_runs_as_a_scheduler_job_and_is_registered_from_it(tmp_path):
    from examlops.data.hpc import get_hpc_jobs
    from examlops.platform_db import get_adapter

    res = run_finetune(
        "demo-base",
        dataset_rev="rev-s",
        adapter_id="sched",
        run_id="ft-sched",
        run_dir=tmp_path / "run",
        steps=30,
        checkpoint_every=15,
        batch=32,
        seed=SEED,
        scheduler=True,
        resources={"gpus": 1, "partition": "gpu"},
        actor="tester",
    )

    assert res.status == "complete", (res.attempts, Path(res.log).read_text()[-2000:])
    assert res.scheduler == "mock" and len(res.hpc_job_ids) == 1
    row = get_adapter("sched")
    assert row["eval_source"] == "measured" and row["hpc_job_id"] == res.hpc_job_ids[0]
    assert row["adapter_uri"] == res.metrics["adapter_bundle"]
    jobs = [j for j in get_hpc_jobs() if j["job_id"] == res.hpc_job_ids[0]]
    assert jobs and jobs[0]["model"] == "finetune:ft-sched" and jobs[0]["state"] == "COMPLETED"
    assert "finetune_job_submitted" in _actions("ft-sched")
    script = next((tmp_path / "jobs").glob("finetune-*/run.sh"))
    assert "examlops.finetuning.train_lora" in script.read_text()
    assert "EXAMLOPS_SIGNING_KEY" not in script.read_text(), "no environment value in the script"


def test_a_fatal_job_is_not_resubmitted_and_registers_nothing(tmp_path):
    from examlops.platform_db import get_adapter

    res = run_finetune(
        "demo-base",
        dataset_rev="rev-s",
        adapter_id="sched-bad",
        run_dir=tmp_path / "bad",
        steps=10,
        backend="made-up",
        seed=SEED,
        max_attempts=3,
        scheduler=True,
    )

    assert res.status == "fatal" and len(res.attempts) == 1, res.attempts
    assert get_adapter("sched-bad") is None


class _RefusingAdapter:
    working_dir = Path("/nonexistent")

    def submit_job(self, **kw):
        raise RuntimeError("sbatch: invalid partition")


class _HangingAdapter:
    def __init__(self, tmp: Path):
        self.working_dir = tmp
        self.cancelled: list[str] = []

    def submit_job(self, **kw):
        return "4242"

    def wait_until_complete(self, job_id, max_wait_s=None):
        raise TimeoutError(f"not terminal within {max_wait_s}s")

    def get_job_status(self, job_id):  # pragma: no cover - not reached
        return {}

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)

    def get_job_logs(self, job_id):
        return "partial output"


def test_a_refused_submission_fails_the_attempt_without_a_registration(tmp_path):
    from examlops.platform_db import get_adapter

    res = run_finetune(
        "demo-base",
        dataset_rev="r",
        adapter_id="refused",
        run_dir=tmp_path / "r",
        max_attempts=2,
        scheduler=True,
        scheduler_adapter=_RefusingAdapter(),
    )
    assert res.status == "failed" and [a.returncode for a in res.attempts] == [1, 1]
    assert "invalid partition" in Path(res.log).read_text()
    assert get_adapter("refused") is None
    assert _actions(res.run_id).count("finetune_job_refused") == 2


def test_a_job_that_never_finishes_is_cancelled_and_bounded(tmp_path):
    adapter = _HangingAdapter(tmp_path)
    run = scheduler_runner({"time": "00:10:00"}, run_id="hang", adapter=adapter)
    log = tmp_path / "attempt.log"

    rc = run(["python", "-m", "examlops.finetuning.train_lora"], {}, log, 5.0)

    assert rc == 1 and adapter.cancelled == ["4242"]
    assert "UNKNOWN" in log.read_text() and "partial output" in log.read_text()


@pytest.mark.parametrize(
    "resources", [{"nodelist": "n1"}, {"partition": "gpu --wrap=evil"}, {"time": ""}]
)
def test_scheduler_resources_are_an_allow_list_of_single_tokens(resources):
    with pytest.raises(ValueError):
        scheduler_runner(resources, run_id="x", adapter=_RefusingAdapter())


# ── MLflow (clause 2, clause 4) ───────────────────────────────────────────────


def _train_local(tmp_path, capsys, *extra) -> dict:
    rc = train_lora.main(
        ["--run-dir", str(tmp_path / "local"), "--steps", "20", "--batch", "32", "--seed", "3"]
        + list(extra)
    )
    assert rc == 0
    return train_lora.parse_metrics(capsys.readouterr().out)


def test_without_a_tracking_uri_mlflow_is_skipped_not_failed(tmp_path, capsys):
    m = _train_local(tmp_path, capsys)
    rec = artifacts.log_to_mlflow(
        "a", base="b", method="lora", dataset_revision="r", metrics=m,
        bundle=m["adapter_bundle"], train_run_id="t",
    )  # fmt: skip
    assert rec.status == "skipped" and "MLFLOW_TRACKING_URI" in rec.reason


def test_an_unreachable_tracker_is_reported_not_raised(tmp_path, capsys):
    m = _train_local(tmp_path, capsys)

    class _Down:
        def set_tracking_uri(self, uri):
            pass

        def set_experiment(self, name):
            raise ConnectionError("tracker down")

    rec = artifacts.log_to_mlflow(
        "a", base="b", method="lora", dataset_revision="r", metrics=m,
        bundle=m["adapter_bundle"], train_run_id="t", tracking_uri="http://x", mlflow_module=_Down(),
    )  # fmt: skip
    assert rec.status == "failed" and "tracker down" in rec.reason


def test_the_timeout_env_is_bounded_and_restored(monkeypatch):
    monkeypatch.delenv("MLFLOW_HTTP_REQUEST_TIMEOUT", raising=False)
    import os

    with artifacts._bounded_http():
        assert os.environ["MLFLOW_HTTP_REQUEST_TIMEOUT"] == "30"
    assert "MLFLOW_HTTP_REQUEST_TIMEOUT" not in os.environ


@pytest.fixture
def tracking(tmp_path, monkeypatch):
    pytest.importorskip("mlflow")
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    monkeypatch.setenv("MLFLOW_ARTIFACT_ROOT", str(tmp_path / "mlartifacts"))
    monkeypatch.chdir(tmp_path)  # the default artifact root is relative to the cwd
    return uri


def test_a_trained_adapter_is_a_first_class_mlflow_artifact(tmp_path, tracking):
    import mlflow

    from examlops.platform_db import get_adapter

    res = run_finetune(
        "demo-base",
        dataset_rev="rev-m",
        adapter_id="ml",
        run_id="ft-ml",
        run_dir=tmp_path / "ml",
        steps=20,
        batch=32,
        seed=SEED,
    )

    assert res.status == "complete" and res.mlflow["status"] == "logged", res.mlflow
    row = get_adapter("ml")
    assert row["mlflow_run_id"] == res.mlflow["run_id"]
    client = mlflow.MlflowClient(tracking_uri=tracking)
    run = client.get_run(row["mlflow_run_id"])
    assert run.data.tags["examlops.eval_source"] == "measured"
    assert run.data.tags["examlops.adapter_sha256"] == row["adapter_sha256"]
    assert run.data.metrics["eval_score"] == pytest.approx(row["eval_score"])
    files = {f.path for f in client.list_artifacts(run.info.run_id, "adapter")}
    assert files == {"adapter/adapter_config.json", "adapter/adapter_model.pt"}
    assert res.mlflow["model_version"] is None, "an adapter is an artifact, not a model version"
    assert "adapter_mlflow_logged" in _actions("ml")


def test_a_full_fine_tune_is_registered_as_a_model_version(tmp_path, tracking):
    import mlflow

    res = run_finetune(
        "demo-base",
        dataset_rev="rev-f",
        adapter_id="full-ft",
        run_dir=tmp_path / "full",
        method="full",
        steps=20,
        batch=32,
        seed=SEED,
    )

    assert res.status == "complete" and res.mlflow["model_version"] == "1", res.mlflow
    versions = mlflow.MlflowClient(tracking_uri=tracking).search_model_versions("name='full-ft'")
    assert [v.run_id for v in versions] == [res.mlflow["run_id"]]


def test_asking_for_mlflow_without_a_tracker_is_a_failure_not_a_skip(tmp_path):
    res = run_finetune(
        "demo-base", dataset_rev="r", adapter_id="want-ml", run_dir=tmp_path / "w",
        steps=10, batch=16, seed=SEED, mlflow=True,
    )  # fmt: skip
    assert res.status == "complete" and res.mlflow["status"] == "failed"
    assert "adapter_mlflow_failed" in _actions("want-ml")


def test_no_mlflow_is_honoured(tmp_path, tracking):
    from examlops.platform_db import get_adapter

    res = run_finetune(
        "demo-base", dataset_rev="r", adapter_id="no-ml", run_dir=tmp_path / "n",
        steps=10, batch=16, seed=SEED, mlflow=False,
    )  # fmt: skip
    assert res.mlflow["status"] == "skipped" and get_adapter("no-ml")["mlflow_run_id"] is None


def test_the_cli_trains_a_full_fine_tune_on_the_scheduler(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path / "data"))
    result = CliRunner().invoke(
        app,
        ["--json", "finetune", "demo-base", "--train", "--method", "full", "--dataset", "r",
         "--adapter-id", "cli-full", "--steps", "20", "--batch", "32", "--seed", "3",
         "--scheduler", "--gpus", "1", "--no-mlflow"],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["trained"] is True and payload["method"] == "full"
    assert payload["executor"] == "mock" and len(payload["hpc_job_ids"]) == 1
    assert payload["mlflow"]["status"] == "skipped"


def test_the_cli_rejects_an_unknown_scheduler_resource_value(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    result = CliRunner().invoke(
        app,
        ["finetune", "demo-base", "--train", "--dataset", "r", "--scheduler",
         "--partition", "gpu --wrap=x"],
    )  # fmt: skip
    assert result.exit_code == 2, result.output


# ── a lost audit is counted, never silent ─────────────────────────────────────


def _audit_down(monkeypatch):
    from examlops.data import audit as audit_mod
    from examlops.data.audit import reset_dropped_audit_events

    def boom(*a, **k):
        raise RuntimeError("audit down")

    reset_dropped_audit_events()
    monkeypatch.setattr(audit_mod, "write_audit_event", boom)


def test_a_lost_scheduler_submission_audit_is_counted_and_the_job_still_runs(tmp_path, monkeypatch):
    """The job id in the audit log is how a GPU-hour is traced back to a run; losing it is counted."""
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events
    from examlops.platform_db import get_adapter

    _audit_down(monkeypatch)
    res = run_finetune(
        "demo-base", dataset_rev="r", adapter_id="lost-sched", run_dir=tmp_path / "ls",
        steps=10, batch=16, seed=SEED, scheduler=True, mlflow=True,
    )  # fmt: skip

    assert res.status == "complete" and get_adapter("lost-sched")["hpc_job_id"]
    dropped = dropped_audit_events()
    assert dropped.get("finetune_job_submitted") == 1, dropped
    assert dropped.get("adapter_mlflow_failed") == 1, dropped  # no tracker, and --mlflow asked
    reset_dropped_audit_events()


def test_a_lost_refusal_audit_is_counted(tmp_path, monkeypatch):
    """A refused submission leaves no job and no adapter: the audit event is its only trace."""
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    _audit_down(monkeypatch)
    run = scheduler_runner({}, run_id="lost-refusal", adapter=_RefusingAdapter())
    assert run(["python"], {}, tmp_path / "a.log", 5.0) == 1
    assert dropped_audit_events().get("finetune_job_refused") == 1
    reset_dropped_audit_events()
