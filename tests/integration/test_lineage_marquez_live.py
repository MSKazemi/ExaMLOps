"""ADR 0004 clause 3 against a REAL Marquez — the `lineage` Compose profile.

The unit suite validates every event against the published OpenLineage schema
(`tests/unit/test_lineage_openlineage_conformance.py`). This one sends them to the receiver the
profile runs and reads back what it stored, because a 201 from the receiver is not the same as the
graph being right: before BL-068 every event was accepted, and Marquez silently replaced each run
id and drew one dataset per revision.

Opt-in: skipped unless ``EXAMLOPS_LINEAGE_LIVE_MARQUEZ_URL`` is set, because it needs the profile
running:

    docker compose -f platform/infra/docker-compose/docker-compose.yml --profile lineage up -d marquez-web
    EXAMLOPS_LINEAGE_LIVE_MARQUEZ_URL=http://localhost:15050 \\
        .venv/bin/pytest tests/integration/test_lineage_marquez_live.py -v
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "cli" / "src"))

URL = os.getenv("EXAMLOPS_LINEAGE_LIVE_MARQUEZ_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not URL, reason="needs `--profile lineage` running (see module)")


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{URL}/api/v1{path}", timeout=10) as resp:
        return json.load(resp)


def _q(name: str) -> str:
    return urllib.parse.quote(name, safe="")


@pytest.fixture
def emit(monkeypatch, tmp_path):
    from examlops import lineage
    from examlops.platform_db import init_db

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_OPENLINEAGE_URL", URL)
    init_db()
    return lineage


def test_a_training_run_lands_under_its_mlflow_run_id(emit):
    job = f"train:live-{uuid.uuid4().hex[:8]}"
    mlflow_run = uuid.uuid4().hex  # an MLflow run id: 32 hex digits
    emit.emit_lineage(
        "COMPLETE",
        job,
        mlflow_run,
        inputs=[emit.dataset_node("LiveData", "rev-1")],
        outputs=[emit.model_node("live", 1)],
        facets={"backend": "minio", **emit.hpc_job_id_facet("77", "flux")},
    )

    (run,) = _get(f"/namespaces/examlops/jobs/{_q(job)}/runs")["runs"]
    assert run["id"].replace("-", "") == mlflow_run
    assert run["state"] == "COMPLETED"
    assert run["facets"]["examlops.run"]["run_id"] == mlflow_run
    assert run["facets"]["examlops.run"]["backend"] == "minio"
    assert run["facets"]["examlops.hpc_job"]["job_id"] == "77"


def test_start_and_complete_of_a_non_uuid_run_are_one_run(emit):
    # Unique per run: a receiver keeps a run id, with the job it first saw, forever.
    suffix = uuid.uuid4().hex[:8]
    job, run = f"retrain:live-{suffix}", f"retrain-live-{suffix}"
    emit.emit_lineage("START", job, run, outputs=[emit.model_node("live", "pending")])
    emit.emit_lineage("COMPLETE", job, run, outputs=[emit.model_node("live", 2)])

    runs = _get(f"/namespaces/examlops/jobs/{_q(job)}/runs")["runs"]
    assert [(r["id"], r["state"]) for r in runs] == [(emit.lineage_run_id(run), "COMPLETED")]


def test_revisions_are_versions_of_one_dataset(emit):
    dataset = f"LiveData-{uuid.uuid4().hex[:8]}"
    for revision in ("rev-a", "rev-b"):
        emit.emit_lineage(
            "COMPLETE",
            f"train:{dataset}",
            uuid.uuid4().hex,
            inputs=[emit.dataset_node(dataset, revision)],
            outputs=[emit.model_node(dataset.lower(), revision)],
        )

    names = {d["name"] for d in _get("/namespaces/examlops/datasets?limit=1000")["datasets"]}
    assert f"examlops://dataset/{dataset}" in names
    assert not {n for n in names if n.startswith(f"examlops://dataset/{dataset}@")}
    # Marquez versions a dataset when a job WRITES it; for one a job reads it keeps the latest
    # read's version facet. Each run's own revision is in its examlops.dataset_revision facet.
    latest = _get(f"/namespaces/examlops/datasets/{_q(f'examlops://dataset/{dataset}')}")
    assert latest["facets"]["version"]["datasetVersion"] == "rev-b"


def test_a_training_run_reads_running_then_completed(emit):
    """BL-069: START and COMPLETE share the flow run id, so the receiver closes the run."""
    job, flow_run = f"train:live-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())
    emit.emit_lineage("START", job, flow_run, inputs=[emit.dataset_node("LiveData")])
    assert _get(f"/namespaces/examlops/jobs/{_q(job)}/runs")["runs"][0]["state"] == "RUNNING"

    emit.emit_lineage("COMPLETE", job, flow_run, mlflow_run_id=uuid.uuid4().hex)

    (run,) = _get(f"/namespaces/examlops/jobs/{_q(job)}/runs")["runs"]
    assert (run["id"], run["state"]) == (flow_run, "COMPLETED")


def test_a_failed_run_reads_failed_with_its_reason(emit):
    job, flow_run = f"train:live-{uuid.uuid4().hex[:8]}", str(uuid.uuid4())
    emit.emit_lineage("START", job, flow_run)
    emit.emit_lineage("FAIL", job, flow_run, facets=emit.error_facet("violates its data contract"))

    (run,) = _get(f"/namespaces/examlops/jobs/{_q(job)}/runs")["runs"]
    assert run["state"] == "FAILED"
    assert run["facets"]["errorMessage"]["message"] == "violates its data contract"


def test_registration_then_cost_leave_the_training_run_completed(emit):
    """START → OTHER (what registration learned) → COMPLETE, then a cost recording. Marquez shows
    a run RUNNING again after a trailing OTHER, so the cost is a child run: Marquez nests it under
    the training job (`<parent>.<child>`) and the training run stays COMPLETED."""
    model = f"live-{uuid.uuid4().hex[:8]}"
    job, flow_run, mlflow_run = f"train:{model}", str(uuid.uuid4()), uuid.uuid4().hex
    emit.emit_lineage("START", job, flow_run, inputs=[emit.dataset_node(model)], model=model)
    emit.emit_lineage(
        "OTHER",
        job,
        flow_run,
        inputs=[emit.dataset_node(model, "r1")],
        outputs=[emit.model_node(model, 1)],
        mlflow_run_id=mlflow_run,
        model=model,
    )
    emit.emit_lineage("COMPLETE", job, flow_run, model=model)
    assert emit.attach_run_cost(mlflow_run, gpu_hours=1.5, cost_usd=3.0)

    (run,) = _get(f"/namespaces/examlops/jobs/{_q(job)}/runs")["runs"]
    assert (run["id"], run["state"]) == (flow_run, "COMPLETED")
    assert [o["name"] for o in _get(f"/namespaces/examlops/jobs/{_q(job)}")["outputs"]] == [
        f"examlops://model/{model}/1"
    ], "the OTHER event's outputs are the run's"
    (child,) = _get(f"/namespaces/examlops/jobs/{_q(f'{job}.cost:{model}')}/runs")["runs"]
    assert child["state"] == "COMPLETED"
    assert child["facets"]["parent"]["run"]["runId"] == flow_run
    assert child["facets"]["examlops.cost"]["cost_usd"] == 3.0
