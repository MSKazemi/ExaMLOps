# tests/unit/test_lineage_emit_paths.py
"""ADR 0004 clauses 1 and 2 — the paths that were supposed to emit lineage, and did not.

Clause 1: "a single `emit_lineage()` helper emits OpenLineage START/COMPLETE/FAIL run events
**from the training flow, promotion, and retrain paths**". The helper shipped, the dual-write
shipped, the fail-open shipped — and none of the three named paths called it. Its only callers
were `examlops.finetuning`, `examlops.distributed` and `exa data synth`, so the provenance graph
described the platform's side quests and not its main road.

Clause 2 names an `hpc_job_id` facet. There was none, and the only place that *knows* a
scheduler job id is the training flow — so the facet and its one real producer land together.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
sys.path.insert(0, str(Path(__file__).parents[2] / "pipelines"))

from examlops.cli.commands import pipeline as pipeline_cmd  # noqa: E402
from examlops.cli.commands import retrain as retrain_cmd  # noqa: E402
from examlops.lineage import hpc_job_id_facet  # noqa: E402


def _all_events() -> list[dict]:
    """Every recorded lineage run — there is no list-all helper, so read the table."""
    from examlops.platform_db import get_db, init_db

    init_db()
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM lineage_events ORDER BY id DESC")]


def _events(job_prefix: str) -> list[dict]:
    return [e for e in _all_events() if str(e["job"]).startswith(job_prefix)]


# ── clause 2: the facet ───────────────────────────────────────────────────────


def test_the_hpc_job_facet_carries_the_job_and_its_scheduler():
    facet = hpc_job_id_facet("f7Cw8cWS4fw5", "flux")
    assert facet["examlops.hpc_job"]["job_id"] == "f7Cw8cWS4fw5"
    assert facet["examlops.hpc_job"]["scheduler"] == "flux"


def test_the_facet_is_namespaced_like_every_sibling():
    """A flat facet would merge `_producer` into the top level and collide with the others."""
    from examlops.lineage import cost_facet, dataset_revision_facet

    for facet in (hpc_job_id_facet("j"), cost_facet(1.0), dataset_revision_facet("r")):
        assert len(facet) == 1
        key = next(iter(facet))
        assert key.startswith("examlops.")
        assert "_producer" in facet[key]


def test_the_facet_is_scheduler_neutral():
    """A graph that only understands Slurm cannot describe a Flux site."""
    for scheduler in ("slurm", "flux", "mock", None):
        facet = hpc_job_id_facet("job-1", scheduler)
        assert facet["examlops.hpc_job"]["job_id"] == "job-1"


def test_emit_lineage_attaches_the_facet_when_a_job_id_is_given():
    from examlops.lineage import emit_lineage

    emit_lineage(
        "COMPLETE", job="train:Facet", run_id="r-facet", hpc_job_id="j-9", scheduler="flux"
    )
    facets = str(_events("train:Facet")[0]["facets_json"])
    assert "j-9" in facets and "flux" in facets


def test_no_job_id_means_no_facet():
    """An absent scheduler job is not an empty one — a mock run has no job to name."""
    from examlops.lineage import emit_lineage

    emit_lineage("COMPLETE", job="train:NoFacet", run_id="r-nofacet")
    assert "hpc_job" not in str(_events("train:NoFacet")[0]["facets_json"])


# ── clause 1: promotion ───────────────────────────────────────────────────────


def test_a_promotion_emits_lineage_from_the_model_to_the_deployment():
    pipeline_cmd._emit_promotion_lineage("JPCP", "18", "Staging", "Production", "rmse", 4.2)

    event = _events("promote:JPCP")[0]

    assert event["event_type"] == "COMPLETE"
    assert event["model"] == "JPCP"
    assert event["model_version"] == "18"


def test_the_promotion_records_which_alias_it_came_from():
    """ "Where did production come from" is the question a promotion node exists to answer."""
    pipeline_cmd._emit_promotion_lineage("MACK", "3", "Canary", "Production", "rmse", 1.0)
    assert "Canary" in str(_events("promote:MACK")[0]["facets_json"])


def test_the_promotion_carries_the_metric_that_justified_it():
    pipeline_cmd._emit_promotion_lineage("MCB", "7", "Staging", "Production", "rmse", 4.25)
    assert "4.25" in str(_events("promote:MCB")[0]["facets_json"])


def test_promotion_lineage_never_fails_a_completed_promotion(monkeypatch):
    """The alias has already moved; a bookkeeping error must not report failure."""
    import examlops.lineage as lineage_mod

    def _boom(*_a, **_k):
        raise RuntimeError("marquez down")

    monkeypatch.setattr(lineage_mod, "emit_lineage", _boom)

    pipeline_cmd._emit_promotion_lineage("Swallowed", "1", "Staging", "Production", "rmse", 1.0)

    # Swallowed, not recorded — and the caller saw no exception.
    assert _events("promote:Swallowed") == []


# ── clause 1: retrain ─────────────────────────────────────────────────────────


def test_a_retrain_emits_a_start_event_not_a_completion():
    """The retrain has been *scheduled*; the flow emits its own completion when it finishes."""
    retrain_cmd._emit_retrain_lineage("jpcp", "PM100Dataset", {"flow_run_id": "fr-1"})

    event = _events("retrain:JPCP")[0]

    assert event["event_type"] == "START"
    assert event["model"] == "JPCP"


def test_the_retrain_run_id_is_the_prefect_flow_run_id():
    """A graph whose run ids match nothing in Prefect is a graph nobody can follow back."""
    retrain_cmd._emit_retrain_lineage("mack", "FDataDataset", {"flow_run_id": "fr-abc"})
    assert _events("retrain:MACK")[0]["run_id"] == "fr-abc"


def test_a_retrain_without_a_flow_run_id_still_records_a_run():
    retrain_cmd._emit_retrain_lineage("mcbound", "FDataDataset", {})
    assert _events("retrain:MCBOUND")[0]["run_id"].startswith("retrain-MCBOUND")


def test_retrain_lineage_is_fail_open(monkeypatch):
    import examlops.lineage as lineage_mod

    monkeypatch.setattr(
        lineage_mod, "emit_lineage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )

    retrain_cmd._emit_retrain_lineage("swallowed", "PM100Dataset", {"flow_run_id": "fr-2"})

    assert _events("retrain:SWALLOWED") == []


# ── clause 1: the training flow ───────────────────────────────────────────────


@pytest.fixture
def training():
    import pipeline_generator

    return pipeline_generator._emit_training_lineage


def test_a_training_run_emits_dataset_to_model_lineage(training):
    training("jpcp", "PM100Dataset", {"run_id": "mlf-1", "version": "18"}, None, None, "minio")

    event = _events("train:jpcp")[0]

    assert event["event_type"] == "COMPLETE"
    assert event["model_version"] == "18"
    assert event["mlflow_run_id"] == "mlf-1"


def test_the_training_run_carries_the_hpc_job_facet(training):
    """The training flow is the only path that knows a scheduler job id."""
    training("mack", "FDataDataset", {"run_id": "mlf-2", "version": "4"}, "j-77", "flux", None)

    facets = str(_events("train:mack")[0]["facets_json"])

    assert "j-77" in facets and "flux" in facets


def test_a_run_that_registered_nothing_is_still_recorded(training):
    """A training run that produced no registered version is exactly what a graph should show."""
    training("mcbound", "FDataDataset", {"run_id": "mlf-3"}, None, None, None)

    event = _events("train:mcbound")[0]

    assert event["model_version"] in (None, "")
    assert event["run_id"] == "mlf-3"


def test_training_lineage_never_fails_a_completed_training_run(training, monkeypatch, capsys):
    import examlops.lineage as lineage_mod

    monkeypatch.setattr(
        lineage_mod, "emit_lineage", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    training("jpcp", "PM100Dataset", {"run_id": "mlf-4"}, None, None, None)
    assert "lineage emit skipped" in capsys.readouterr().out


# ── all three named paths, together ───────────────────────────────────────────


def test_every_path_the_adr_names_now_emits(training):
    """The clause names three; before this iteration the graph had none of them."""
    training("jpcp", "PM100Dataset", {"run_id": "mlf-9", "version": "18"}, "j-1", "flux", None)
    pipeline_cmd._emit_promotion_lineage("jpcp", "18", "Staging", "Production", "rmse", 4.2)
    retrain_cmd._emit_retrain_lineage("jpcp", "PM100Dataset", {"flow_run_id": "fr-9"})

    jobs = {str(e["job"]).split(":")[0] for e in _all_events()}

    assert {"train", "promote", "retrain"} <= jobs
