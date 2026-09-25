# tests/unit/test_hardware_profiles_pipeline.py
"""Hardware Profiles Phase 3 — training & serving integration (ADR 0157, spec §4/§5).

GWT-6 is the load-bearing one: ``exa pipeline distributed launch <model> --hardware-profile
gpu-small`` must build the **same** ``torchrun`` invocation as ``--nodes 1 --gpus-per-node 1``
would, proving the profile is sugar over the existing seam and not a second execution path.

Also covered:

* ``exa pipeline run --hardware-profile`` feeds the resolved ``ResourceAsk`` into the very
  ``--cluster auto`` placement call ``--gpus`` has always fed;
* ``--gpus`` alongside a profile overrides **only** ``gpu_count`` and says so (never silent);
* both commands refuse a profile whose ``applicability`` excludes ``training``, naming the
  profile's actual applicability;
* the serving side: ``resources.hardware_profile`` round-trips through ``load_model_yaml`` and
  resolves into ``ray_actor_options``;
* the regression path: a model YAML with **no** ``resources:`` block behaves exactly as today.

Runs against a real sqlite ``platform.db`` through the real code paths — no mocks of this
repo's own resolution logic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from typer.testing import CliRunner  # noqa: E402

from examlops import hpc_placement  # noqa: E402
from examlops.cli import _output  # noqa: E402
from examlops.cli.commands import distributed_cmd  # noqa: E402
from examlops.cli.commands import pipeline as pipeline_cmd
from examlops.cli.main import app  # noqa: E402
from examlops.data import init_db  # noqa: E402
from examlops.hardware_profiles import (  # noqa: E402
    HardwareProfileError,
    create_profile_version,
    resolve_for,
    to_ray_actor_options,
)
from examlops.hardware_profiles_yaml import (  # noqa: E402
    model_ray_actor_options,
    validate_resources_block,
    yaml_block,
)

runner = CliRunner()

_YAML_HEAD = """\
name: {name}
model_class: {name}
config_class: {lower}_config.Config
task_type: regression
"""


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    # `_hardware_profile_ask` exports EXAMLOPS_HARDWARE_PROFILE in-process (Phase 4); setenv
    # first so teardown restores it to absent instead of leaking it into the next test.
    monkeypatch.setenv("EXAMLOPS_HARDWARE_PROFILE", "")
    monkeypatch.delenv("EXAMLOPS_HARDWARE_PROFILE")
    init_db()
    yield


def _gpu_small() -> None:
    create_profile_version(
        "gpu-small",
        accelerator_family="nvidia",
        gpu_count=1,
        cpu=4,
        memory_gb=16,
        nodes=1,
        applicability=("training", "workbench"),
    )


def _write_model_yaml(directory: Path, name: str, *, resources: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name.lower()}.yaml"
    path.write_text(_YAML_HEAD.format(name=name, lower=name.lower()) + resources)
    return path


# ── GWT-6 — the profile is sugar over the existing seam ────────────────────────────────────


def test_gwt6_profile_launch_builds_the_same_torchrun_as_explicit_flags():
    """A 1-node/1-GPU profile and ``--nodes 1 --gpus-per-node 1`` must be indistinguishable."""
    _gpu_small()

    explicit = runner.invoke(
        app,
        [
            "--json",
            "pipeline",
            "distributed",
            "launch",
            "JPCP",
            "--nodes",
            "1",
            "--gpus-per-node",
            "1",
        ],
    )
    assert explicit.exit_code == 0, explicit.output
    by_flags = json.loads(explicit.output)

    profiled = runner.invoke(
        app,
        ["--json", "pipeline", "distributed", "launch", "JPCP", "--hardware-profile", "gpu-small"],
    )
    assert profiled.exit_code == 0, profiled.output
    by_profile = json.loads(profiled.output)

    assert by_profile["torchrun"] == by_flags["torchrun"]
    assert "--nnodes=1" in by_profile["torchrun"]
    assert "--nproc_per_node=1" in by_profile["torchrun"]
    assert by_profile["nodes"] == by_flags["nodes"] == 1
    assert by_profile["run_id"] == by_flags["run_id"]


def test_profile_topology_reaches_launch_and_strategy_is_untouched():
    """A 2×4 profile populates both topology flags; ``--strategy`` is not part of the shape."""
    create_profile_version(
        "train-big",
        accelerator_family="nvidia",
        gpu_count=4,
        cpu=32,
        nodes=2,
        applicability=("training",),
    )
    result = runner.invoke(
        app,
        [
            "--json",
            "pipeline",
            "distributed",
            "launch",
            "JPCP",
            "--hardware-profile",
            "train-big",
            "--strategy",
            "zero",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["nodes"] == 2
    assert "--nnodes=2" in payload["torchrun"]
    assert "--nproc_per_node=4" in payload["torchrun"]
    assert payload["strategy"] == "zero"


def test_explicit_topology_flags_win_over_the_profile():
    """A profile is a default, not an override — and the partial override is reported."""
    create_profile_version(
        "train-big",
        accelerator_family="nvidia",
        gpu_count=4,
        nodes=2,
        applicability=("training",),
    )
    nodes, gpus = distributed_cmd._topology("train-big", 3, None)
    assert (nodes, gpus) == (3, 4)


def test_topology_without_a_profile_is_unchanged():
    assert distributed_cmd._topology(None, None, None) == (1, 1)
    assert distributed_cmd._topology(None, 4, 8) == (4, 8)


# ── --gpus is a partial override, never silent ─────────────────────────────────────────────


def test_gpus_overrides_only_the_profiles_gpu_count_and_says_so(monkeypatch):
    create_profile_version(
        "train-big",
        accelerator_family="nvidia",
        gpu_count=4,
        cpu=32,
        nodes=2,
        applicability=("training",),
    )
    warnings: list[str] = []
    monkeypatch.setattr(_output, "warning", warnings.append)

    ask = pipeline_cmd._hardware_profile_ask("train-big", "auto", 1)

    assert ask.gpus == 1, "--gpus must override the profile's gpu_count"
    assert ask.cpus == 32 and ask.nodes == 2, "cpu/nodes must still come from the profile"
    assert warnings, "a partial override must be reported, never silent"
    assert "overrides only the gpu_count" in warnings[0]
    assert "train-big" in warnings[0]


def test_profile_ask_without_gpus_is_the_whole_profile(monkeypatch):
    _gpu_small()
    warnings: list[str] = []
    monkeypatch.setattr(_output, "warning", warnings.append)

    ask = pipeline_cmd._hardware_profile_ask("gpu-small", None, 0)

    assert (ask.gpus, ask.cpus, ask.nodes) == (1, 4, 1)
    assert warnings == [], "nothing was overridden, so nothing should be warned about"


def test_profile_ask_feeds_the_same_auto_placement_call_as_gpus(monkeypatch):
    """The resolved ask reaches ``choose_cluster`` — the very call ``--gpus`` feeds today."""
    create_profile_version(
        "train-big",
        accelerator_family="nvidia",
        gpu_count=4,
        cpu=32,
        nodes=2,
        applicability=("training",),
    )
    seen: dict[str, object] = {}

    def _fake_choose(ask, clusters, score_fn):
        seen["ask"] = ask
        return hpc_placement.PlacementResult(cluster=None, reason="none in this test")

    monkeypatch.setattr(hpc_placement, "choose_cluster", _fake_choose)
    monkeypatch.setattr(pipeline_cmd, "_run_generator", lambda args: None)

    result = runner.invoke(
        app,
        [
            "pipeline",
            "run",
            "--model",
            "JPCP",
            "--dummy",
            "--cluster",
            "auto",
            "--hardware-profile",
            "train-big",
        ],
    )
    assert result.exit_code == 1  # placement found nothing — but the ask is what we assert on
    ask = seen["ask"]
    assert isinstance(ask, hpc_placement.ResourceAsk)
    assert (ask.gpus, ask.cpus, ask.nodes) == (4, 32, 2)


def test_gpus_alone_still_feeds_placement_unchanged(monkeypatch):
    """The pre-Phase-3 path: no profile, ``--gpus`` builds the ask exactly as before."""
    seen: dict[str, object] = {}

    def _fake_choose(ask, clusters, score_fn):
        seen["ask"] = ask
        return hpc_placement.PlacementResult(cluster=None, reason="none in this test")

    monkeypatch.setattr(hpc_placement, "choose_cluster", _fake_choose)
    monkeypatch.setattr(pipeline_cmd, "_run_generator", lambda args: None)

    runner.invoke(
        app, ["pipeline", "run", "--model", "JPCP", "--dummy", "--cluster", "auto", "--gpus", "2"]
    )
    assert seen["ask"] == hpc_placement.ResourceAsk(gpus=2, cpus=0, nodes=1)


# ── applicability is a refusal, not a warning ──────────────────────────────────────────────


def test_pipeline_run_refuses_a_profile_that_is_not_applicable_to_training():
    create_profile_version(
        "serve-only", accelerator_family="nvidia", gpu_count=1, applicability=("serving",)
    )
    result = runner.invoke(
        app, ["pipeline", "run", "--model", "JPCP", "--dummy", "--hardware-profile", "serve-only"]
    )
    assert result.exit_code == 1, result.output
    text = result.output + (result.stderr or "")
    assert "not applicable to 'training'" in text
    assert "['serving']" in text, "the error must name the profile's ACTUAL applicability"


def test_distributed_launch_refuses_a_profile_that_is_not_applicable_to_training():
    create_profile_version(
        "serve-only", accelerator_family="nvidia", gpu_count=1, applicability=("serving",)
    )
    result = runner.invoke(
        app,
        ["pipeline", "distributed", "launch", "JPCP", "--hardware-profile", "serve-only"],
    )
    assert result.exit_code == 2, result.output
    text = result.output + (result.stderr or "")
    assert "not applicable to 'training'" in text
    assert "['serving']" in text


def test_an_any_profile_is_applicable_to_training():
    create_profile_version("anywhere", accelerator_family="cpu", cpu=2, applicability=("any",))
    profile, resolution = resolve_for("anywhere", "training")
    assert profile.name == "anywhere"
    assert resolution.status == "unchecked"


def test_a_missing_profile_is_refused_by_name():
    with pytest.raises(HardwareProfileError, match="not found"):
        resolve_for("nope", "training")


# ── serving: resources.hardware_profile ────────────────────────────────────────────────────


def test_resources_block_round_trips_through_the_loader(tmp_path):
    from pipelines.model_loader import load_model_yaml

    path = _write_model_yaml(
        tmp_path / "models", "JPCP", resources="\nresources:\n  hardware_profile: serve-small\n"
    )
    cfg = load_model_yaml(path)
    assert cfg.resources == {"hardware_profile": "serve-small"}
    assert yaml_block("JPCP", tmp_path / "models") == {"hardware_profile": "serve-small"}


def test_resources_block_resolves_into_ray_actor_options(tmp_path):
    create_profile_version(
        "serve-small",
        accelerator_family="nvidia",
        gpu_count=1,
        gpu_fraction=0.5,
        cpu=2,
        applicability=("serving",),
    )
    _write_model_yaml(
        tmp_path / "models", "JPCP", resources="\nresources:\n  hardware_profile: serve-small\n"
    )
    options = model_ray_actor_options("JPCP", models_dir=tmp_path / "models")
    assert options == {"num_gpus": 0.5, "num_cpus": 2.0}


def test_serving_refuses_a_profile_that_is_not_applicable_to_serving(tmp_path):
    create_profile_version(
        "train-only", accelerator_family="nvidia", gpu_count=1, applicability=("training",)
    )
    _write_model_yaml(
        tmp_path / "models", "JPCP", resources="\nresources:\n  hardware_profile: train-only\n"
    )
    with pytest.raises(HardwareProfileError, match="not applicable to 'serving'"):
        model_ray_actor_options("JPCP", models_dir=tmp_path / "models")


def test_a_cpu_only_profile_pins_no_gpu():
    create_profile_version("cpu-box", accelerator_family="cpu", cpu=4, applicability=("serving",))
    _profile, resolution = resolve_for("cpu-box", "serving")
    assert to_ray_actor_options(resolution) == {"num_cpus": 4.0}


@pytest.mark.parametrize(
    "block,expected_fragment",
    [
        ({"hardware-profile": "x"}, "unknown resources key"),
        ({"hardware_profile": 7}, "wrong type"),
        ({"hardware_profile": "  "}, "must not be empty"),
        ("gpu-small", "must be a mapping"),
    ],
)
def test_a_malformed_resources_block_is_an_error(block, expected_fragment):
    errors = validate_resources_block(block)
    assert errors and expected_fragment in errors[0]


def test_an_absent_resources_block_is_valid():
    assert validate_resources_block(None) == []
    assert validate_resources_block({}) == []


# ── regression: no profile anywhere ⇒ exactly today's behaviour ────────────────────────────


def test_a_model_yaml_with_no_resources_block_behaves_as_today(tmp_path):
    from pipelines.model_loader import load_model_yaml

    path = _write_model_yaml(tmp_path / "models", "JPCP")
    assert load_model_yaml(path).resources == {}
    assert yaml_block("JPCP", tmp_path / "models") is None
    assert model_ray_actor_options("JPCP", models_dir=tmp_path / "models") is None


def test_the_model_server_keeps_its_default_actor_options_without_a_profile(tmp_path, monkeypatch):
    from serving.ray_serving.app import _server_actor_options

    monkeypatch.delenv("RAY_MODELS_DIR", raising=False)
    assert _server_actor_options() == {"num_cpus": 1}

    _write_model_yaml(tmp_path / "models", "JPCP")
    monkeypatch.setenv("RAY_MODELS_DIR", str(tmp_path / "models"))
    assert _server_actor_options() == {"num_cpus": 1}


def test_the_model_server_is_sized_by_the_most_demanding_profile(tmp_path, monkeypatch):
    create_profile_version(
        "serve-small",
        accelerator_family="nvidia",
        gpu_count=1,
        gpu_fraction=0.5,
        cpu=2,
        applicability=("serving",),
    )
    create_profile_version(
        "serve-big", accelerator_family="nvidia", gpu_count=2, cpu=8, applicability=("any",)
    )
    models = tmp_path / "models"
    _write_model_yaml(models, "JPCP", resources="\nresources:\n  hardware_profile: serve-small\n")
    _write_model_yaml(models, "MACK", resources="\nresources:\n  hardware_profile: serve-big\n")
    monkeypatch.setenv("RAY_MODELS_DIR", str(models))

    from serving.ray_serving.app import _server_actor_options

    assert _server_actor_options() == {"num_cpus": 8.0, "num_gpus": 2.0}


def test_an_unresolvable_profile_never_stops_the_server_starting(tmp_path, monkeypatch):
    """No profile row at all — the deployment must still come up on today's defaults."""
    models = tmp_path / "models"
    _write_model_yaml(models, "JPCP", resources="\nresources:\n  hardware_profile: absent\n")
    monkeypatch.setenv("RAY_MODELS_DIR", str(models))

    from serving.ray_serving.app import _server_actor_options

    assert _server_actor_options() == {"num_cpus": 1}


def test_pipeline_run_without_a_profile_never_touches_the_registry(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(pipeline_cmd, "_run_generator", calls.append)

    def _explode(*a, **kw):  # pragma: no cover - fails the test if reached
        raise AssertionError("resolve_for must not be called without --hardware-profile")

    monkeypatch.setattr("examlops.hardware_profiles.resolve_for", _explode)

    result = runner.invoke(app, ["pipeline", "run", "--model", "JPCP", "--dummy"])
    assert result.exit_code == 0, result.output
    assert calls == [["--dummy", "--model", "JPCP"]]
