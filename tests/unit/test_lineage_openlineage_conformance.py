# tests/unit/test_lineage_openlineage_conformance.py
"""ADR 0004 — the events ExaMLOps emits validate against the OpenLineage 2-0-2 schema.

Found standing up Marquez for clause 3 (BL-068) and reading back what it had stored. The emitter
called its events "OpenLineage-schema-conformant", and three things were not:

- ``run.runId`` was the platform's own id. The schema says ``format: uuid``, and the training
  flow sends an MLflow run id (32 hex digits, no dashes) or ``train-<model>-<dataset>``. Marquez
  quietly substitutes a UUID of its own, so the MLflow id was lost from the graph; a stricter
  receiver refuses the event.
- A dataset revision was part of the dataset **name** (``FData@abc123``), so a receiver drew one
  unrelated dataset per revision instead of one dataset with versions.
- Callers passed bare values as facets (``{"backend": "minio"}``). A facet must be an object with
  ``_producer`` and ``_schemaURL``.

These validate against the published schemas, vendored in ``fixtures/openlineage/``.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import lineage  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402

jsonschema = pytest.importorskip("jsonschema")
referencing = pytest.importorskip("referencing")

FIXTURES = Path(__file__).parent / "fixtures" / "openlineage"
MLFLOW_RUN = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"


def _validator():
    from referencing import Registry, Resource

    spec = json.loads((FIXTURES / "OpenLineage-2-0-2.json").read_text())
    facet = json.loads((FIXTURES / "DatasetVersionDatasetFacet-1-0-1.json").read_text())
    registry = Registry().with_resources(
        [(s["$id"], Resource.from_contents(s)) for s in (spec, facet)]
    )
    run_event = {"$ref": f"{spec['$id']}#/$defs/RunEvent"}
    version_facet = {"$ref": f"{facet['$id']}#/$defs/DatasetVersionDatasetFacet"}
    checker = jsonschema.Draft202012Validator.FORMAT_CHECKER
    make = jsonschema.Draft202012Validator
    return (
        make(run_event, registry=registry, format_checker=checker),
        make(version_facet, registry=registry, format_checker=checker),
    )


RUN_EVENT, VERSION_FACET = _validator()


def _assert_valid(event):
    errors = sorted(RUN_EVENT.iter_errors(event), key=str)
    assert not errors, [e.message for e in errors]
    for dataset in event["inputs"] + event["outputs"]:
        if "version" in dataset.get("facets", {}):
            VERSION_FACET.validate(dataset["facets"]["version"])


def _training_event(run_id=MLFLOW_RUN, **facets):
    return lineage.build_event(
        "COMPLETE",
        "train:jpcp",
        run_id,
        inputs=[lineage.dataset_node("FData", "abc123")],
        outputs=[lineage.model_node("jpcp", 18)],
        facets={
            **lineage.dataset_revision_facet("abc123"),
            **lineage.hpc_job_id_facet("4242", "flux"),
            **facets,
        },
    )


@pytest.mark.parametrize(
    "run_id",
    [
        MLFLOW_RUN,
        "5f1d2c1e-8b0a-4c7e-9a52-3e1f6d0b9c47",
        "train-jpcp-FData",
        "asset:features@3",
        "",
    ],
)
def test_every_event_validates_against_the_published_schema(run_id):
    _assert_valid(_training_event(run_id, backend="minio", **{"dataplane.source": "pg/orders"}))


def test_the_schema_check_is_not_vacuous():
    """The validator really checks the uuid format and the facet contract."""
    event = _training_event()
    event["run"]["runId"] = MLFLOW_RUN
    assert list(RUN_EVENT.iter_errors(event)), "a dashless run id must fail the schema"
    event = _training_event()
    event["run"]["facets"]["backend"] = {"value": "minio"}
    assert list(RUN_EVENT.iter_errors(event)), "a facet without _producer must fail the schema"


# ── run ids ──────────────────────────────────────────────────────────────────


def test_an_mlflow_run_id_is_sent_as_the_uuid_it_already_is():
    """So the receiver's run and the MLflow run share one id, dashes aside."""
    event = _training_event(MLFLOW_RUN)

    assert event["run"]["runId"].replace("-", "") == MLFLOW_RUN
    assert event["run"]["facets"]["examlops.run"]["run_id"] == MLFLOW_RUN


def test_any_other_id_maps_to_one_uuid_for_start_and_complete():
    start = lineage.build_event("START", "retrain:jpcp", "retrain-jpcp-7")
    done = lineage.build_event("COMPLETE", "retrain:jpcp", "retrain-jpcp-7")

    assert start["run"]["runId"] == done["run"]["runId"]
    assert start["run"]["facets"]["examlops.run"]["run_id"] == "retrain-jpcp-7"


def test_the_name_based_mapping_is_fixed_forever():
    """Changing the namespace would give every past run a new id in the receiver."""
    assert lineage.lineage_run_id("train-jpcp-FData") == str(
        uuid.uuid5(
            uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/MSKazemi/ExaMLOps/openlineage/run"),
            "train-jpcp-FData",
        )
    )
    assert lineage.lineage_run_id("train-jpcp-FData") == "5b6df02d-7a42-5d4f-8a5c-6864b4e9248f"


def test_a_canonical_uuid_needs_no_translation_facet():
    rid = str(uuid.uuid4())
    event = lineage.build_event("COMPLETE", "j", rid)

    assert event["run"]["runId"] == rid and event["run"]["facets"] == {}


# ── datasets and versions ────────────────────────────────────────────────────


def test_a_revision_is_a_version_of_one_dataset_not_a_new_dataset():
    a = _training_event()["inputs"][0]
    b = lineage.build_event("COMPLETE", "j", "r", inputs=[lineage.dataset_node("FData", "def")])

    assert a["name"] == b["inputs"][0]["name"] == "examlops://dataset/FData"
    assert a["facets"]["version"]["datasetVersion"] == "abc123"
    assert b["inputs"][0]["facets"]["version"]["datasetVersion"] == "def"


def test_an_unversioned_dataset_carries_no_version_facet():
    for node in (lineage.dataset_node("FData"), lineage.dataset_node("FData", "")):
        (entry,) = lineage.build_event("COMPLETE", "j", "r", inputs=[node])["inputs"]
        assert entry == {"namespace": "examlops", "name": "examlops://dataset/FData"}


def test_platform_db_keeps_the_revision_in_the_node_name():
    """Only the event is translated: `exa models lineage --impact <revision>` reads these rows."""
    from examlops.platform_db import lineage_impact

    init_db()
    lineage.emit_lineage(
        "COMPLETE",
        "train:jpcp",
        MLFLOW_RUN,
        inputs=[lineage.dataset_node("FData", "abc123")],
        outputs=[lineage.model_node("jpcp", 18)],
        dataset_revision="abc123",
        model="jpcp",
        model_version=18,
    )

    assert lineage_impact("abc123"), "the revision still finds the model trained on it"


# ── facets ───────────────────────────────────────────────────────────────────


def test_loose_values_are_gathered_into_one_conformant_facet():
    facets = _training_event(backend="minio", **{"dataplane.source": "pg/orders"})["run"]["facets"]

    assert "backend" not in facets and "dataplane.source" not in facets
    assert facets["examlops.run"]["backend"] == "minio"
    assert facets["examlops.run"]["dataplane.source"] == "pg/orders"
    assert facets["examlops.hpc_job"]["job_id"] == "4242", "conformant facets pass through"


def test_the_producer_is_this_project():
    event = _training_event()

    assert event["producer"] == "https://github.com/MSKazemi/ExaMLOps"
    assert all(f["_producer"] == event["producer"] for f in event["run"]["facets"].values())


def test_what_is_posted_is_the_translated_event(monkeypatch):
    posted = []
    monkeypatch.setenv("EXAMLOPS_OPENLINEAGE_URL", "http://marquez:5000")
    monkeypatch.setattr(lineage, "_post", lambda url, event: posted.append((url, event)))
    init_db()

    lineage.emit_lineage("COMPLETE", "train:jpcp", MLFLOW_RUN, facets={"backend": ""})

    ((url, event),) = posted
    assert url == "http://marquez:5000"
    _assert_valid(event)


def test_the_mlflow_run_and_trace_reach_the_event(monkeypatch):
    """They were written to platform_db's columns only, so a receiver showed a run it could not
    link back to MLflow or to its OTel trace (clauses 2 and 5)."""
    posted = []
    monkeypatch.setenv("EXAMLOPS_OPENLINEAGE_URL", "http://marquez:5000")
    monkeypatch.setattr(lineage, "_post", lambda url, event: posted.append(event))
    init_db()

    lineage.emit_lineage(
        "COMPLETE", "promote:jpcp", "promote-jpcp-18", mlflow_run_id=MLFLOW_RUN, trace_id="4bf92f35"
    )

    (event,) = posted
    _assert_valid(event)
    assert event["run"]["facets"]["examlops.mlflow_run"]["run_id"] == MLFLOW_RUN
    assert event["run"]["facets"]["examlops.trace"]["trace_id"] == "4bf92f35"
