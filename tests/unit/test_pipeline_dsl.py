"""ADR 0080 — the pipeline-as-code IR, DSL and lowering (pure: no CLI, no Prefect, no network)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from examlops import sdk  # noqa: E402
from examlops.pipeline_dsl import (  # noqa: E402
    IRError,
    NotLowerableError,
    build_ir,
    content_hash,
    custom_python,
    dataset,
    evaluate,
    hpo,
    lower_training,
    pipeline,
    promote,
    topological_order,
    train,
    validate_ir,
)
from examlops.pipeline_dsl.ir import new_node  # noqa: E402
from examlops.pipeline_dsl.loader import load_pipeline_file  # noqa: E402

EXAMPLE = _ROOT / "examples" / "pipeline-as-code" / "jpcp_flow.py"
REFERENCE_JPCP = _ROOT / "usecases" / "reference" / "models" / "jpcp.yaml"
_LIFECYCLE = [{"name": "Production", "metric": "rmse", "threshold": 50.0}]


def _simple(name="M"):
    @pipeline(name=name)
    def flow():
        d = dataset("D1", backend="zenodo")
        m = train(d, config_class="x_config.XConfiguration", task_type="regression")
        promote(evaluate(m), lifecycle=_LIFECYCLE)

    return flow


# ── IR validity ──────────────────────────────────────────────────────────────────────────────
def test_compile_yields_validated_hashed_versioned_ir():
    ir = _simple().compile()
    assert ir["schema_version"] == 1 and ir["kind"] == "training"
    assert [n["kind"] for n in ir["nodes"]] == ["dataset", "train", "evaluate", "promote"]
    assert ir["content_hash"] == content_hash(ir) and ir["content_hash"].startswith("sha256:")
    validate_ir(ir)
    # typed ports are carried on the nodes
    train_node = next(n for n in ir["nodes"] if n["kind"] == "train")
    assert train_node["inputs"] == {"datasets": "dataset[]"} and train_node["outputs"] == {
        "model": "model"
    }


def test_same_pipeline_hashes_identically_and_a_change_changes_it():
    assert _simple().compile()["content_hash"] == _simple().compile()["content_hash"]
    assert _simple("M").compile()["content_hash"] != _simple("N").compile()["content_hash"]


def test_hash_is_independent_of_dict_key_order():
    ir = _simple().compile()
    shuffled = json.loads(json.dumps(ir, sort_keys=True))
    shuffled = dict(reversed(list(shuffled.items())))
    assert content_hash(shuffled) == ir["content_hash"]
    validate_ir(shuffled)


def test_tampering_after_compile_is_detected():
    ir = _simple().compile()
    ir["nodes"][0]["params"]["backend"] = "minio"
    with pytest.raises(IRError, match="content_hash does not match"):
        validate_ir(ir)


def _base():
    return copy.deepcopy(_simple().compile())


def _rehash(doc):
    doc.pop("content_hash", None)
    return doc


def test_duplicate_step_id_refused():
    doc = _rehash(_base())
    doc["nodes"].append(copy.deepcopy(doc["nodes"][0]))
    with pytest.raises(IRError, match="duplicate step id"):
        validate_ir(doc)


def test_dangling_edge_refused():
    doc = _rehash(_base())
    doc["edges"].append({"from": "ghost", "output": "data", "to": "train", "input": "datasets"})
    with pytest.raises(IRError, match="dangling edge: source step 'ghost'"):
        validate_ir(doc)
    doc = _rehash(_base())
    doc["edges"][0]["input"] = "nope"
    with pytest.raises(IRError, match="has no input 'nope'"):
        validate_ir(doc)


def test_unknown_step_kind_refused():
    doc = _rehash(_base())
    doc["nodes"][0]["kind"] = "teleport"
    with pytest.raises(IRError, match="unknown step kind 'teleport'"):
        validate_ir(doc)
    with pytest.raises(IRError, match="unknown step kind"):
        new_node("x", "teleport")


def test_unknown_pipeline_kind_and_schema_version_refused():
    doc = _rehash(_base())
    doc["kind"] = "inference"
    with pytest.raises(IRError, match="unknown pipeline kind"):
        validate_ir(doc)
    doc = _rehash(_base())
    doc["schema_version"] = 99
    with pytest.raises(IRError, match="unsupported schema_version"):
        validate_ir(doc)


def test_cycle_refused_with_the_steps_named():
    # evaluate feeds dataset? datasets have no inputs, so build a cycle between two `custom_python`.
    a = new_node("a", "custom_python", {"entrypoint": "m:f"})
    b = new_node("b", "custom_python", {"entrypoint": "m:g"})
    edges = [
        {"from": "a", "output": "result", "to": "b", "input": "upstream"},
        {"from": "b", "output": "result", "to": "a", "input": "upstream"},
    ]
    with pytest.raises(IRError, match=r"cycle detected among steps: a, b"):
        build_ir(name="C", kind="training", nodes=[a, b], edges=edges)


def test_type_mismatch_and_unconnected_required_input_refused():
    ds = new_node("d", "dataset", {"name": "D"})
    ev = new_node("e", "evaluate")
    with pytest.raises(IRError, match="type mismatch"):
        build_ir(
            name="T",
            kind="training",
            nodes=[ds, ev],
            edges=[{"from": "d", "output": "data", "to": "e", "input": "model"}],
        )
    with pytest.raises(IRError, match="required input 'model' is not connected"):
        build_ir(name="T", kind="training", nodes=[ev], edges=[])


def test_unknown_and_missing_params_refused():
    with pytest.raises(IRError, match=r"unknown param\(s\) \['bogus'\]"):

        @pipeline(name="P")
        def flow():
            dataset("D", bogus=1)

        flow.compile()
    with pytest.raises(IRError, match=r"missing required param"):

        @pipeline(name="P")
        def flow2():
            train(dataset("D"), task_type="regression")

        flow2.compile()


def test_non_json_param_refused():
    with pytest.raises(IRError, match="not JSON-serialisable"):

        @pipeline(name="P")
        def flow():
            dataset("D", batch_size=float("nan"))

        flow.compile()


def test_unknown_registry_key_refused():
    with pytest.raises(IRError, match="unknown registry key"):

        @pipeline(name="P", bogus_section={"a": 1})
        def flow():
            train(dataset("D"), config_class="a.B", task_type="regression")

        flow.compile()


def test_topological_order_is_deterministic_and_dependency_respecting():
    order = topological_order(_simple().compile())
    assert order.index("dataset_D1") < order.index("train") < order.index("evaluate")
    assert order.index("evaluate") < order.index("promote")


# ── tracing context ──────────────────────────────────────────────────────────────────────────
def test_helpers_outside_a_pipeline_trace_raise():
    with pytest.raises(IRError, match="only run inside a @pipeline"):
        dataset("D")


def test_nested_compile_refused():
    inner = _simple("inner")

    @pipeline(name="outer")
    def outer():
        inner.compile()

    with pytest.raises(IRError, match="inside another pipeline"):
        outer.compile()


def test_a_non_ref_input_is_refused():
    from examlops.pipeline_dsl import step

    with pytest.raises(IRError, match="must be the result of another step"):

        @pipeline(name="P")
        def flow():
            step("evaluate", inputs={"model": "not-a-ref"})

        flow.compile()


# ── lowering ─────────────────────────────────────────────────────────────────────────────────
def test_example_lowers_to_exactly_the_reference_jpcp_yaml(tmp_path):
    """The DSL twin of ``models/jpcp.yaml`` loads to the *same* ``ModelYAMLConfig``."""
    from pipelines.model_loader import load_model_yaml

    pdef, names = load_pipeline_file(str(EXAMPLE))
    assert names == ["JPCP"]
    lowered = lower_training(pdef.compile())
    out = tmp_path / "jpcp.yaml"
    out.write_text(yaml.safe_dump(lowered.model_yaml, sort_keys=False))
    assert load_model_yaml(out) == load_model_yaml(REFERENCE_JPCP)


def test_lowered_config_registers_like_the_yaml_config(tmp_path):
    """Through the generator's own registration path: same datasets, same inference params."""
    if not (_ROOT / "modelzoo" / "seanergys_modelzoo").is_dir():
        pytest.skip("seanergys_modelzoo not present")
    import pipelines.pipeline_generator as pg
    from pipelines.model_loader import load_model_yaml

    pdef, _ = load_pipeline_file(str(EXAMPLE))
    out = tmp_path / "jpcp.yaml"
    out.write_text(yaml.safe_dump(lower_training(pdef.compile()).model_yaml, sort_keys=False))
    saved = dict(pg.MODEL_REGISTRY)
    try:
        pg.register_model_from_yaml(load_model_yaml(REFERENCE_JPCP))
        _, ref_cfg, ref_tasks = pg.MODEL_REGISTRY["JPCP"]
        ref = (
            [d.__name__ for d in ref_cfg.SUPPORTED_DATASETS],
            ref_cfg.get_inference_params(),
            sorted(ref_tasks),
        )
        pg.register_model_from_yaml(load_model_yaml(out))
        _, cfg, tasks = pg.MODEL_REGISTRY["JPCP"]
        got = (
            [d.__name__ for d in cfg.SUPPORTED_DATASETS],
            cfg.get_inference_params(),
            sorted(tasks),
        )
        assert got == ref
    finally:
        pg.MODEL_REGISTRY.clear()
        pg.MODEL_REGISTRY.update(saved)


def test_dataset_order_is_the_authors_order_not_alphabetical():
    ir = _simple().compile()
    doc = lower_training(ir).model_yaml
    assert [d["name"] for d in doc["datasets"]] == ["D1"]

    @pipeline(name="Z")
    def flow():
        b = dataset("Zeta")
        a = dataset("Alpha")
        promote(
            evaluate(train(b, a, config_class="a.B", task_type="regression")),
            lifecycle=_LIFECYCLE,
        )

    assert [d["name"] for d in lower_training(flow.compile()).model_yaml["datasets"]] == [
        "Zeta",
        "Alpha",
    ]


@pytest.mark.parametrize(
    "build, needle",
    [
        (lambda: hpo(dataset("D"), config_class="a.B"), "is not lowerable yet"),
        (lambda: custom_python("m:f"), "is not lowerable yet"),
    ],
)
def test_known_but_unlowerable_kinds_are_refused_never_skipped(build, needle):
    @pipeline(name="P")
    def flow():
        d = dataset("D")
        promote(
            evaluate(train(d, config_class="a.B", task_type="regression")), lifecycle=_LIFECYCLE
        )
        build()

    ir = flow.compile()  # valid, explainable ...
    with pytest.raises(NotLowerableError, match=needle):  # ... but refused when lowered
        lower_training(ir)


def test_orphan_dataset_second_train_and_missing_evaluate_are_refused():
    @pipeline(name="P")
    def orphan():
        dataset("Unused")
        promote(
            evaluate(train(dataset("D"), config_class="a.B", task_type="regression")),
            lifecycle=_LIFECYCLE,
        )

    with pytest.raises(NotLowerableError, match="not consumed by the train step"):
        lower_training(orphan.compile())

    @pipeline(name="P")
    def two_train():
        d = dataset("D")
        evaluate(train(d, config_class="a.B", task_type="regression"))
        train(d, config_class="a.C", task_type="regression")

    with pytest.raises(NotLowerableError, match="exactly one 'train'"):
        lower_training(two_train.compile())

    @pipeline(name="P")
    def no_eval():
        train(dataset("D"), config_class="a.B", task_type="regression")

    with pytest.raises(NotLowerableError, match="always runs a 'evaluate' step"):
        lower_training(no_eval.compile())


def test_only_the_validation_split_is_lowerable():
    @pipeline(name="P")
    def flow():
        evaluate(train(dataset("D"), config_class="a.B", task_type="regression"), split="test")

    with pytest.raises(NotLowerableError, match="only the 'validation' split"):
        lower_training(flow.compile())


def test_resources_and_cluster_become_run_hints_and_are_reported_as_not_in_the_yaml():
    from examlops.pipeline_dsl import Resources

    @pipeline(name="P", cluster="auto")
    def flow():
        evaluate(
            train(
                dataset("D"),
                config_class="a.B",
                task_type="regression",
                resources=Resources(gpus=2, cpus=8),
            )
        )

    low = lower_training(flow.compile())
    assert low.hints == {"cluster": "auto", "gpus": 2}
    assert any("target.cluster" in d for d in low.dropped) and any("gpus" in d for d in low.dropped)
    assert "resources" not in low.model_yaml  # the YAML schema has no such field


# ── file loading and trust tiers ─────────────────────────────────────────────────────────────
def test_pick_pipeline_by_name_and_ambiguity(tmp_path):
    f = tmp_path / "two.py"
    f.write_text(
        "from examlops.sdk import pipeline, dataset, train\n"
        "@pipeline(name='A')\ndef a():\n    train(dataset('D'), config_class='a.B', task_type='r')\n"
        "@pipeline(name='B')\ndef b():\n    train(dataset('D'), config_class='a.B', task_type='r')\n"
    )
    with pytest.raises(IRError, match="defines several pipelines"):
        load_pipeline_file(str(f))
    assert load_pipeline_file(f"{f}:B")[0].name == "B"
    with pytest.raises(IRError, match="no pipeline named 'Z'"):
        load_pipeline_file(f"{f}:Z")


_UNTRUSTED_OK = (
    "@pipeline(name='U')\n"
    "def u():\n"
    "    d = dataset('D')\n"
    "    evaluate(train(d, config_class='a.B', task_type='regression'))\n"
)


def test_untrusted_mode_accepts_a_declarative_file_with_dsl_names_preinjected(tmp_path):
    f = tmp_path / "ok.py"
    f.write_text(_UNTRUSTED_OK)
    pdef, _ = load_pipeline_file(str(f), sandboxed=True)
    assert pdef.compile()["name"] == "U"


@pytest.mark.parametrize(
    "body",
    [
        "import os\n",
        "x = open('/etc/passwd')\n",
        "y = getattr(1, 'real')\n",
        "z = (1).__class__\n",
        "exec('1')\n",
    ],
)
def test_untrusted_mode_refuses_the_escape_hatches_the_provider_gate_refuses(tmp_path, body):
    f = tmp_path / "bad.py"
    f.write_text(body + _UNTRUSTED_OK)
    with pytest.raises(IRError, match="refused by the untrusted-mode gate"):
        load_pipeline_file(str(f), sandboxed=True)


def test_trusted_mode_runs_real_python_and_says_so_by_behaviour(tmp_path):
    """Trusted tier is unsandboxed: an import (and any side effect) runs. Documented, not hidden."""
    marker = tmp_path / "ran"
    f = tmp_path / "t.py"
    f.write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
        + _UNTRUSTED_OK.replace("@pipeline", "from examlops.sdk import *\n@pipeline")
    )
    load_pipeline_file(str(f))
    assert marker.exists()


# ── SDK facade ───────────────────────────────────────────────────────────────────────────────
def test_sdk_exposes_the_dsl():
    for name in ("pipeline", "step", "dataset", "train", "evaluate", "promote", "Resources"):
        assert name in sdk.__all__ and hasattr(sdk, name)
    assert sdk.pipeline is pipeline
