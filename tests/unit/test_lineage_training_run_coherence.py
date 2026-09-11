# tests/unit/test_lineage_training_run_coherence.py
"""ADR 0004 clause 1 — a training run is one lineage run, from START to its end (BL-069).

Found by reading the graph back out of Marquez (BL-068). Clause 1 says the paths emit
"START/COMPLETE/FAIL run events", and in practice:

- **nothing emitted FAIL** — a training flow that died left no trace in the graph at all;
- **nothing emitted the training run's START**, and `exa retrain` opened a START on job
  `retrain:<MODEL>` under the Prefect flow run id, while the flow closed job `train:<model>` under
  the MLflow run id. Two jobs, two runs, and the retrain's START was never closed: in any lineage
  receiver every retrain stayed "running" forever.

Now the flow's state hooks send the run's one START and its one ending (COMPLETE / FAIL / ABORT) —
exactly what the OpenLineage 2-0-2 schema asks of a run — under the Prefect flow run id;
registration adds an OTHER to that run with what it learned (dataset → model); `exa models cost
--record` adds another with the cost; and the retrain request is its own completed run that links
to the training run it scheduled. These run real Prefect flow runs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_MZ = Path(os.environ.get("EXAMLOPS_MODELZOO_DIR") or ROOT / "modelzoo")
_NO_MODELZOO = not (_MZ / "seanergys_modelzoo").is_dir()

sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))
sys.path.insert(0, str(ROOT / "pipelines"))

from examlops import lineage  # noqa: E402
from examlops.data.events import lineage_graph  # noqa: E402
from examlops.platform_db import get_db, init_db  # noqa: E402

needs_pipelines = pytest.mark.skipif(
    _NO_MODELZOO, reason="pipeline_generator imports the use-case pack, which needs modelzoo"
)


def _events(job: str) -> list[dict]:
    init_db()
    with get_db() as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM lineage_events WHERE job=? ORDER BY id", (job,))
        ]


@pytest.fixture(scope="module")
def prefect_server():
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        yield


@pytest.fixture
def pg():
    import pipeline_generator

    return pipeline_generator


# ── the training flow is wired ───────────────────────────────────────────────


@needs_pipelines
def test_the_training_flow_emits_on_every_way_a_run_ends(pg):
    """Hooks, not a try/except: a crash or a cancellation never reaches an except clause."""
    names = {
        kind: [h.__name__ for h in getattr(pg.training_flow, f"on_{kind}_hooks")]
        for kind in ("running", "failure", "crashed", "cancellation")
    }

    names["completion"] = [h.__name__ for h in pg.training_flow.on_completion_hooks]

    assert names == {
        "running": ["_lineage_start"],
        "completion": ["_lineage_complete"],
        "failure": ["_lineage_fail"],
        "crashed": ["_lineage_fail"],
        "cancellation": ["_lineage_abort"],
    }


# ── a real flow run: one run id for START and its end ────────────────────────


@needs_pipelines
def test_start_and_complete_are_one_run_under_the_flow_run_id(pg, prefect_server):
    from prefect import flow, task

    @task
    def register_and_emit():
        pg._emit_training_lineage(
            "jpcp", "PM100Dataset", {"run_id": "mlf-coh-1", "version": "7"}, None, None, None
        )

    @flow(
        on_running=[pg._flow_state_lineage("START")],
        on_completion=[pg._flow_state_lineage("COMPLETE")],
        on_failure=[pg._flow_state_lineage("FAIL")],
    )
    def training_like(model_name: str, dataset_cls_name: str):
        register_and_emit()

    state = training_like("JPCP", "PM100Dataset", return_state=True)
    flow_run_id = str(state.state_details.flow_run_id)

    events = _events("train:jpcp")
    assert {e["run_id"] for e in events} == {flow_run_id}, "every event is on the flow run"
    assert [e["event_type"] for e in events] == ["START", "OTHER", "COMPLETE"]
    assert events[1]["mlflow_run_id"] == "mlf-coh-1", "the MLflow run travels as a facet"
    assert events[1]["model_version"] == "7"


@needs_pipelines
def test_a_run_that_registers_then_fails_has_one_ending(pg, prefect_server):
    """The spec allows one of COMPLETE/ABORT/FAIL per run. Registration is not the ending: a run
    that registered its version and then failed at promotion ended in FAIL, and says only that."""
    from prefect import flow, task

    @task
    def register_and_emit():
        pg._emit_training_lineage("mack", "FDataDataset", {"run_id": "mlf-coh-2"}, None, None, None)

    @flow(
        on_running=[pg._flow_state_lineage("START")],
        on_completion=[pg._flow_state_lineage("COMPLETE")],
        on_failure=[pg._flow_state_lineage("FAIL")],
    )
    def training_like(model_name: str, dataset_cls_name: str):
        register_and_emit()
        raise RuntimeError("promotion refused")

    training_like("MACK", "FDataDataset", return_state=True)

    assert [e["event_type"] for e in _events("train:mack")] == ["START", "OTHER", "FAIL"]


@needs_pipelines
def test_a_failed_run_emits_fail_with_its_reason(pg, prefect_server):
    from prefect import flow

    @flow(on_running=[pg._flow_state_lineage("START")], on_failure=[pg._flow_state_lineage("FAIL")])
    def training_like(model_name: str, dataset_cls_name: str):
        raise RuntimeError("FData violates its data contract (score 0.4)")

    state = training_like("MCBound", "FDataDataset", return_state=True)
    flow_run_id = str(state.state_details.flow_run_id)

    events = [e for e in _events("train:mcbound") if e["run_id"] == flow_run_id]
    assert [e["event_type"] for e in events] == ["START", "FAIL"]
    error = json.loads(events[1]["facets_json"])["errorMessage"]
    assert "violates its data contract" in error["message"]
    assert error["programmingLanguage"] == "python"


@needs_pipelines
def test_a_hook_never_fails_the_flow(pg, prefect_server, monkeypatch, capsys):
    """A lineage outage must not turn into a flow-state change."""
    from prefect import flow

    monkeypatch.setattr(
        lineage, "emit_lineage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )

    @flow(on_running=[pg._flow_state_lineage("START")])
    def training_like(model_name: str, dataset_cls_name: str):
        return "trained"

    assert training_like("JPCP", "PM100Dataset") == "trained"
    assert "lineage START skipped" in capsys.readouterr().out


@needs_pipelines
def test_outside_a_flow_run_the_mlflow_run_id_stands_in(pg):
    """A direct call (no Prefect context) still records one run, as before."""
    pg._emit_training_lineage("mcbound", "FDataDataset", {"run_id": "mlf-direct"}, None, None, None)

    events = _events("train:mcbound")
    assert [(e["run_id"], e["event_type"]) for e in events] == [("mlf-direct", "COMPLETE")]


# ── the graph shows runs, not events ─────────────────────────────────────────


def _emit(event_type, run_id, **kw):
    lineage.emit_lineage(event_type, "train:graphm", run_id, model="graphm", **kw)


def test_one_entry_per_run_with_its_event_history():
    _emit("START", "r1", inputs=[lineage.dataset_node("D")])
    _emit(
        "COMPLETE",
        "r1",
        inputs=[lineage.dataset_node("D", "rev9")],
        dataset_revision="rev9",
        model_version="3",
        outputs=[lineage.model_node("graphm", 3)],
    )
    _emit("START", "r2")
    _emit("FAIL", "r2")

    runs = {r["run_id"]: r for r in lineage_graph("graphm")["runs"]}

    assert set(runs) == {"r1", "r2"}
    assert runs["r1"]["events"] == ["START", "COMPLETE"]
    assert runs["r1"]["event_type"] == "COMPLETE"
    assert runs["r2"]["events"] == ["START", "FAIL"] and runs["r2"]["event_type"] == "FAIL"


def test_a_later_fail_does_not_erase_what_the_run_recorded():
    """The synthetic-only promotion gate reads each run's dataset revision from this graph."""
    from examlops.promotion_gates import training_dataset_revisions

    _emit("COMPLETE", "r3", dataset_revision="rev-syn", model_version="4")
    _emit("FAIL", "r3")

    (run,) = lineage_graph("graphm")["runs"]
    assert run["event_type"] == "FAIL"
    assert (run["dataset_revision"], run["model_version"]) == ("rev-syn", "4")
    assert training_dataset_revisions("graphm") == ["rev-syn"]


def test_each_run_node_is_listed_once():
    _emit("START", "r4", inputs=[lineage.dataset_node("D")])
    _emit("COMPLETE", "r4", inputs=[lineage.dataset_node("D")])

    upstream = [u["node_name"] for u in lineage_graph("graphm")["upstream"]]
    assert upstream == ["examlops://dataset/D"]


# ── the failure reason, as a standard facet ──────────────────────────────────


def test_the_error_facet_is_the_standard_one_and_bounded():
    facet = lineage.error_facet("x" * 2000)["errorMessage"]

    assert facet["_schemaURL"].endswith("ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet")
    assert len(facet["message"]) == 500 and facet["message"].endswith("…")
    assert "stackTrace" not in facet, "a stack trace would carry local paths to the receiver"


# ── clause 5: correlated with the active trace ───────────────────────────────


def test_an_event_emitted_inside_a_span_carries_its_trace():
    """No path passed a trace id, so clause 5's correlation had no producer."""
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("promote") as span:
        lineage.emit_lineage("COMPLETE", "promote:tracem", "t1", model="tracem")
        expected = format(span.get_span_context().trace_id, "032x")

    (run,) = lineage_graph("tracem")["runs"]
    assert run["trace_id"] == expected
    assert json.loads(run["facets_json"])["examlops.trace"]["trace_id"] == expected


def test_without_a_span_there_is_no_trace_facet():
    lineage.emit_lineage("COMPLETE", "promote:tracem", "t2", model="tracem")

    (run,) = lineage_graph("tracem")["runs"]
    assert run["trace_id"] is None
    assert "examlops.trace" not in json.loads(run["facets_json"] or "{}")


# ── clause 2: cost and carbon, recorded when they are known ──────────────────


def _training_run(run_id, mlflow_run_id):
    lineage.emit_lineage("START", "train:costm", run_id, model="costm")
    lineage.emit_lineage("OTHER", "train:costm", run_id, model="costm", mlflow_run_id=mlflow_run_id)
    lineage.emit_lineage("COMPLETE", "train:costm", run_id, model="costm")


def test_the_recorded_cost_is_a_child_run_of_the_training_run():
    """Cost is known only after the scheduler accounts the job, long after the run ended. An OTHER
    on the finished run is what the spec suggests, and Marquez then shows the run RUNNING again —
    so the recording is its own run, whose standard parent facet names the training run."""
    _training_run("8a1f7c2e-5b3d-4e9f-a0c6-1d2e3f4a5b6c", "mlf-c1")

    assert lineage.attach_run_cost("mlf-c1", gpu_hours=2.5, cost_usd=5.0, kwh=1.2, co2e_kg=0.4)

    runs = {r["job"]: r for r in lineage_graph("costm")["runs"]}
    assert runs["train:costm"]["events"] == ["START", "OTHER", "COMPLETE"], "still finished"
    child = json.loads(runs["cost:costm"]["facets_json"])
    assert runs["cost:costm"]["event_type"] == "COMPLETE"
    assert child["parent"]["run"]["runId"] == "8a1f7c2e-5b3d-4e9f-a0c6-1d2e3f4a5b6c"
    assert child["parent"]["job"] == {"namespace": "examlops", "name": "train:costm"}
    cost = child["examlops.cost"]
    assert (cost["gpu_hours"], cost["cost_usd"], cost["kwh"], cost["co2e_kg"]) == (
        2.5,
        5.0,
        1.2,
        0.4,
    )


def test_each_recording_is_its_own_run():
    """`exa models cost --record` re-records every version each time; a second COMPLETE on one
    run would break the one-ending rule."""
    _training_run("fr-c3", "mlf-c3")

    lineage.attach_run_cost("mlf-c3", gpu_hours=1.0)
    lineage.attach_run_cost("mlf-c3", gpu_hours=1.0)

    costs = [r for r in lineage_graph("costm")["runs"] if r["job"] == "cost:costm"]
    assert len(costs) == 2 and all(r["events"] == ["COMPLETE"] for r in costs)


def test_a_cost_for_an_unknown_run_records_nothing():
    assert lineage.attach_run_cost("mlf-nobody", gpu_hours=1.0) is False
    assert lineage_graph("costm")["runs"] == []


def test_exa_models_cost_records_cost_and_carbon():
    from examlops.cli.commands import models as models_cmd

    lineage.emit_lineage("COMPLETE", "train:costm", "fr-c2", model="costm", mlflow_run_id="mlf-c2")

    models_cmd._attach_lineage_cost("mlf-c2", 3.0, None, 6.0)

    (child,) = [r for r in lineage_graph("costm")["runs"] if r["job"] == "cost:costm"]
    cost = json.loads(child["facets_json"])["examlops.cost"]
    assert cost["gpu_hours"] == 3.0 and cost["cost_usd"] == 6.0
    assert cost["kwh"] > 0 and cost["co2e_kg"] > 0, "carbon from the default provider"


def test_the_cost_command_records_the_child_run(monkeypatch):
    """Through the command, not the helper: `exa models cost --record` (mock scheduler)."""
    from unittest.mock import patch

    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "mock")
    monkeypatch.delenv("EXAMLOPS_HPC_SCHEDULER", raising=False)
    lineage.emit_lineage("COMPLETE", "train:jpcp", "fr-cmd", model="jpcp", mlflow_run_id="run-cmd1")
    registered = {"registered_model": {"latest_versions": [{"version": "3", "run_id": "run-cmd1"}]}}

    with patch("examlops.cli._client.get", return_value=registered):
        with patch("examlops.cli._client.post", return_value={}):
            result = CliRunner().invoke(app, ["models", "cost", "jpcp", "--record"])

    assert result.exit_code == 0, result.output
    (child,) = [r for r in lineage_graph("jpcp")["runs"] if r["job"] == "cost:jpcp"]
    facets = json.loads(child["facets_json"])
    assert facets["parent"]["job"]["name"] == "train:jpcp"
    assert facets["examlops.cost"]["gpu_hours"] > 0
