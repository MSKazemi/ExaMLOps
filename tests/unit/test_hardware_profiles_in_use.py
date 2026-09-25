# tests/unit/test_hardware_profiles_in_use.py
"""Hardware Profiles Phase 4 — the resolution ledger, ``in-use`` and ``exa status`` (ADR 0157).

ADR 0157's Consequences name the risk this closes: "a profile catalog that nothing actually
resolves against live capacity" — the resolution status must be visible wherever a profile is
used, not only at the moment ``resolve`` is typed. So every consumer (workbench create, training
run, distributed launch, serving deploy) appends its resolution to an append-only ledger, and
``in-use`` / ``exa status`` read it back with ``degraded``/``unresolvable``/``missing`` flagged.

Real code paths end to end (CLI -> examlops -> sqlite). The only fakes are the cluster
inventory rows seeded through ``examlops.data.hpc`` and the control plane in the status test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops import hardware_profiles as hp  # noqa: E402
from examlops import workbenches as wb  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data import get_db, init_db  # noqa: E402
from examlops.data import hardware_profiles as data  # noqa: E402
from examlops.data.projects import create_project  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    # setenv first so teardown restores "absent": `exa pipeline run` exports this in-process.
    monkeypatch.setenv("EXAMLOPS_HARDWARE_PROFILE", "")
    monkeypatch.delenv("EXAMLOPS_HARDWARE_PROFILE")
    init_db()
    create_project("demo")
    create_project("other")
    yield


def _gpu_small(**kw):
    params = dict(
        accelerator_family="nvidia",
        gpu_count=1,
        cpu=4,
        memory_gb=16,
        applicability=("training", "workbench", "serving"),
    )
    params.update(kw)
    return hp.create_profile_version("gpu-small", **params)


def _run(*args: str):
    return runner.invoke(app, list(args))


# ── the ledger ────────────────────────────────────────────────────────────────────────────


def test_resolve_for_with_consumer_ref_appends_one_ledger_row():
    _gpu_small()
    hp.resolve_for("gpu-small", "training", consumer_ref="JPCP", project="demo", actor="alice")
    rows = data.list_resolutions("gpu-small")
    assert len(rows) == 1
    r = rows[0]
    assert (r["consumer"], r["consumer_ref"], r["project"], r["actor"]) == (
        "training",
        "JPCP",
        "demo",
        "alice",
    )
    assert (r["version"], r["status"]) == (1, hp.STATUS_UNCHECKED)


def test_resolve_for_without_consumer_ref_records_nothing():
    """A read (``exa hardware profile resolve``, the dashboard's resolve) is not a use."""
    _gpu_small()
    hp.resolve_for("gpu-small", "training")
    assert data.list_resolutions() == []


def test_a_refused_applicability_is_not_recorded():
    hp.create_profile_version("wb-only", accelerator_family="cpu", applicability=("workbench",))
    with pytest.raises(hp.HardwareProfileError):
        hp.resolve_for("wb-only", "training", consumer_ref="JPCP")
    assert data.list_resolutions() == []


def test_ledger_write_failure_is_fail_open(monkeypatch, caplog):
    _gpu_small()

    def _boom(*a, **k):
        raise RuntimeError("attempt to write a readonly database")

    monkeypatch.setattr(data, "record_resolution", _boom)
    profile, resolution = hp.resolve_for("gpu-small", "training", consumer_ref="JPCP")
    assert resolution.status == hp.STATUS_UNCHECKED  # the call it observes still succeeded
    assert "could not record" in caplog.text


def test_record_resolution_rejects_unknown_consumer_and_empty_ref():
    _gpu_small()
    res = hp.resolve_profile("gpu-small")
    with pytest.raises(hp.HardwareProfileError, match="unknown consumer"):
        hp.record_resolution(res, consumer="notebook", consumer_ref="x")
    with pytest.raises(hp.HardwareProfileError, match="consumer_ref"):
        hp.record_resolution(res, consumer="training", consumer_ref="  ")


def test_project_filter_applies_before_the_limit():
    """50 newer rows from another project must not push the one ``demo`` row off the page."""
    _gpu_small()
    res = hp.resolve_profile("gpu-small")
    hp.record_resolution(res, consumer="training", consumer_ref="A", project="demo")
    for i in range(50):
        hp.record_resolution(res, consumer="training", consumer_ref=f"B{i}", project="other")
    rows = data.list_resolutions(project="demo", limit=1)
    assert [r["consumer_ref"] for r in rows] == ["A"]


def test_limit_is_clamped():
    _gpu_small()
    res = hp.resolve_profile("gpu-small")
    for i in range(3):
        hp.record_resolution(res, consumer="training", consumer_ref=f"m{i}")
    assert len(data.list_resolutions(limit=0)) == 1  # clamped up to 1, never "no limit"
    assert len(data.list_resolutions(limit=10**9)) == 3
    # newest first, ties on the second-resolution ts broken by id
    assert [r["consumer_ref"] for r in data.list_resolutions()] == ["m2", "m1", "m0"]


# ── consumers write the ledger ──────────────────────────────────────────────────────────────


def test_workbench_create_records_its_resolution_after_the_row_exists():
    _gpu_small()
    wb.create_workbench("nb1", "demo", hardware_profile="gpu-small", created_by="alice")
    rows = data.list_resolutions(consumer="workbench")
    assert [(r["consumer_ref"], r["version"], r["project"]) for r in rows] == [
        ("demo/nb1", 1, "demo")
    ]


def test_a_refused_workbench_create_leaves_no_ledger_row():
    hp.create_profile_version("trainer", accelerator_family="cpu", applicability=("training",))
    with pytest.raises(wb.WorkbenchError):
        wb.create_workbench("nb1", "demo", hardware_profile="trainer")
    assert data.list_resolutions() == []


def test_distributed_launch_topology_records_against_the_model():
    from examlops.cli.commands import distributed_cmd

    _gpu_small(nodes=2)
    assert distributed_cmd._topology("gpu-small", None, None, "JPCP") == (2, 1)
    rows = data.list_resolutions(consumer="training")
    assert [(r["consumer_ref"], r["version"]) for r in rows] == [("JPCP", 1)]


def test_pipeline_run_profile_ask_records_and_exports_the_exact_version(monkeypatch):
    from examlops.cli.commands import pipeline

    _gpu_small()
    _gpu_small(gpu_count=2)  # v2 becomes active
    ask = pipeline._hardware_profile_ask("gpu-small", None, 0, model="JPCP", project="demo")
    assert ask.gpus == 2
    import os

    assert os.environ["EXAMLOPS_HARDWARE_PROFILE"] == "gpu-small@v2"
    rows = data.list_resolutions(consumer="training")
    assert [(r["consumer_ref"], r["version"], r["project"]) for r in rows] == [("JPCP", 2, "demo")]


def test_serving_model_actor_options_records_against_the_model(tmp_path):
    from examlops.hardware_profiles_yaml import model_ray_actor_options

    _gpu_small(gpu_fraction=0.5)
    (tmp_path / "jpcp.yaml").write_text(
        "name: JPCP\nresources:\n  hardware_profile: gpu-small\n", encoding="utf-8"
    )
    opts = model_ray_actor_options("JPCP", models_dir=tmp_path)
    assert opts == {"num_gpus": 0.5, "num_cpus": 4.0}
    rows = data.list_resolutions(consumer="serving")
    assert [r["consumer_ref"] for r in rows] == ["JPCP"]


# ── in-use ────────────────────────────────────────────────────────────────────────────────


def test_in_use_lists_running_workbenches_only():
    _gpu_small()
    wb.create_workbench("running", "demo", hardware_profile="gpu-small")
    wb.create_workbench("stopped", "demo", hardware_profile="gpu-small")
    wb.create_workbench("plain", "demo")
    wb.start_workbench("running", "demo")
    wb.start_workbench("plain", "demo")
    report = hp.in_use_report()
    assert [(e["consumer"], e["consumer_ref"], e["status"]) for e in report["entries"]] == [
        ("workbench", "demo/running", hp.STATUS_UNCHECKED)
    ]
    assert report["attention"] == []


def test_deleting_a_bound_version_reports_missing():
    _gpu_small()
    wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    wb.start_workbench("nb1", "demo")
    data.delete_profile("gpu-small")
    report = hp.in_use_report()
    (entry,) = report["entries"]
    assert entry["status"] == hp.STATUS_MISSING and entry["exists"] is False
    assert report["attention"] == [entry]


def test_a_degraded_training_resolution_needs_attention():
    from examlops.data import hpc as hpc_data

    _gpu_small(accelerator_model_hint="H100-80GB")
    hpc_data.upsert_cluster("lxp", "slurm")
    hpc_data.set_cluster_state("lxp", "ACTIVE")
    hpc_data.record_node_snapshot(
        "lxp",
        "slurm",
        [
            {
                "name": "n1",
                "cpus": 32,
                "memory_mb": 200_000,
                "gpus": 4,
                "gpu_model": "A100-80GB",
                "state": "idle",
                "partition": "gpu",
            }
        ],
    )
    hp.resolve_for("gpu-small", "training", target_cluster="lxp", consumer_ref="JPCP")
    report = hp.in_use_report()
    (entry,) = report["attention"]
    assert entry["status"] == hp.STATUS_DEGRADED
    assert entry["unconfirmed"] == ["accelerator_model_hint"]
    assert entry["target_cluster"] == "lxp"


def test_only_the_latest_resolution_per_consumer_counts():
    _gpu_small()
    res = hp.resolve_profile("gpu-small")
    bad = hp.ProfileResolution(
        name=res.name,
        version=res.version,
        status=hp.STATUS_UNRESOLVABLE,
        reason="x",
        resources=res.resources,
    )
    hp.record_resolution(bad, consumer="training", consumer_ref="JPCP")
    hp.record_resolution(res, consumer="training", consumer_ref="JPCP")
    report = hp.in_use_report()
    assert [e["status"] for e in report["entries"]] == [hp.STATUS_UNCHECKED]


def test_in_use_window_excludes_old_training_rows():
    _gpu_small()
    hp.resolve_for("gpu-small", "training", consumer_ref="JPCP")
    with get_db() as conn:
        conn.execute("UPDATE hardware_profile_resolutions SET ts = datetime('now', '-30 days')")
    assert hp.in_use_report(days=7)["entries"] == []
    assert len(hp.in_use_report(days=60)["entries"]) == 1


def test_in_use_rejects_a_non_positive_window():
    with pytest.raises(hp.HardwareProfileError):
        hp.in_use_report(days=0)


# ── CLI ───────────────────────────────────────────────────────────────────────────────────


def test_cli_in_use_json_exits_1_on_attention():
    _gpu_small()
    wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    wb.start_workbench("nb1", "demo")
    ok = _run("--json", "hardware", "profile", "in-use")
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["entries"][0]["consumer_ref"] == "demo/nb1"

    data.delete_profile("gpu-small")
    bad = _run("--json", "hardware", "profile", "in-use")
    assert bad.exit_code == 1
    assert json.loads(bad.stdout)["attention"][0]["status"] == "missing"


def test_cli_in_use_project_scope():
    _gpu_small()
    for project in ("demo", "other"):
        wb.create_workbench("nb", project, hardware_profile="gpu-small")
        wb.start_workbench("nb", project)
    out = _run("--json", "hardware", "profile", "in-use", "--project", "other")
    assert [e["consumer_ref"] for e in json.loads(out.stdout)["entries"]] == ["other/nb"]


def test_cli_history_filters_and_rejects_unknown_consumer():
    _gpu_small()
    hp.resolve_for("gpu-small", "training", consumer_ref="JPCP")
    hp.resolve_for("gpu-small", "serving", consumer_ref="MACK")
    out = _run("--json", "hardware", "profile", "history", "gpu-small", "--consumer", "serving")
    assert out.exit_code == 0, out.output
    assert [r["consumer_ref"] for r in json.loads(out.stdout)] == ["MACK"]
    bad = _run("hardware", "profile", "history", "--consumer", "notebook")
    assert bad.exit_code == 1


def test_cli_delete_names_consumers_still_bound(monkeypatch):
    _gpu_small()
    wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    wb.start_workbench("nb1", "demo")
    out = _run("hardware", "profile", "delete", "gpu-small", "--yes")
    assert out.exit_code == 0, out.output
    assert "workbench:demo/nb1" in out.output
    assert hp.list_versions("gpu-small") == []


# ── shape validation (spec §2 field constraints) ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"name": "Bad Name"}, "slug"),
        ({"gpu_count": -1}, "gpu_count"),
        ({"cpu": -0.5}, "cpu"),
        ({"memory_gb": -1}, "memory_gb"),
        ({"nodes": 0}, "nodes"),
        ({"mig_profile": "9g.99gb"}, "mig_profile"),
    ],
)
def test_create_refuses_a_shape_no_consumer_could_honour(kw, match):
    name = kw.pop("name", "ok-name")
    with pytest.raises(hp.HardwareProfileError, match=match):
        hp.create_profile_version(name, accelerator_family="nvidia", **kw)
    assert hp.list_names() == []


def test_create_accepts_a_known_mig_profile():
    p = hp.create_profile_version(
        "mig", accelerator_family="nvidia", gpu_count=1, mig_profile="1g.5gb"
    )
    assert p.mig_profile == "1g.5gb"


# ── exa status ──────────────────────────────────────────────────────────────────────────────


def test_status_summary_counts_and_attention():
    from examlops.cli.commands import status

    _gpu_small()
    wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    wb.start_workbench("nb1", "demo")
    summary = status._hardware_profile_summary()
    assert summary == {
        "in_use": 1,
        "counts": {"unchecked": 1},
        "attention": [],
        "truncated": False,
    }


def test_status_summary_is_none_when_the_datastore_fails(monkeypatch):
    from examlops.cli.commands import status

    def _boom(**_):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(hp, "in_use_report", _boom)
    assert status._hardware_profile_summary() is None


# ── adversarial review (s30): bounds, tenant scope, fail-open, provenance ────────────────────


def _bulk_training_rows(ref: str, n: int, project: str | None = None) -> None:
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO hardware_profile_resolutions "
            "(name, version, consumer, consumer_ref, project, status) VALUES (?,?,?,?,?,?)",
            [("gpu-small", 1, "training", ref, project, hp.STATUS_UNCHECKED)] * n,
        )


def test_a_busy_consumer_cannot_push_a_quiet_one_out_of_in_use():
    """The "latest per consumer" reduction must happen before the bound, not after it.

    One unresolvable resolution for QUIET, then more than a page of rows for BUSY. De-duplicating
    a newest-N page in Python dropped QUIET entirely, and with it the only attention entry.
    """
    _gpu_small()
    res = hp.resolve_profile("gpu-small")
    bad = hp.ProfileResolution(
        name=res.name,
        version=res.version,
        status=hp.STATUS_UNRESOLVABLE,
        reason="x",
        resources=res.resources,
    )
    hp.record_resolution(bad, consumer="training", consumer_ref="QUIET")
    _bulk_training_rows("BUSY", data.MAX_RESOLUTION_ROWS + 5)
    report = hp.in_use_report()
    assert sorted(e["consumer_ref"] for e in report["entries"]) == ["BUSY", "QUIET"]
    assert [e["consumer_ref"] for e in report["attention"]] == ["QUIET"]
    assert report["truncated"] is False


def test_latest_per_consumer_reports_truncation_instead_of_hiding_it():
    _gpu_small()
    for ref in ("a", "b", "c"):
        _bulk_training_rows(ref, 2)
    rows, truncated = data.list_latest_resolutions("training", limit=2)
    assert [r["consumer_ref"] for r in rows] == ["c", "b"]
    assert truncated is True
    rows, truncated = data.list_latest_resolutions("training", limit=3)
    assert len(rows) == 3 and truncated is False


def test_tenant_scope_is_applied_in_sql_and_empty_scope_denies():
    _gpu_small()
    _bulk_training_rows("mine", 1, project="demo")
    _bulk_training_rows("theirs", 60, project="other")
    _bulk_training_rows("platform", 1, project=None)

    def refs(rows):
        return sorted({r["consumer_ref"] for r in rows})

    # the limit counts only in-scope rows: 60 newer "other" rows cannot hide "mine"
    assert refs(data.list_resolutions(projects={"demo"}, limit=1)) == ["mine"]
    assert refs(data.list_resolutions(projects={"demo", None})) == ["mine", "platform"]
    assert data.list_resolutions(projects=set()) == []  # default-deny, never "no filter"
    report = hp.in_use_report(projects={"demo"})
    assert [e["consumer_ref"] for e in report["entries"]] == ["mine"]


def test_in_use_scope_also_hides_other_projects_workbenches():
    _gpu_small()
    for project in ("demo", "other"):
        wb.create_workbench("nb", project, hardware_profile="gpu-small")
        wb.start_workbench("nb", project)
    report = hp.in_use_report(projects={"demo"})
    assert [e["consumer_ref"] for e in report["entries"]] == ["demo/nb"]


def test_workbench_create_survives_a_failed_ledger_reread(monkeypatch, caplog):
    """The row exists before the ledger is touched: a datastore error there must not raise."""
    _gpu_small()
    real = hp.resolve_profile
    calls = {"n": 0}

    def _second_call_fails(*a, **k):  # the pre-create sizing works; the post-create re-read fails
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(hp, "resolve_profile", _second_call_fails)
    created = wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    assert created["hardware_profile"] == "gpu-small"
    assert wb.get_workbench("nb1", "demo") is not None
    assert "not recorded" in caplog.text


def test_a_run_without_a_profile_does_not_inherit_a_stale_profile_tag(monkeypatch):
    import os

    from examlops.cli.commands import pipeline

    seen: dict[str, str | None] = {}
    monkeypatch.setattr(
        pipeline,
        "_run_generator",
        lambda args: seen.update(tag=os.environ.get("EXAMLOPS_HARDWARE_PROFILE")),
    )
    monkeypatch.setenv("EXAMLOPS_HARDWARE_PROFILE", "gpu-small@v9")
    pipeline._run_body(
        "JPCP", None, True, None, None, None, None, None, 0, None, None, hardware_profile=None
    )
    assert seen == {"tag": None}
