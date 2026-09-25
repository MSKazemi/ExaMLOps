"""ADR 0032 decision 1/3/4: the model YAML's ``distributed:`` block selects strategy, topology,
elasticity and NCCL config; ``preflight`` refuses what this environment cannot run before anything
is submitted. No torch needed beyond ``find_spec``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.distributed import strategy as st  # noqa: E402


def _pack(tmp_path: Path, block: str | None, name: str = "Demo") -> Path:
    d = tmp_path / "models"
    d.mkdir(exist_ok=True)
    body = f"name: {name}\nmodel_class: X\n"
    if block is not None:
        body += "distributed:\n" + "".join(f"  {line}\n" for line in block.splitlines())
    (d / f"{name.lower()}.yaml").write_text(body)
    return d


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()


def test_yaml_block_selects_strategy_topology_and_elasticity(tmp_path):
    d = _pack(
        tmp_path,
        "strategy: ddp\nnodes: 4\nmin_nodes: 2\ngpus_per_node: 8\nmax_restarts: 3\n"
        "nccl:\n  NCCL_IB_DISABLE: 1\n  NCCL_SOCKET_IFNAME: ib0",
    )
    plan = st.resolve_plan("demo", models_dir=d)  # name match is case-insensitive
    assert plan.strategy == "ddp" and plan.nodes == 4 and plan.min_nodes == 2
    assert plan.nproc_per_node == 8  # defaults to gpus_per_node
    assert plan.elastic and plan.nnodes_spec == "2:4"
    assert plan.nccl == {"NCCL_IB_DISABLE": "1", "NCCL_SOCKET_IFNAME": "ib0"}
    assert plan.source == "yaml"


def test_flags_override_yaml_and_none_means_unset(tmp_path):
    d = _pack(tmp_path, "strategy: ddp\nnodes: 2")
    plan = st.resolve_plan("Demo", models_dir=d, overrides={"strategy": "fsdp", "nodes": None})
    assert plan.strategy == "fsdp" and plan.nodes == 2 and plan.source == "yaml+flags"


def test_no_block_gives_fsdp_default_single_node(tmp_path):
    d = _pack(tmp_path, None)
    plan = st.resolve_plan("Demo", models_dir=d)
    assert plan.strategy == "fsdp" and plan.nodes == 1 and not plan.elastic
    assert plan.source == "defaults"


@pytest.mark.parametrize(
    "block,needle",
    [
        ("min-nodes: 1", "unknown distributed key 'min-nodes'"),
        ("strategy: horovod", "strategy must be one of"),
        ("nodes: 0", "out of range"),
        ("nodes: true", "must be an integer"),
        ("nodes: 1\nmin_nodes: 3", "exceeds nodes"),
        ("entrypoint: 'os; rm -rf /'", "dotted Python module"),
        ("nccl:\n  LD_PRELOAD: /evil.so", "not an NCCL_*"),
        ("nccl:\n  NCCL_IB_KEY: x", "not an NCCL_*"),  # secret-looking names are refused
        ("nccl:\n  NCCL_DEBUG: 'INFO; curl x'", "characters outside"),
    ],
)
def test_malformed_blocks_are_errors_not_silent_defaults(tmp_path, block, needle):
    d = _pack(tmp_path, block)
    with pytest.raises(ValueError, match=needle.replace("*", r"\*")):
        st.resolve_plan("Demo", models_dir=d)


def test_preflight_refuses_zero_without_deepspeed_and_without_entrypoint(monkeypatch):
    real = st._importable
    monkeypatch.setattr(st, "_importable", lambda m: False if m == "deepspeed" else real(m))
    problems = st.preflight(st.DistributedPlan(model="m", strategy="zero"))
    assert any("needs 'deepspeed'" in p for p in problems)
    assert any("needs a model entrypoint" in p for p in problems)
    with pytest.raises(st.StrategyUnavailable, match="cannot run distributed plan"):
        st.require_runnable(st.DistributedPlan(model="m", strategy="zero"))


def test_preflight_refuses_nccl_on_a_cpu_plan_and_an_unimportable_entrypoint():
    plan = st.DistributedPlan(
        model="m", strategy="ddp", nccl={"NCCL_DEBUG": "INFO"}, entrypoint="no_such_mod_xyz.train"
    )
    problems = st.preflight(plan)
    assert any("CPU plan" in p for p in problems)
    assert any("not importable" in p for p in problems)


def test_preflight_passes_the_reference_strategies_when_torch_is_present():
    pytest.importorskip("torch")
    assert st.preflight(st.DistributedPlan(model="m", strategy="fsdp")) == []
    assert st.preflight(st.DistributedPlan(model="m", strategy="ddp")) == []


def test_launch_takes_the_strategy_and_entrypoint_from_yaml(tmp_path, monkeypatch):
    d = _pack(tmp_path, "strategy: ddp\nentrypoint: examlops.distributed.train_ddp")
    monkeypatch.setenv("RAY_MODELS_DIR", str(d))
    from examlops.distributed import launch_distributed

    h = launch_distributed("Demo", nodes=2, strategy=None, node_list=["n07", "n08"])
    assert h.spec.strategy == "ddp"
    cmd = h.spec.torchrun_command()
    assert "train.py" not in cmd  # the placeholder is gone
    i = cmd.index("-m")
    assert cmd[i + 1] == "examlops.distributed.train_ddp" and "--strategy=ddp" in cmd


def test_launch_without_entrypoint_names_the_real_reference_script(tmp_path, monkeypatch):
    monkeypatch.setenv("RAY_MODELS_DIR", str(_pack(tmp_path, None)))
    from examlops.distributed import launch_distributed

    cmd = launch_distributed("Demo", nodes=1, strategy=None).spec.torchrun_command()
    script = next(c for c in cmd if c.endswith("train_ddp.py"))
    assert Path(script).is_file() and "--strategy=fsdp" in cmd


def test_a_malformed_yaml_block_stops_the_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("RAY_MODELS_DIR", str(_pack(tmp_path, "strategy: nope")))
    from examlops.distributed import launch_distributed

    with pytest.raises(ValueError, match="strategy must be one of"):
        launch_distributed("Demo", nodes=1, strategy=None)
