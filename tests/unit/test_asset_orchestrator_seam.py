# tests/unit/test_asset_orchestrator_seam.py
"""ADR 0036 clauses 1 and 3 — the seam the decision is built around, and "via the scheduler".

The recorded finding: "the `AssetOrchestrator` seam the decision is built around **does not exist
as code** — it and Dagster are named only in the module docstring, so nothing is swappable; clause
3's 'via the scheduler (phase 23)' is not done — materializing an asset calls a local Python
function and bumps a row, and never submits to Slurm or Flux."
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import assets  # noqa: E402
from examlops.assets import (  # noqa: E402
    AssetOrchestrator,
    LocalOrchestrator,
    SchedulerOrchestrator,
    declare_asset,
    get_orchestrator,
    materialize,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("EXAMLOPS_ASSET_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("EXAMLOPS_TEST_ASSET_MARKER", str(tmp_path / "marker.json"))
    monkeypatch.setenv("EXAMLOPS_ASSET_JOB_DIR", str(tmp_path / "asset-jobs"))


def _fx():
    # A job imports its production function by name, so the scheduler path needs a module-level
    # one. Imported inside the test, never at collection: its `@asset` writes to PLATFORM_DB.
    from tests.unit import _asset_job_fixtures

    return _asset_job_fixtures


class _Adapter:
    """A scheduler that records what it was asked to submit and reports it COMPLETED.

    It does not run the script — `test_asset_scheduler_job.py` drives the real mock, Slurm and
    Flux adapters for that. This stand-in once accepted a submission with no script at all,
    which is how a scheduler path that could not run on any real scheduler passed its tests.
    """

    def __init__(self, tmp_path=None):
        import tempfile

        self.working_dir = Path(tmp_path or tempfile.mkdtemp())
        self.submitted: list[dict] = []

    def submit_job(self, script_path=None, resources=None, training_data=None, remote_dir=None):
        assert script_path, "a real adapter refuses a job with no script"
        self.submitted.append({"resources": resources, "script": Path(script_path).read_text()})
        return "job-42"

    def wait_until_complete(self, job_id, poll_interval=0, max_wait_s=None):
        return ""

    def get_job_status(self, job_id):
        return {"state": "COMPLETED", "exit_code": 0, "start_time": None, "end_time": None}

    def get_job_logs(self, job_id):
        return ""


# ── the seam exists as code ───────────────────────────────────────────────────


def test_both_orchestrators_satisfy_the_protocol():
    """Named only in a docstring, nothing is swappable — that was the finding."""
    assert isinstance(LocalOrchestrator(), AssetOrchestrator)
    assert isinstance(SchedulerOrchestrator(), AssetOrchestrator)


def test_local_is_the_default():
    """An asset layer that starts submitting scheduler jobs on upgrade surprises everyone."""
    assert get_orchestrator().name == "local"


def test_the_environment_selects_the_orchestrator(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ASSET_ORCHESTRATOR", "scheduler")
    assert get_orchestrator().name == "scheduler"


def test_an_explicit_name_beats_the_environment(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ASSET_ORCHESTRATOR", "scheduler")
    assert get_orchestrator("local").name == "local"


def test_an_unrecognised_name_falls_back_to_local(monkeypatch):
    """A typo must leave the asset built, not route it to an engine nobody configured."""
    monkeypatch.setenv("EXAMLOPS_ASSET_ORCHESTRATOR", "dagsterr")
    assert get_orchestrator().name == "local"


# ── local: unchanged behaviour ────────────────────────────────────────────────


def test_local_calls_the_production_function_with_upstream_versions():
    seen = {}

    class _Def:
        name = "d"
        fn = staticmethod(lambda **kw: seen.update(kw))

    LocalOrchestrator().run(_Def(), {"upstream_a": 3})

    assert seen == {"upstream_a": 3}


def test_local_tolerates_an_asset_with_no_production_function():
    class _Def:
        name = "d"
        fn = None

    assert LocalOrchestrator().run(_Def(), {})["orchestrator"] == "local"


def test_materialize_still_runs_in_process_by_default():
    calls = []
    declare_asset("SeamA", kind="dataset")
    assets._REGISTRY["SeamA"] = assets.AssetDef(
        name="SeamA", kind="dataset", deps=[], fn=lambda **kw: calls.append(1)
    )

    materialize("SeamA", force=True)

    assert calls == [1]


# ── scheduler: clause 3 ───────────────────────────────────────────────────────


def test_the_scheduler_orchestrator_submits_a_job(monkeypatch):
    adapter = _Adapter()
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: adapter)

    result = SchedulerOrchestrator().run(assets.AssetDef("big_model", fn=_fx().build_marker), {})

    assert result["hpc_job_id"] == "job-42"
    assert adapter.submitted[0]["resources"]["job_name"] == "asset-big_model"


def test_the_submitted_job_cannot_resubmit_itself(monkeypatch):
    """A job that re-entered `exa assets materialize` would re-walk the graph and submit again
    (the old design needed `--no-deps --orchestrator local` to stop it). The job now runs the
    production function and nothing else, so there is no graph for it to walk."""
    adapter = _Adapter()
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: adapter)

    SchedulerOrchestrator().run(assets.AssetDef("m", fn=_fx().build_marker), {})

    script = adapter.submitted[0]["script"]
    assert "-m examlops.assets.job" in script
    assert "materialize" not in script


def test_a_missing_scheduler_falls_back_to_local_and_says_so(monkeypatch):
    """An absent scheduler is an environment fact, not an asset failure."""
    monkeypatch.setattr(
        assets, "_scheduler_adapter", lambda: (_ for _ in ()).throw(RuntimeError("no sbatch"))
    )

    result = SchedulerOrchestrator().run(assets.AssetDef("m", fn=_fx().build_marker), {})

    assert Path(os.environ["EXAMLOPS_TEST_ASSET_MARKER"]).exists(), "the asset must still get built"
    assert "no sbatch" in result["fallback"]


def test_materialize_routes_through_the_scheduler_when_asked(monkeypatch):
    adapter = _Adapter()
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: adapter)
    declare_asset("SeamB", kind="model")
    assets._REGISTRY["SeamB"] = assets.AssetDef(
        name="SeamB", kind="model", deps=[], fn=_fx().build_marker
    )

    materialize("SeamB", force=True, orchestrator="scheduler")

    assert adapter.submitted, "clause 3: materialization must reach the scheduler"


# ── --no-deps ─────────────────────────────────────────────────────────────────


def test_no_deps_builds_only_the_named_asset():
    built = []
    for name in ("Up1", "Down1"):
        declare_asset(name, kind="dataset", deps=["Up1"] if name == "Down1" else [])
        assets._REGISTRY[name] = assets.AssetDef(
            name=name,
            kind="dataset",
            deps=["Up1"] if name == "Down1" else [],
            fn=(lambda n: lambda **kw: built.append(n))(name),
        )

    result = materialize("Down1", force=True, no_deps=True)

    assert result.rebuilt == ["Down1"]
    assert built == ["Down1"]


def test_without_no_deps_ancestors_are_still_rebuilt():
    built = []
    for name in ("Up2", "Down2"):
        declare_asset(name, kind="dataset", deps=["Up2"] if name == "Down2" else [])
        assets._REGISTRY[name] = assets.AssetDef(
            name=name,
            kind="dataset",
            deps=["Up2"] if name == "Down2" else [],
            fn=(lambda n: lambda **kw: built.append(n))(name),
        )

    materialize("Down2", force=True)

    assert built == ["Up2", "Down2"]


# ── provenance ────────────────────────────────────────────────────────────────


def test_the_lineage_event_records_which_orchestrator_built_the_version(monkeypatch):
    """An asset built on a cluster and one built in a notebook are different facts."""
    adapter = _Adapter()
    monkeypatch.setattr(assets, "_scheduler_adapter", lambda: adapter)
    declare_asset("SeamC", kind="model")
    assets._REGISTRY["SeamC"] = assets.AssetDef(
        name="SeamC", kind="model", deps=[], fn=_fx().build_marker
    )

    materialize("SeamC", force=True, orchestrator="scheduler")

    from examlops.platform_db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT facets_json FROM lineage_events WHERE job='asset:SeamC' ORDER BY id DESC"
        ).fetchone()
    assert "scheduler" in str(row["facets_json"])
    assert "job-42" in str(row["facets_json"])


def test_a_local_build_is_recorded_as_local():
    declare_asset("SeamD", kind="dataset")
    assets._REGISTRY["SeamD"] = assets.AssetDef(name="SeamD", kind="dataset", deps=[], fn=None)

    materialize("SeamD", force=True)

    from examlops.platform_db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT facets_json FROM lineage_events WHERE job='asset:SeamD' ORDER BY id DESC"
        ).fetchone()
    assert "local" in str(row["facets_json"])


def test_the_audit_event_names_the_orchestrator():
    from examlops.data.audit import export_audit_events

    declare_asset("SeamE", kind="dataset")
    assets._REGISTRY["SeamE"] = assets.AssetDef(name="SeamE", kind="dataset", deps=[], fn=None)

    materialize("SeamE", force=True)

    events = [
        e
        for e in export_audit_events()
        if e["action"] == "asset_materialize" and e["target"] == "SeamE"
    ]
    assert events and "local" in str(events[-1]["details"])
