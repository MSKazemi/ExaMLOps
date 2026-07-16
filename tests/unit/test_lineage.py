# tests/unit/test_lineage.py
"""A2 — OpenLineage & provenance graph (ADR 0004, spec A2).

GWT-1 no-op when URL unset · GWT-3 impact analysis · GWT-4 dual-write ·
GWT-5 event schema shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import lineage  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    init_db,
    lineage_graph,
    lineage_impact,
)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("EXAMLOPS_OPENLINEAGE_URL", raising=False)
    init_db()


def test_gwt1_noop_when_url_unset():
    # No URL → no network; must not raise and the run "succeeds".
    lineage.emit_lineage(
        "COMPLETE",
        job="train.jpcp",
        run_id="run-1",
        inputs=[lineage.dataset_node("PM100Dataset", "revX")],
        outputs=[lineage.model_node("jpcp", 18)],
        dataset_revision="revX",
        model="jpcp",
        model_version=18,
    )
    # Dual-write still happened (R7): the operational DB has the rows.
    g = lineage_graph("jpcp")
    assert len(g["runs"]) == 1


def test_gwt4_dual_write_records_io():
    lineage.emit_lineage(
        "START",
        job="train.jpcp",
        run_id="run-2",
        inputs=[lineage.dataset_node("FData", "rev1")],
        outputs=[lineage.model_node("jpcp", 19)],
        dataset_revision="rev1",
        model="jpcp",
        model_version=19,
    )
    g = lineage_graph("jpcp")
    up = {n["node_name"] for n in g["upstream"]}
    down = {n["node_name"] for n in g["downstream"]}
    assert "examlops://dataset/FData@rev1" in up
    assert "examlops://model/jpcp/19" in down


def test_gwt3_impact_lists_derived_models():
    for i, model in enumerate(("m1", "m2"), start=1):
        lineage.emit_lineage(
            "COMPLETE",
            job=f"train.{model}",
            run_id=f"run-imp-{i}",
            inputs=[lineage.dataset_node("Shared", "revZ")],
            outputs=[lineage.model_node(model, i)],
            dataset_revision="revZ",
            model=model,
            model_version=i,
        )
    derived = lineage_impact("revZ")
    models = {r["model"] for r in derived}
    assert models == {"m1", "m2"}


def test_impact_empty_for_unknown_revision():
    assert lineage_impact("nope") == []


def test_gwt5_event_schema_shape():
    event = lineage.build_event(
        "COMPLETE",
        job="train.jpcp",
        run_id="run-3",
        inputs=[lineage.dataset_node("FData", "rev1")],
        outputs=[lineage.model_node("jpcp", 18)],
        facets=lineage.dataset_revision_facet("rev1"),
    )
    assert event["eventType"] == "COMPLETE"
    assert event["run"]["runId"] == "run-3"
    assert event["job"]["name"] == "train.jpcp"
    assert event["producer"].startswith("https://")
    assert event["inputs"][0]["name"] == "examlops://dataset/FData@rev1"
    # custom facet carries the required _producer/_schemaURL (R12)
    f = event["run"]["facets"]["examlops.dataset_revision"]
    assert f["_producer"] and f["_schemaURL"]
    assert f["revision"] == "rev1"


def test_cost_and_eval_facets():
    cf = lineage.cost_facet(12.5, kwh=3.0, co2e=1.2)["examlops.cost"]
    assert cf["gpu_hours"] == 12.5 and cf["co2e_kg"] == 1.2
    ef = lineage.eval_facet(0.92, metric="rmse")["examlops.eval"]
    assert ef["metric"] == "rmse" and ef["score"] == 0.92


def test_emit_fail_open_on_unreachable_url(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OPENLINEAGE_URL", "http://127.0.0.1:1/marquez")
    # Must not raise despite an unreachable endpoint (R6).
    lineage.emit_lineage(
        "COMPLETE", job="j", run_id="run-4", outputs=[lineage.model_node("jpcp", 1)], model="jpcp"
    )
    assert len(lineage_graph("jpcp")["runs"]) == 1
