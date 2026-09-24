# tests/unit/test_hardware_profiles.py
"""Hardware Profiles — ADR 0157, spec `design/vision/specs/spec-hardware-profiles.md` §5.

Phase 1 GWTs only (Phase 2/3's GWT-5/GWT-6 need workbench/distributed-launch wiring that does
not exist yet):

GWT-1 — immutable versioning + label move.
GWT-2 — a second ``set`` creates version 2; version 1 stays retrievable; ``active`` moves.
GWT-3 — ``resolve_profile`` status transitions: unchecked / unresolvable / degraded / verified.
GWT-4 — delete semantics: confirmation gate, single-version delete leaves a dangling label with
        a warning (never silently re-pointed), whole-name delete removes every version + label.

Plus: CLI exit codes, ``--json`` output, and the empty-``applicability`` rejection (spec §2:
"``set()`` MUST reject an empty tuple").

Runs against a real sqlite ``platform.db`` (``tests/conftest.py`` gives every test a private
``PLATFORM_DB``), through the real code paths — CLI -> ``examlops.hardware_profiles`` ->
``examlops.data.hardware_profiles`` -> sqlite — no mocks of this repo's own code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli.main import app  # noqa: E402
from examlops.data import init_db  # noqa: E402
from examlops.data.hardware_profiles import (  # noqa: E402
    delete_profile as data_delete_profile,
)
from examlops.data.hardware_profiles import (
    get_profile_version,
    list_profile_versions,
    resolve_label,
)
from examlops.data.hpc import record_node_snapshot, set_cluster_state, upsert_cluster  # noqa: E402
from examlops.hardware_profiles import (  # noqa: E402
    HardwareProfileError,
    create_profile_version,
    get_profile,
    list_names,
    list_versions,
    resolve_profile,
    to_resource_ask,
    to_workload,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()
    yield


def _run(*args: str):
    return runner.invoke(app, list(args))


# ── GWT-1 — immutability + label move ──────────────────────────────────────────────────────


def test_gwt1_set_creates_version_1_and_moves_active_label():
    profile = create_profile_version(
        "gpu-small",
        accelerator_family="nvidia",
        gpu_count=1,
        cpu=4,
        memory_gb=16,
        applicability=("training", "workbench"),
    )
    assert profile.version == 1
    assert profile.name == "gpu-small"
    assert profile.applicability == ("training", "workbench")

    active = get_profile("gpu-small")  # default label="active"
    assert active is not None
    assert active.version == 1
    assert active.gpu_count == 1
    assert active.cpu == 4.0
    assert active.memory_gb == 16.0


def test_gwt1_cli_set_and_show_round_trip():
    result = _run(
        "hardware",
        "profile",
        "set",
        "gpu-small",
        "--accelerator-family",
        "nvidia",
        "--gpu",
        "1",
        "--cpu",
        "4",
        "--memory-gb",
        "16",
        "--applicability",
        "training,workbench",
    )
    assert result.exit_code == 0, result.output

    result = _run("--json", "hardware", "profile", "show", "gpu-small")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["version"] == 1
    assert payload["applicability"] == ["training", "workbench"]
    assert payload["gpu_count"] == 1


# ── GWT-2 — a second set creates version 2; version 1 survives; active moves ────────────────


def test_gwt2_second_set_creates_version_2_and_moves_active():
    v1 = create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=1)
    v2 = create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=2)
    assert v1.version == 1
    assert v2.version == 2

    versions = list_versions("gpu-small")
    assert sorted(v.version for v in versions) == [1, 2]

    # Version 1 is still retrievable in full, unedited.
    v1_again = get_profile("gpu-small", version=1)
    assert v1_again is not None
    assert v1_again.gpu_count == 1

    # 'active' now resolves to version 2.
    active = get_profile("gpu-small")
    assert active is not None
    assert active.version == 2
    assert active.gpu_count == 2


def test_gwt2_cli_version_flag_reaches_an_older_version():
    _run("hardware", "profile", "set", "gpu-small", "--accelerator-family", "nvidia", "--gpu", "1")
    _run("hardware", "profile", "set", "gpu-small", "--accelerator-family", "nvidia", "--gpu", "2")

    result = _run("--json", "hardware", "profile", "show", "gpu-small", "--version", "1")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["gpu_count"] == 1

    result = _run("--json", "hardware", "profile", "list")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1  # one name; shown at its active version
    assert rows[0]["version"] == 2
    assert rows[0]["gpu_count"] == 2


# ── GWT-3 — resolve_profile status transitions ──────────────────────────────────────────────


def test_gwt3_unchecked_when_no_target_cluster():
    create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=1)
    resolution = resolve_profile("gpu-small")
    assert resolution.status == "unchecked"
    assert resolution.reason == "no target cluster given"
    assert resolution.resources.gpus == 1


def test_gwt3_unresolvable_against_an_unknown_cluster():
    create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=1)
    resolution = resolve_profile("gpu-small", target_cluster="does-not-exist")
    assert resolution.status == "unresolvable"
    assert "does-not-exist" in resolution.reason


def test_gwt3_cli_resolve_unknown_cluster_exits_nonzero():
    _run("hardware", "profile", "set", "gpu-small", "--accelerator-family", "nvidia", "--gpu", "1")
    result = _run("hardware", "profile", "resolve", "gpu-small", "--cluster", "does-not-exist")
    assert result.exit_code == 1, result.output
    assert "unresolvable" in result.output


def _register_lxp(gpu_model: str = "A100-80GB", mig_profiles: list[str] | None = None) -> None:
    caps = {"mig_profiles": mig_profiles} if mig_profiles else None
    upsert_cluster("lxp", "slurm", capabilities=caps)
    set_cluster_state("lxp", "ACTIVE")
    record_node_snapshot(
        "lxp",
        "slurm",
        [
            {
                "name": "n1",
                "cpus": 32,
                "memory_mb": 200_000,
                "gpus": 4,
                "gpu_model": gpu_model,
                "state": "idle",
                "partition": "gpu",
            }
        ],
    )


def test_gwt3_verified_when_snapshot_confirms_the_ask():
    _register_lxp(gpu_model="A100-80GB")
    create_profile_version(
        "gpu-verify",
        accelerator_family="nvidia",
        gpu_count=2,
        cpu=8,
        accelerator_model_hint="A100-80GB",
    )
    resolution = resolve_profile("gpu-verify", target_cluster="lxp")
    assert resolution.status == "verified"
    assert resolution.unconfirmed == ()


def test_gwt3_degraded_when_model_hint_unconfirmed():
    """Capacity is satisfiable, but the reported GPU model doesn't match the hint."""
    _register_lxp(gpu_model="A100-80GB")
    create_profile_version(
        "gpu-degraded",
        accelerator_family="nvidia",
        gpu_count=2,
        cpu=8,
        accelerator_model_hint="H100-80GB",  # not what the snapshot reports
    )
    resolution = resolve_profile("gpu-degraded", target_cluster="lxp")
    assert resolution.status == "degraded"
    assert resolution.unconfirmed == ("accelerator_model_hint",)
    assert "accelerator_model_hint" in resolution.reason


def test_gwt3_degraded_vs_unresolvable_is_a_real_distinction():
    """The exact distinction the spec calls out: degraded still satisfies the coarse ask."""
    _register_lxp(gpu_model="A100-80GB")
    # Satisfiable coarse ask (2 <= 4 GPUs) but an unconfirmed model hint -> degraded, not
    # unresolvable — resolution PROCEEDS on the coarse ask.
    create_profile_version(
        "coarse-ok", accelerator_family="nvidia", gpu_count=2, accelerator_model_hint="H100"
    )
    degraded = resolve_profile("coarse-ok", target_cluster="lxp")
    assert degraded.status == "degraded"

    # Unsatisfiable coarse ask (10 > 4 GPUs) -> unresolvable, regardless of the model hint.
    create_profile_version(
        "coarse-bad", accelerator_family="nvidia", gpu_count=10, accelerator_model_hint="H100"
    )
    unresolvable = resolve_profile("coarse-bad", target_cluster="lxp")
    assert unresolvable.status == "unresolvable"


def test_gwt3_degraded_for_unconfirmed_mig_profile():
    _register_lxp(gpu_model="A100-80GB", mig_profiles=["1g.5gb"])
    create_profile_version(
        "gpu-mig", accelerator_family="nvidia", gpu_count=1, mig_profile="3g.20gb"
    )
    resolution = resolve_profile("gpu-mig", target_cluster="lxp")
    assert resolution.status == "degraded"
    assert "mig_profile" in resolution.unconfirmed


def test_gwt3_verified_when_mig_profile_is_reported():
    _register_lxp(gpu_model="A100-80GB", mig_profiles=["1g.5gb", "3g.20gb"])
    create_profile_version(
        "gpu-mig-ok", accelerator_family="nvidia", gpu_count=1, mig_profile="3g.20gb"
    )
    resolution = resolve_profile("gpu-mig-ok", target_cluster="lxp")
    assert resolution.status == "verified"


def test_gwt3_resolve_raises_for_a_missing_profile():
    with pytest.raises(HardwareProfileError):
        resolve_profile("does-not-exist")


def test_gwt3_cli_json_resolve_reports_full_document():
    _register_lxp()
    create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=1, cpu=4)
    result = _run("--json", "hardware", "profile", "resolve", "gpu-small", "--cluster", "lxp")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "verified"
    assert payload["resources"] == {"gpus": 1, "cpus": 4, "memory_gb": 0.0, "nodes": 1}


# ── Adapters (§2.2) ──────────────────────────────────────────────────────────────────────────


def test_to_resource_ask_and_to_workload_adapters():
    create_profile_version(
        "gpu-small", accelerator_family="amd", gpu_count=2, gpu_fraction=0.5, cpu=8, nodes=2
    )
    resolution = resolve_profile("gpu-small")

    ask = to_resource_ask(resolution)
    assert ask.gpus == 2
    assert ask.cpus == 8
    assert ask.nodes == 2

    workload = to_workload(resolution, "train-job")
    assert workload.name == "train-job"
    assert workload.accelerator == "amd"
    assert workload.fraction == 0.5


def test_to_workload_raises_if_the_resolved_version_was_deleted():
    profile = create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=1)
    resolution = resolve_profile("gpu-small")
    data_delete_profile("gpu-small", profile.version)
    with pytest.raises(HardwareProfileError):
        to_workload(resolution, "train-job")


# ── GWT-4 — delete semantics ─────────────────────────────────────────────────────────────────


def test_gwt4_delete_without_yes_prompts_for_confirmation():
    _run("hardware", "profile", "set", "gpu-small", "--accelerator-family", "nvidia", "--gpu", "1")
    result = runner.invoke(app, ["hardware", "profile", "delete", "gpu-small"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "Cancelled" in result.output
    # Nothing was removed.
    assert get_profile_version("gpu-small", 1) is not None


def test_gwt4_delete_with_yes_skips_the_prompt():
    _run("hardware", "profile", "set", "gpu-small", "--accelerator-family", "nvidia", "--gpu", "1")
    result = _run("hardware", "profile", "delete", "gpu-small", "--yes")
    assert result.exit_code == 0, result.output
    assert get_profile_version("gpu-small", 1) is None


def test_gwt4_delete_one_version_leaves_the_other_and_moves_nothing():
    create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=1)
    create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=2)
    removed = data_delete_profile("gpu-small", 1)
    assert removed == 1
    assert get_profile_version("gpu-small", 1) is None
    remaining = list_profile_versions("gpu-small")
    assert [r["version"] for r in remaining] == [2]
    # 'active' (never touched by this delete) still points at 2.
    assert resolve_label("gpu-small", "active")["version"] == 2


def test_gwt4_deleting_the_active_versions_target_leaves_it_dangling_with_a_warning():
    profile = create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=1)
    assert resolve_label("gpu-small", "active")["version"] == profile.version

    result = _run("hardware", "profile", "delete", "gpu-small", "--version", "1", "--yes")
    assert result.exit_code == 0, result.output
    assert "dangling" in result.output
    assert "never silently re-pointed" in result.output

    # `resolve_label` joins through to the (now-gone) version content, so it honestly reports
    # nothing rather than fabricating a result — this is itself the "dangling" symptom.
    assert resolve_label("gpu-small", "active") is None
    assert get_profile("gpu-small") is None  # the label now points at nothing real

    # The label POINTER itself (the (name,label)->version row) was never re-pointed to some
    # other version — it still names the version that is now gone. There is no public helper
    # for reading the raw pointer (only the joined-through `resolve_label`), so check the row
    # directly: this is exactly what "dangling, never silently re-pointed" (GWT-4) means.
    from examlops.data import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT version FROM hardware_profile_labels WHERE name=? AND label='active'",
            ("gpu-small",),
        ).fetchone()
    assert row is not None
    assert row["version"] == 1  # unchanged — dangling, not silently re-pointed elsewhere


def test_gwt4_delete_whole_name_removes_every_version_and_label():
    create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=1)
    create_profile_version("gpu-small", accelerator_family="nvidia", gpu_count=2)

    result = _run("hardware", "profile", "delete", "gpu-small", "--yes")
    assert result.exit_code == 0, result.output
    assert "all versions" in result.output.lower()

    assert list_profile_versions("gpu-small") == []
    assert resolve_label("gpu-small", "active") is None
    assert "gpu-small" not in list_names()


def test_gwt4_delete_a_nonexistent_profile_exits_nonzero():
    result = _run("hardware", "profile", "delete", "ghost", "--yes")
    assert result.exit_code == 1, result.output


def test_gwt4_delete_a_nonexistent_version_exits_nonzero():
    _run("hardware", "profile", "set", "gpu-small", "--accelerator-family", "nvidia", "--gpu", "1")
    result = _run("hardware", "profile", "delete", "gpu-small", "--version", "99", "--yes")
    assert result.exit_code == 1, result.output


# ── The empty-applicability rejection (spec §2) ──────────────────────────────────────────────


def test_empty_applicability_tuple_is_rejected():
    with pytest.raises(HardwareProfileError, match="applicability"):
        create_profile_version("gpu-small", accelerator_family="nvidia", applicability=())


def test_cli_empty_applicability_is_rejected():
    result = _run(
        "hardware",
        "profile",
        "set",
        "gpu-small",
        "--accelerator-family",
        "nvidia",
        "--applicability",
        "",
    )
    assert result.exit_code == 1, result.output
    assert "applicability" in result.output.lower()
    assert "gpu-small" not in list_names()  # nothing was written


def test_cli_bad_accelerator_family_is_rejected():
    result = _run("hardware", "profile", "set", "gpu-small", "--accelerator-family", "bogus-vendor")
    assert result.exit_code == 1, result.output


def test_cli_bad_applicability_value_is_rejected():
    result = _run(
        "hardware",
        "profile",
        "set",
        "gpu-small",
        "--accelerator-family",
        "nvidia",
        "--applicability",
        "not-a-real-scope",
    )
    assert result.exit_code == 1, result.output


# ── list_names / list_versions ───────────────────────────────────────────────────────────────


def test_list_names_and_applicability_filter():
    create_profile_version("wb-only", accelerator_family="cpu", applicability=("workbench",))
    create_profile_version(
        "train-only", accelerator_family="nvidia", gpu_count=1, applicability=("training",)
    )
    assert set(list_names()) == {"wb-only", "train-only"}

    result = _run("--json", "hardware", "profile", "list", "--applicability", "training")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [r["name"] for r in rows] == ["train-only"]
