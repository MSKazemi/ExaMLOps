"""ADR 0080 — the registry YAML as the IR: ``placement:``, YAML-as-IR input, ``show --ir``,
``compile -o model.yaml``, ``Resources`` *is* ``ResourceAsk``, and ``run --ir`` on Slurm/Flux.

Everything below runs the production code paths; only the generator subprocess, the placement
resolver and the scheduler adapter are replaced by recording fakes, and every test asserts the
outcome (files written, what was staged, what exit code and which document) rather than argv.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "platform" / "cli" / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops.cli.main import app  # noqa: E402
from examlops.hpc_placement import ResourceAsk  # noqa: E402
from examlops.pipeline_dsl import (  # noqa: E402
    IRError,
    NotLowerableError,
    NotRepresentableError,
    Resources,
    dataset,
    evaluate,
    ir_from_model_yaml,
    lower_training,
    pipeline,
    render_pipeline_source,
    train,
)
from examlops.pipeline_dsl.ir import build_ir, new_node  # noqa: E402
from examlops.pipeline_dsl.loader import MAX_IR_BYTES, load_ir, load_pipeline_file  # noqa: E402
from examlops.pipeline_dsl.placement import (  # noqa: E402
    placement_ask,
    validate_placement_block,
)

runner = CliRunner()
EXAMPLE = str(_ROOT / "examples" / "pipeline-as-code" / "jpcp_flow.py")
PACK_MODELS = _ROOT / "usecases" / "reference" / "models"
JPCP_YAML = PACK_MODELS / "jpcp.yaml"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.delenv("EXAMLOPS_HPC_SCHEDULER", raising=False)
    monkeypatch.delenv("EXAMLOPS_SLURM_MODE", raising=False)
    monkeypatch.delenv("RAY_MODELS_DIR", raising=False)
    monkeypatch.delenv("MODELS_YAML_DIR", raising=False)
    monkeypatch.setattr("examlops.policy.POLICY_YAML", tmp_path / "policy.yaml")
    from examlops import platform_db

    platform_db.init_db()
    return tmp_path


def _pack_with(tmp_path: Path, monkeypatch, **extra) -> Path:
    """A one-model pack whose jpcp.yaml carries ``extra`` top-level sections."""
    raw = yaml.safe_load(JPCP_YAML.read_text())
    raw.update(extra)
    models = tmp_path / "pack" / "models"
    models.mkdir(parents=True)
    (models / "jpcp.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    monkeypatch.setenv("RAY_MODELS_DIR", str(models))
    return models / "jpcp.yaml"


# ── Resources is ResourceAsk (clause 4) ──────────────────────────────────────────────────────
def test_resources_is_the_placement_class_not_a_mirror():
    assert Resources is ResourceAsk
    assert Resources(gpus=2, cpus=8).as_dict() == {"gpus": 2, "cpus": 8, "nodes": 1}


def test_a_step_ask_reaches_the_ir_as_the_placement_ask():
    @pipeline(name="P")
    def flow():
        evaluate(
            train(dataset("D"), config_class="a.B", task_type="r", resources=ResourceAsk(gpus=4))
        )

    node = next(n for n in flow.compile()["nodes"] if n["kind"] == "train")
    assert placement_ask(node["resources"]) == ResourceAsk(gpus=4, cpus=0, nodes=1)


# ── the placement: section ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "block, fragment",
    [
        ("x", "must be a mapping"),
        ({"zone": "a"}, "unknown placement key"),
        ({"gpus": True}, "must be an integer"),
        ({"gpus": "2"}, "must be an integer"),
        ({"gpus": -1}, "must be >= 0"),
        ({"nodes": 0}, "must be >= 1"),
        ({"gpus": 10**9}, "sanity cap"),
        ({"cluster": ""}, "non-empty string"),
        ({"cluster": 3}, "non-empty string"),
    ],
)
def test_placement_validation_fails_closed(block, fragment):
    errors = validate_placement_block(block)
    assert errors and any(fragment in e for e in errors), errors


def test_valid_and_absent_placement():
    assert validate_placement_block(None) == []
    assert validate_placement_block({"cluster": "auto", "gpus": 2, "cpus": 8, "nodes": 1}) == []
    assert placement_ask({"cluster": "auto"}) is None


def test_lowering_refuses_a_train_ask_the_yaml_cannot_hold():
    nodes = [
        new_node("d", "dataset", {"name": "D"}),
        new_node("t", "train", {"config_class": "a.B", "task_type": "r"}, {"nodes": 0}),
        new_node("e", "evaluate", {}),
    ]
    edges = [
        {"from": "d", "output": "data", "to": "t", "input": "datasets"},
        {"from": "t", "output": "model", "to": "e", "input": "model"},
    ]
    doc = build_ir(name="P", kind="training", nodes=nodes, edges=edges)  # the IR allows 0 …
    with pytest.raises(NotLowerableError, match="placement"):
        lower_training(doc)  # … the YAML does not, so lowering refuses instead of dropping it


def test_lowered_placement_is_read_by_the_generator_loader(tmp_path):
    from pipelines.model_loader import load_model_yaml

    @pipeline(name="P", cluster="gpu-a")
    def flow():
        evaluate(
            train(dataset("D"), config_class="a.B", task_type="r", resources=Resources(gpus=2))
        )

    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump(lower_training(flow.compile()).model_yaml))
    cfg = load_model_yaml(f)
    assert cfg.placement == {"cluster": "gpu-a", "gpus": 2, "cpus": 0, "nodes": 1}


def test_shipped_pack_placement_sections_are_valid():
    for path in sorted(PACK_MODELS.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text()) or {}
        assert validate_placement_block(raw.get("placement")) == [], path.name


# ── round trip with placement ────────────────────────────────────────────────────────────────
def test_placement_round_trips_yaml_to_dsl_to_yaml(tmp_path):
    raw = yaml.safe_load(JPCP_YAML.read_text())
    raw["placement"] = {"cluster": "auto", "gpus": 2}
    doc = ir_from_model_yaml(raw)
    assert doc["target"] == {"cluster": "auto"}
    train_node = next(n for n in doc["nodes"] if n["kind"] == "train")
    assert train_node["resources"] == {"gpus": 2}

    src = tmp_path / "flow.py"
    src.write_text(render_pipeline_source(doc))
    pdef, _ = load_pipeline_file(str(src))
    again = pdef.compile()
    assert again["target"] == doc["target"]
    assert lower_training(again).model_yaml == json.loads(json.dumps(raw))


@pytest.mark.parametrize("bad", [{}, {"gpus": -2}, {"what": 1}])
def test_decompile_refuses_an_empty_or_invalid_placement(bad):
    raw = yaml.safe_load(JPCP_YAML.read_text())
    raw["placement"] = bad
    with pytest.raises(NotRepresentableError, match="placement"):
        ir_from_model_yaml(raw)


# ── YAML is accepted wherever an IR is ───────────────────────────────────────────────────────
def test_load_ir_reads_a_registry_yaml_as_the_same_graph():
    doc = load_ir(str(JPCP_YAML))
    assert doc == ir_from_model_yaml(yaml.safe_load(JPCP_YAML.read_text()))


def test_load_ir_rejects_bad_yaml_and_oversize_files(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [unclosed\n")
    with pytest.raises(IRError, match="not valid YAML"):
        load_ir(str(bad))
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (MAX_IR_BYTES + 1))
    with pytest.raises(IRError, match="size cap"):
        load_ir(str(big))


def test_explain_accepts_a_registry_yaml(env):
    res = runner.invoke(app, ["--json", "pipeline", "explain", str(JPCP_YAML)])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["name"] == "JPCP" and out["runnable"] is True
    assert [r["kind"] for r in out["plan"]][-1] == "promote"


# ── compile -o model.yaml ────────────────────────────────────────────────────────────────────
def test_compile_out_yaml_writes_the_registry_yaml_not_json(env):
    target = env / "jpcp.yaml"
    res = runner.invoke(app, ["pipeline", "compile", EXAMPLE, "-o", str(target)])
    assert res.exit_code == 0, res.output
    written = yaml.safe_load(target.read_text())
    assert written["name"] == "JPCP" and "nodes" not in written
    # it is the mapping the shipped YAML is (the example flow leaves `enabled` at its default)
    shipped = yaml.safe_load(JPCP_YAML.read_text())
    shipped.pop("enabled", None)
    assert written == shipped


def test_compile_out_yaml_json_mode_names_the_format(env):
    target = env / "jpcp.yml"
    res = runner.invoke(app, ["--json", "pipeline", "compile", EXAMPLE, "-o", str(target)])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["ir_format"] == "yaml" and out["yaml_file"] == str(target) and target.is_file()


def test_compile_out_yaml_refuses_an_unlowerable_pipeline_and_writes_nothing(env, tmp_path):
    f = tmp_path / "h.py"
    f.write_text(
        "from examlops.sdk import pipeline, dataset, hpo\n"
        "@pipeline(name='H')\ndef h():\n    hpo(dataset('D'), config_class='a.B')\n"
    )
    target = tmp_path / "h.yaml"
    res = runner.invoke(app, ["pipeline", "compile", str(f), "-o", str(target)])
    assert res.exit_code == 1 and "not lowerable" in res.output.lower()
    assert not target.exists()


# ── exa pipeline show NAME [--ir] ────────────────────────────────────────────────────────────
def test_show_prints_the_registry_yaml(env):
    res = runner.invoke(app, ["pipeline", "show", "JPCP"])
    assert res.exit_code == 0, res.output
    assert "config_class" in res.output and "sha256:" in res.output


def test_show_ir_is_the_compiled_graph_and_one_json_document(env):
    res = runner.invoke(app, ["--json", "pipeline", "show", "jpcp", "--ir"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    expected = ir_from_model_yaml(yaml.safe_load(JPCP_YAML.read_text()))
    assert out["ir"] == expected and out["content_hash"] == expected["content_hash"]
    assert out["file"] == "jpcp.yaml" and out["representable"] is True


def test_show_unknown_model_exits_1(env):
    res = runner.invoke(app, ["pipeline", "show", "NoSuchModel"])
    assert res.exit_code == 1 and "No model named" in res.output


def test_show_ir_refuses_a_yaml_with_no_ir_form_but_plain_show_still_works(
    env, tmp_path, monkeypatch
):
    _pack_with(tmp_path, monkeypatch, unknown_section={"x": 1})
    res = runner.invoke(app, ["pipeline", "show", "JPCP", "--ir"])
    assert res.exit_code == 1 and "no pipeline-IR form" in res.output
    res = runner.invoke(app, ["--json", "pipeline", "show", "JPCP"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["representable"] is False and "unknown_section" in out["not_representable_reason"]


# ── run: the placement defaults and --cluster from the IR target ──────────────────────────────
def _capture_run(argv, scope: dict | None = None):
    """Run ``argv`` with placement + generator stubbed; ``seen`` = the placement ask it received.

    The fake mirrors ``_resolve_cluster_env``'s real signature (keyword-only residency scope
    included) so a signature drift fails here instead of passing silently; pass ``scope`` to also
    capture the model/dataset/registry the residency gate would be consulted with.
    """
    seen: dict = {}

    def fake_resolve(cluster, gpus, ask=None, *, model=None, dataset=None, registry=None):
        seen["cluster"], seen["gpus"] = cluster, gpus
        if scope is not None:
            scope.update(model=model, dataset=dataset, registry=registry)
        return True

    gen: list[list[str]] = []
    with (
        patch("examlops.cli.commands.pipeline._resolve_cluster_env", side_effect=fake_resolve),
        patch("examlops.cli.commands.pipeline._run_generator", side_effect=gen.append),
    ):
        res = runner.invoke(app, argv)
    return res, seen, gen


def test_run_ir_target_cluster_and_gpus_reach_placement(env, tmp_path):
    f = tmp_path / "flow.py"
    f.write_text(
        "from examlops.sdk import pipeline, dataset, train, evaluate, Resources\n"
        "@pipeline(name='JPCP', cluster='auto')\n"
        "def jpcp():\n"
        "    evaluate(train(dataset('PM100Dataset'), config_class='jpcp_config.JPCPConfiguration',"
        " task_type='regression', resources=Resources(gpus=3)))\n"
    )
    ir = tmp_path / "flow.json"
    assert runner.invoke(app, ["pipeline", "compile", str(f), "-o", str(ir)]).exit_code == 0
    res, seen, gen = _capture_run(["pipeline", "run", "--ir", str(ir), "--dummy"])
    assert res.exit_code == 0, res.output
    assert seen == {"cluster": "auto", "gpus": 3} and len(gen) == 1


def test_run_ir_accepts_a_registry_yaml_with_placement(env, tmp_path, monkeypatch):
    y = _pack_with(tmp_path, monkeypatch, placement={"cluster": "gpu-a", "gpus": 2})
    res, seen, gen = _capture_run(["pipeline", "run", "--ir", str(y), "--dummy"])
    assert res.exit_code == 0, res.output
    assert seen == {"cluster": "gpu-a", "gpus": 2}
    assert "--model-yaml" in gen[0]


def test_run_model_uses_the_yaml_placement_as_default(env, tmp_path, monkeypatch):
    _pack_with(tmp_path, monkeypatch, placement={"cluster": "auto", "gpus": 1})
    scope: dict = {}
    res, seen, gen = _capture_run(["pipeline", "run", "--model", "JPCP", "--dummy"], scope)
    assert res.exit_code == 0, res.output
    assert seen == {"cluster": "auto", "gpus": 1} and gen == [["--dummy", "--model", "JPCP"]]
    # The data-residency gate is consulted for the model being placed, not a blank scope.
    assert scope["model"] == "JPCP"


def test_an_explicit_cluster_wins_over_the_yaml_placement(env, tmp_path, monkeypatch):
    _pack_with(tmp_path, monkeypatch, placement={"cluster": "auto", "gpus": 1})
    res, seen, _ = _capture_run(["pipeline", "run", "--model", "JPCP", "--cluster", "hpc-b"])
    assert res.exit_code == 0, res.output
    assert seen == {"cluster": "hpc-b", "gpus": 0}


def test_run_model_without_placement_is_unchanged(env):
    res, seen, gen = _capture_run(["pipeline", "run", "--model", "JPCP", "--dummy"])
    assert res.exit_code == 0, res.output
    assert seen == {} and gen == [["--dummy", "--model", "JPCP"]]


def test_an_invalid_pack_placement_fails_closed(env, tmp_path, monkeypatch):
    _pack_with(tmp_path, monkeypatch, placement={"gpus": -1, "cluster": "auto"})
    res, seen, gen = _capture_run(["pipeline", "run", "--model", "JPCP"])
    assert res.exit_code == 1 and "invalid placement" in res.output
    assert not gen and not seen


# ── run --ir on Slurm/Flux: the generator stages the YAML with the job (clause 3) ────────────
def _generator():
    return pytest.importorskip("pipelines.pipeline_generator")  # needs the pack + modelzoo


def test_hpc_job_stages_a_non_pack_yaml_and_the_script_registers_it(tmp_path, monkeypatch):
    pg = _generator()
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "flux")
    staged_src = tmp_path / "ir.yaml"
    staged_src.write_text(JPCP_YAML.read_text())
    monkeypatch.setitem(pg._EXTRA_MODEL_YAMLS, "JPCP", staged_src)

    adapter = MagicMock()
    adapter.working_dir = tmp_path / "work"
    adapter.submit_job.return_value = "f123"
    with patch("adapter.get_scheduler_adapter", return_value=adapter):
        job_id, _ = pg.slurm_submit_task.fn(MagicMock(), MagicMock(), "JPCP", "PM100Dataset")

    assert job_id == "f123"
    ((local, remote), _) = adapter.executor.put.call_args
    assert local == str(staged_src) and remote.endswith("/model.yaml")
    script = Path(adapter.submit_job.call_args.kwargs["script_path"]).read_text()
    assert f"--model-yaml {remote}" in script


def test_hpc_job_for_a_pack_model_stages_nothing(tmp_path, monkeypatch):
    pg = _generator()
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "slurm")
    monkeypatch.delitem(pg._EXTRA_MODEL_YAMLS, "JPCP", raising=False)
    adapter = MagicMock()
    adapter.working_dir = tmp_path / "work"
    adapter.submit_job.return_value = "7"
    with patch("adapter.get_scheduler_adapter", return_value=adapter):
        pg.slurm_submit_task.fn(MagicMock(), MagicMock(), "JPCP", "PM100Dataset")
    adapter.executor.put.assert_not_called()
    script = Path(adapter.submit_job.call_args.kwargs["script_path"]).read_text()
    assert "--model-yaml" not in script


def test_register_extra_model_yaml_records_the_file(tmp_path, monkeypatch):
    pg = _generator()
    src = tmp_path / "x.yaml"
    raw = yaml.safe_load(JPCP_YAML.read_text())
    raw["name"] = "JPCPIR"
    src.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(pg, "_EXTRA_MODEL_YAMLS", {})
    monkeypatch.setattr(pg, "MODEL_REGISTRY", dict(pg.MODEL_REGISTRY))
    assert pg.register_extra_model_yaml(src) == "JPCPIR"
    assert pg._EXTRA_MODEL_YAMLS == {"JPCPIR": src.resolve()} and "JPCPIR" in pg.MODEL_REGISTRY


def _register_placed(pg, tmp_path, monkeypatch, placement) -> str:
    src = tmp_path / "placed.yaml"
    raw = yaml.safe_load(JPCP_YAML.read_text())
    raw["name"] = "JPCPPL"
    raw["placement"] = placement
    src.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr(pg, "_EXTRA_MODEL_YAMLS", {})
    monkeypatch.setattr(pg, "MODEL_REGISTRY", dict(pg.MODEL_REGISTRY))
    for var in ("GPUS", "CPUS", "NODES"):
        monkeypatch.delenv(f"EXAMLOPS_HPC_{var}", raising=False)
        monkeypatch.delenv(f"EXAMLOPS_SLURM_{var}", raising=False)
    return pg.register_extra_model_yaml(src)


def test_the_placement_ask_is_what_the_hpc_job_requests(tmp_path, monkeypatch):
    """The ask that placed the job must also be requested from the scheduler — a job placed for
    its 2 GPUs used to be submitted asking for none (only EXAMLOPS_HPC_GPUS reached sbatch)."""
    pg = _generator()
    name = _register_placed(pg, tmp_path, monkeypatch, {"gpus": 2, "cpus": 8, "nodes": 1})
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "slurm")
    adapter = MagicMock()
    adapter.working_dir = tmp_path / "work"
    adapter.submit_job.return_value = "9"
    with patch("adapter.get_scheduler_adapter", return_value=adapter):
        pg.slurm_submit_task.fn(MagicMock(), MagicMock(), name, "PM100Dataset")
    res = adapter.submit_job.call_args.kwargs["resources"]
    assert (res["gpus"], res["cpus_per_task"], res["nodes"]) == ("2", "8", "1")


def test_explicit_hpc_env_still_wins_over_the_placement_ask(tmp_path, monkeypatch):
    pg = _generator()
    name = _register_placed(pg, tmp_path, monkeypatch, {"gpus": 2, "cpus": 8})
    monkeypatch.setenv("EXAMLOPS_HPC_GPUS", "4")
    res = pg._hpc_resources(name, "PM100Dataset")
    assert res["gpus"] == "4" and res["cpus_per_task"] == "8" and res["nodes"] == "1"


def test_an_invalid_placement_fails_closed_at_submission(tmp_path, monkeypatch):
    pg = _generator()
    name = _register_placed(pg, tmp_path, monkeypatch, {"gpus": 2})
    pg.MODEL_REGISTRY[name][1]._yaml.placement["gpus"] = -1
    with pytest.raises(ValueError, match="invalid placement"):
        pg._hpc_resources(name, "PM100Dataset")


# ── the compute-node side: slurm_train_script.py --model-yaml ───────────────────────────────
def _load_train_script():
    spec = importlib.util.spec_from_file_location(
        "_exa_slurm_train_script", _ROOT / "pipelines" / "slurm_train_script.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_generator(monkeypatch):
    fake = types.ModuleType("pipelines.pipeline_generator")
    fake.MODEL_REGISTRY = {}

    class _Model:
        estimator = {"trained": True}

        def train_step(self, loader):
            self.loader = loader

    class _Cfg:
        @staticmethod
        def get_train_components(ds_cls, **kw):
            return _Model(), None, ["batch"]

    def register_extra_model_yaml(path):
        name = yaml.safe_load(Path(path).read_text())["name"]
        fake.MODEL_REGISTRY[name] = (object, _Cfg, {})
        return name

    fake.register_extra_model_yaml = register_extra_model_yaml
    fake._resolve_dataset_cls = lambda cfg, ds: object
    monkeypatch.setitem(sys.modules, "pipelines.pipeline_generator", fake)
    return fake


def _run_script(monkeypatch, *argv):
    mod = _load_train_script()
    monkeypatch.setattr(sys, "argv", ["slurm_train_script.py", *argv])
    mod.main()


def test_node_registers_the_staged_yaml_and_trains(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://unused")
    _fake_generator(monkeypatch)
    staged = tmp_path / "model.yaml"
    staged.write_text("name: JPCP\n")
    out = tmp_path / "out" / "model.pkl"
    _run_script(
        monkeypatch,
        *("--model", "JPCP", "--dataset", "D", "--output", str(out)),
        *("--model-yaml", str(staged)),
    )
    import joblib

    assert joblib.load(out) == {"trained": True}


def test_node_refuses_a_missing_or_mismatched_staged_yaml(tmp_path, monkeypatch):
    _fake_generator(monkeypatch)
    out = tmp_path / "m.pkl"
    base = ("--model", "JPCP", "--dataset", "D", "--output", str(out))
    with pytest.raises(SystemExit) as exc:
        _run_script(monkeypatch, *base, "--model-yaml", str(tmp_path / "gone.yaml"))
    assert exc.value.code == 1
    other = tmp_path / "other.yaml"
    other.write_text("name: OTHER\n")
    with pytest.raises(SystemExit) as exc:
        _run_script(monkeypatch, *base, "--model-yaml", str(other))
    assert exc.value.code == 1 and not out.exists()


# ── review fixes: the whole placement ask reaches `--cluster auto`; a broken YAML fails closed ──
#: One wide single-node cluster and one narrower four-node cluster. A GPU-only ask prefers
#: ``wide`` (more idle GPUs); an ask that also needs 3 nodes can only land on ``tall``.
_CLUSTERS = [
    {"name": "wide", "scheduler": "slurm", "nodes": [{"state": "idle", "gpus": 8, "cpus": 64}]},
    {
        "name": "tall",
        "scheduler": "flux",
        "nodes": [{"state": "idle", "gpus": 1, "cpus": 16} for _ in range(4)],
    },
]


def _run_placed(argv, monkeypatch):
    """Run through the real ``_resolve_cluster_env`` + ``choose_cluster``; record the target."""
    chosen: list[str] = []
    monkeypatch.setattr(
        "examlops.hpc_registry.active_clusters_with_inventory", lambda: _CLUSTERS, raising=False
    )
    monkeypatch.delenv("EXAMLOPS_PLACEMENT_PROVIDER", raising=False)

    def fake_env(target):
        chosen.append(target)
        return {}

    gen: list[list[str]] = []
    with (
        patch("examlops.hpc_registry.resolve_env", side_effect=fake_env),
        patch("examlops.cli.commands.pipeline._run_generator", side_effect=gen.append),
    ):
        res = runner.invoke(app, argv)
    return res, chosen, gen


def test_a_gpu_only_ask_lands_on_the_wide_cluster(env, tmp_path, monkeypatch):
    """Control for the tests below: without a node ask, placement picks ``wide``."""
    _pack_with(tmp_path, monkeypatch, placement={"cluster": "auto", "gpus": 1})
    res, chosen, gen = _run_placed(["pipeline", "run", "--model", "JPCP"], monkeypatch)
    assert res.exit_code == 0, res.output
    assert chosen == ["wide"] and len(gen) == 1


def test_run_model_placement_nodes_reach_auto_placement(env, tmp_path, monkeypatch):
    """`placement.nodes` must constrain `--cluster auto`, not only `gpus` (it used to be dropped)."""
    _pack_with(tmp_path, monkeypatch, placement={"cluster": "auto", "gpus": 1, "nodes": 3})
    res, chosen, gen = _run_placed(["pipeline", "run", "--model", "JPCP"], monkeypatch)
    assert res.exit_code == 0, res.output
    assert chosen == ["tall"] and len(gen) == 1


def test_run_ir_step_nodes_reach_auto_placement(env, tmp_path, monkeypatch):
    y = _pack_with(tmp_path, monkeypatch, placement={"cluster": "auto", "gpus": 1, "nodes": 3})
    res, chosen, _ = _run_placed(["pipeline", "run", "--ir", str(y), "--dummy"], monkeypatch)
    assert res.exit_code == 0, res.output
    assert chosen == ["tall"]


def test_explicit_gpus_overrides_only_the_gpu_count_of_the_placement_ask(
    env, tmp_path, monkeypatch
):
    """`--gpus 5` keeps the YAML's `nodes: 3`: wide has 1 node, tall only 4 GPUs -> no fit."""
    _pack_with(tmp_path, monkeypatch, placement={"cluster": "auto", "gpus": 1, "nodes": 3})
    res, chosen, gen = _run_placed(
        ["pipeline", "run", "--model", "JPCP", "--gpus", "5"], monkeypatch
    )
    assert chosen == [] and not gen
    assert "found no cluster" in res.output


def test_explicit_gpus_is_also_what_the_job_requests(env, tmp_path, monkeypatch):
    """Placement scored --gpus 5, so the submission must ask for 5, not the YAML's 1."""
    monkeypatch.delenv("EXAMLOPS_HPC_GPUS", raising=False)
    _pack_with(tmp_path, monkeypatch, placement={"cluster": "auto", "gpus": 1})
    seen: list[str | None] = []

    def gen(_argv):
        seen.append(__import__("os").environ.get("EXAMLOPS_HPC_GPUS"))

    monkeypatch.setattr(
        "examlops.hpc_registry.active_clusters_with_inventory", lambda: _CLUSTERS, raising=False
    )
    with (
        patch("examlops.hpc_registry.resolve_env", return_value={}),
        patch("examlops.cli.commands.pipeline._run_generator", side_effect=gen),
    ):
        res = runner.invoke(app, ["pipeline", "run", "--model", "JPCP", "--gpus", "5"])
    assert res.exit_code == 0, res.output
    assert seen == ["5"]


def test_an_unparseable_pack_yaml_fails_closed_for_run_and_show(env, tmp_path, monkeypatch):
    """A model YAML that exists but does not parse must not be read as "no placement"."""
    models = tmp_path / "pack" / "models"
    models.mkdir(parents=True)
    (models / "jpcp.yaml").write_text("name: JPCP\nplacement: [unclosed\n")
    monkeypatch.setenv("RAY_MODELS_DIR", str(models))
    res, chosen, gen = _run_placed(["pipeline", "run", "--model", "JPCP"], monkeypatch)
    assert res.exit_code == 1 and "could not be read as YAML" in res.output
    assert not gen and not chosen
    res = runner.invoke(app, ["pipeline", "show", "JPCP"])
    assert res.exit_code == 1 and "could not be read as YAML" in res.output


def _alias_bomb(levels: int = 8) -> str:
    """A sub-kilobyte registry YAML whose ``fairness`` section expands to 10**levels nodes."""
    lines = ['  a0: &a0 ["x","x","x","x","x","x","x","x","x","x"]']
    for i in range(1, levels + 1):
        lines.append(f"  a{i}: &a{i} [{','.join([f'*a{i - 1}'] * 10)}]")
    return JPCP_YAML.read_text() + "fairness:\n" + "\n".join(lines) + "\n"


def test_an_alias_bomb_is_refused_before_it_expands(tmp_path):
    """The byte cap does not bound work: 10**8 nodes from <1 KiB took ~40 s at 10**7 unguarded."""
    import time

    bomb = tmp_path / "bomb.yaml"
    bomb.write_text(_alias_bomb())
    assert bomb.stat().st_size - JPCP_YAML.stat().st_size < 1024  # the bomb itself is <1 KiB
    t0 = time.monotonic()
    with pytest.raises(IRError, match="alias bomb"):
        load_ir(str(bomb))
    assert time.monotonic() - t0 < 5
    loop = tmp_path / "loop.yaml"
    loop.write_text(JPCP_YAML.read_text() + "fairness: &f\n  self: *f\n")
    with pytest.raises(IRError, match="self-referential"):
        load_ir(str(loop))


def test_a_benign_anchor_is_still_accepted(tmp_path):
    ok = tmp_path / "anchors.yaml"
    ok.write_text(JPCP_YAML.read_text() + "fairness:\n  a: &x {k: 1}\n  b: *x\n")
    assert load_ir(str(ok))["registry"]["fairness"] == {"a": {"k": 1}, "b": {"k": 1}}


def test_read_tier_ir_commands_refuse_an_alias_bomb(env, tmp_path, monkeypatch):
    models = tmp_path / "pack" / "models"
    models.mkdir(parents=True)
    (models / "jpcp.yaml").write_text(_alias_bomb())
    monkeypatch.setenv("RAY_MODELS_DIR", str(models))
    for argv in (
        ["pipeline", "explain", str(models / "jpcp.yaml")],
        ["pipeline", "show", "JPCP"],
        ["pipeline", "decompile", str(models / "jpcp.yaml")],
    ):
        res = runner.invoke(app, argv)
        assert res.exit_code == 1 and "alias bomb" in res.output, (argv, res.output)


def test_a_non_mapping_placement_fails_closed(env, tmp_path, monkeypatch):
    _pack_with(tmp_path, monkeypatch, placement=["auto"])
    res, chosen, gen = _run_placed(["pipeline", "run", "--model", "JPCP"], monkeypatch)
    assert res.exit_code == 1 and "invalid placement" in res.output
    assert not gen and not chosen
