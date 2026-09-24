# tests/unit/test_hardware_profiles_workbench.py
"""Hardware Profiles × Workbenches — ADR 0157 Phase 2, spec §4 "Phase 2" / §5 GWT-5.

GWT-5 — ``exa workbench create nb1 --project demo --hardware-profile gpu-small`` creates a
workbench with ``cpu=4``, ``memory_gb=16`` and ``hardware_profile='gpu-small'`` recorded; passing
``--cpu 8`` alongside overrides **only** cpu.

Plus the three refusals/invariants the phase turns on:

* a profile whose ``applicability`` covers neither ``workbench`` nor ``any`` is refused, and the
  error names what it actually declares (never a silent ignore);
* a profile that does not exist is refused;
* the no-profile path is unchanged — same row, same columns, with the two new columns NULL — so
  every workbench created before profiles existed still reads exactly as it did.

Real code paths end to end: CLI -> ``examlops.workbenches`` -> ``examlops.hardware_profiles`` ->
``examlops.data.*`` -> sqlite. No mocks of this repo's own code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops import workbenches as wb  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data import get_db, init_db  # noqa: E402
from examlops.data.projects import create_project  # noqa: E402
from examlops.hardware_profiles import create_profile_version  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()
    create_project("demo")
    yield


def _run(*args: str):
    return runner.invoke(app, list(args))


def _gpu_small(**kw):
    """The spec's own example profile: nvidia, 1 GPU, 4 cores, 16 GB, training+workbench."""
    params = dict(
        accelerator_family="nvidia",
        gpu_count=1,
        cpu=4,
        memory_gb=16,
        applicability=("training", "workbench"),
    )
    params.update(kw)
    return create_profile_version("gpu-small", **params)


# ── GWT-5 — the profile supplies the defaults, and the row records which one ────────────────


def test_gwt5_create_with_profile_fills_cpu_memory_and_records_profile_version():
    _gpu_small()
    row = wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    assert row["cpu"] == 4.0
    assert row["memory_gb"] == 16.0
    assert row["hardware_profile"] == "gpu-small"
    assert row["hardware_profile_version"] == 1


def test_gwt5_cli_create_with_profile(capsys):
    _gpu_small()
    result = _run(
        "workbench", "create", "nb1", "--project", "demo", "--hardware-profile", "gpu-small"
    )
    assert result.exit_code == 0, result.output
    got = wb.get_workbench("nb1", "demo")
    assert (got["cpu"], got["memory_gb"]) == (4.0, 16.0)
    assert (got["hardware_profile"], got["hardware_profile_version"]) == ("gpu-small", 1)


def test_gwt5_explicit_cpu_overrides_only_cpu():
    _gpu_small()
    row = wb.create_workbench("nb1", "demo", cpu=8, hardware_profile="gpu-small")
    assert row["cpu"] == 8.0  # the explicit flag wins
    assert row["memory_gb"] == 16.0  # everything else still comes from the profile
    assert row["hardware_profile_version"] == 1


def test_gwt5_cli_explicit_cpu_overrides_only_cpu_and_says_so():
    _gpu_small()
    result = _run(
        "workbench", "create", "nb1", "--project", "demo",
        "--hardware-profile", "gpu-small", "--cpu", "8",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    got = wb.get_workbench("nb1", "demo")
    assert (got["cpu"], got["memory_gb"]) == (8.0, 16.0)
    # A partial override must be reported, never silent (spec §4 "explicit flags win").
    assert "--cpu" in result.output
    assert "gpu-small" in result.output


def test_partial_override_is_logged_by_the_library(caplog):
    """A non-CLI caller (dashboard, SDK) must see the partial application too."""
    _gpu_small()
    with caplog.at_level("INFO", logger="examlops.workbenches"):
        wb.create_workbench("nb1", "demo", memory_gb=64, hardware_profile="gpu-small")
    said = [r.getMessage() for r in caplog.records]
    assert any("gpu-small" in m and "memory_gb" in m for m in said), caplog.text


def test_an_explicit_value_equal_to_the_profiles_is_still_the_callers():
    """Explicitness is about who set the value, not whether it differs — the report must not
    depend on the two happening to agree."""
    _gpu_small()
    row = wb.create_workbench("nb1", "demo", cpu=4, hardware_profile="gpu-small")
    assert row["cpu"] == 4.0 and row["memory_gb"] == 16.0


def test_a_fractional_cpu_profile_is_not_truncated():
    """ProfileResolution.resources.cpus is an int (the admission seam's shape); the workbench
    column is REAL, so the profile's own float is what must land."""
    create_profile_version(
        "cpu-half", accelerator_family="cpu", cpu=0.5, memory_gb=2, applicability=("workbench",)
    )
    row = wb.create_workbench("tiny", "demo", hardware_profile="cpu-half")
    assert row["cpu"] == 0.5


def test_the_recorded_version_is_pinned_at_create_time():
    """The `active` label moving afterwards must not rewrite what an existing workbench says it
    was created from."""
    _gpu_small()
    wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    _gpu_small(cpu=32, memory_gb=128)  # version 2; `active` moves to it
    wb.create_workbench("nb2", "demo", hardware_profile="gpu-small")
    assert wb.get_workbench("nb1", "demo")["hardware_profile_version"] == 1
    assert wb.get_workbench("nb1", "demo")["cpu"] == 4.0
    assert wb.get_workbench("nb2", "demo")["hardware_profile_version"] == 2
    assert wb.get_workbench("nb2", "demo")["cpu"] == 32.0


# ── applicability gate — refuse, and name what the profile actually declares ────────────────


def test_applicability_without_workbench_is_refused_by_the_library():
    create_profile_version(
        "train-only",
        accelerator_family="nvidia",
        gpu_count=8,
        cpu=64,
        memory_gb=512,
        applicability=("training",),
    )
    with pytest.raises(wb.WorkbenchError) as exc:
        wb.create_workbench("nb1", "demo", hardware_profile="train-only")
    msg = str(exc.value)
    assert "train-only" in msg
    assert "training" in msg  # names the ACTUAL applicability, not just the requirement
    assert "workbench" in msg
    assert wb.get_workbench("nb1", "demo") is None  # nothing written


def test_applicability_without_workbench_is_refused_by_the_cli():
    create_profile_version(
        "serve-only", accelerator_family="nvidia", cpu=2, applicability=("serving",)
    )
    result = _run(
        "workbench", "create", "nb1", "--project", "demo", "--hardware-profile", "serve-only"
    )
    assert result.exit_code == 1
    assert "serve-only" in result.output and "serving" in result.output
    assert wb.get_workbench("nb1", "demo") is None


def test_applicability_any_is_accepted():
    create_profile_version(
        "anywhere", accelerator_family="cpu", cpu=2, memory_gb=8, applicability=("any",)
    )
    row = wb.create_workbench("nb1", "demo", hardware_profile="anywhere")
    assert (row["cpu"], row["memory_gb"]) == (2.0, 8.0)


# ── unknown profile — refused, not silently ignored ─────────────────────────────────────────


def test_unknown_profile_is_refused():
    with pytest.raises(wb.WorkbenchError) as exc:
        wb.create_workbench("nb1", "demo", hardware_profile="nope")
    assert "nope" in str(exc.value)
    assert wb.get_workbench("nb1", "demo") is None


def test_unknown_profile_is_refused_by_the_cli():
    result = _run("workbench", "create", "nb1", "--project", "demo", "--hardware-profile", "nope")
    assert result.exit_code == 1
    assert "nope" in result.output
    assert wb.get_workbench("nb1", "demo") is None


# ── regression — the no-profile path is exactly what it was ─────────────────────────────────


def test_no_profile_path_is_unchanged():
    row = wb.create_workbench("plain", "demo", cpu=2, memory_gb=4, created_by="alice")
    assert row["cpu"] == 2.0 and row["memory_gb"] == 4.0
    assert row["status"] == "STOPPED"
    assert row["storage_volume"] == "demo-plain-data"
    assert row["image"] == "jupyter/scipy-notebook:latest"
    assert row["created_by"] == "alice"
    # The two new columns exist and are NULL — "created without a profile", never a fabricated one.
    assert row["hardware_profile"] is None
    assert row["hardware_profile_version"] is None


def test_no_profile_path_leaves_cpu_and_memory_unset():
    row = wb.create_workbench("plain", "demo")
    assert row["cpu"] is None and row["memory_gb"] is None
    assert row["hardware_profile"] is None


def test_list_surfaces_the_profile_and_a_dash_without_one():
    _gpu_small()
    wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    wb.create_workbench("plain", "demo")
    result = _run("workbench", "list", "--project", "demo")
    assert result.exit_code == 0, result.output
    assert "gpu-small v1" in result.output.replace("\n", " ")


def test_list_json_carries_the_recorded_profile():
    _gpu_small()
    wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    result = _run("--output", "json", "workbench", "list", "--project", "demo")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert rows[0]["hardware_profile"] == "gpu-small"
    assert rows[0]["hardware_profile_version"] == 1


# ── the migration reaches a database whose `workbenches` table predates the feature ─────────


def test_a_pre_existing_workbenches_table_gains_the_columns(tmp_path, monkeypatch):
    """The columns are additive: a table created by an older build must gain them through
    ``platform_db._COLUMN_MIGRATIONS``, not be left at its old shape forever (the live finding
    that guard exists for).

    The older build is simulated by writing the pre-feature DDL into a *fresh* datastore before
    the schema bootstrap runs — through ``get_db()``, so the write lands in whichever engine is
    configured rather than in a SQLite file the platform may not be using at all.
    """
    db = tmp_path / "old.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    with get_db() as old:
        # IF NOT EXISTS only so the same DDL is replayable on a shared (Postgres) schema; on the
        # brand-new file this test actually points at, the table cannot pre-exist, so the old
        # shape below is always the one the bootstrap then has to migrate.
        old.execute(
            """CREATE TABLE IF NOT EXISTS workbenches (
               name TEXT NOT NULL, project TEXT NOT NULL,
               image TEXT NOT NULL DEFAULT 'jupyter/scipy-notebook:latest',
               cpu REAL, memory_gb REAL, storage_volume TEXT,
               status TEXT NOT NULL DEFAULT 'STOPPED',
               created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, created_by TEXT,
               PRIMARY KEY (project, name))"""
        )
        old.execute(
            "INSERT INTO workbenches (name, project, storage_volume) VALUES ('legacy','demo','v')"
        )

    init_db(force=True)
    create_project("demo")
    _gpu_small()
    row = wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    assert row["hardware_profile_version"] == 1
    legacy = wb.get_workbench("legacy", "demo")
    assert legacy is not None and legacy["hardware_profile"] is None


def test_a_table_created_after_the_bootstrap_still_gains_the_columns(tmp_path, monkeypatch):
    """`workbenches` is created lazily — by this module and by the dashboard router's own copy of
    the DDL — so the schema bootstrap can run *before* it exists and find nothing to migrate.

    Reproduced as `OperationalError: table workbenches has no column named hardware_profile` on
    the very first create: bootstrap (table absent) -> another writer creates the old shape ->
    create_workbench. Without the migration inside `_ensure_table` this test fails.
    """
    db = tmp_path / "late.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    init_db()  # caches this path as bootstrapped, with no `workbenches` table yet
    create_project("demo")
    # "another writer" is a second connection, not a second engine: opening it through the seam
    # keeps the race being reproduced (bootstrap already done, old-shape table appears afterwards)
    # while the write still goes to whatever datastore the platform is configured to use.
    with get_db() as stale:
        stale.execute(
            """CREATE TABLE IF NOT EXISTS workbenches (
               name TEXT NOT NULL, project TEXT NOT NULL,
               image TEXT NOT NULL DEFAULT 'jupyter/scipy-notebook:latest',
               cpu REAL, memory_gb REAL, storage_volume TEXT,
               status TEXT NOT NULL DEFAULT 'STOPPED',
               created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, created_by TEXT,
               PRIMARY KEY (project, name))"""
        )

    _gpu_small()
    row = wb.create_workbench("nb1", "demo", hardware_profile="gpu-small")
    assert row["hardware_profile"] == "gpu-small"
    assert row["hardware_profile_version"] == 1


def test_the_dashboards_copy_of_the_ddl_declares_the_same_columns():
    """Two hand-maintained CREATE TABLE statements for one table drift silently; whichever runs
    first wins, and the loser's writes fail. Pin them together."""
    ddl = (
        Path(__file__).parents[2]
        / "platform"
        / "services"
        / "dashboard"
        / "backend"
        / "routers"
        / "workbenches.py"
    ).read_text()
    body = ddl.split("CREATE TABLE IF NOT EXISTS workbenches", 1)[1].split(";", 1)[0]
    assert "hardware_profile" in body and "hardware_profile_version" in body
