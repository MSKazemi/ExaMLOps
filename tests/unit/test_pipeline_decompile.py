"""ADR 0080, the reverse direction — ``exa pipeline decompile``: registry YAML → ``@pipeline``.

The forward direction (``test_pipeline_dsl.py``) proves the DSL twin of ``jpcp.yaml`` lowers to
that YAML. This file proves the other way round, against the *shipped* pack models rather than a
fixture: read the real YAML, emit DSL source, compile that source through the production
``load_pipeline_file`` + ``lower_training``, and require the result to load to the same
``ModelYAMLConfig`` — the same equality assertion the forward test uses.

The other half of the contract is the refusal: a YAML holding anything the DSL cannot express must
fail by name and write nothing, because a file that silently drops a section is worse than no file.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "platform" / "cli" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from typer.testing import CliRunner  # noqa: E402

from examlops.cli import surface as s  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.pipeline_dsl import lower_training  # noqa: E402
from examlops.pipeline_dsl.decompile import (  # noqa: E402
    _DATASET_ORDER,
    _KNOWN_TOP_LEVEL,
    _TRAIN_ORDER,
    NotRepresentableError,
    decompile_model_yaml,
    ir_from_model_yaml,
)
from examlops.pipeline_dsl.ir import REGISTRY_KEYS, STEP_KINDS  # noqa: E402
from examlops.pipeline_dsl.loader import load_pipeline_file  # noqa: E402

runner = CliRunner()

PACK_MODELS = sorted((_ROOT / "usecases" / "reference" / "models").glob("*.yaml"))
JPCP = _ROOT / "usecases" / "reference" / "models" / "jpcp.yaml"


def _round_trip(model_yaml: Path, tmp_path: Path):
    """YAML → DSL source → compile → lowered YAML, through the production code paths."""
    from pipelines.model_loader import load_model_yaml

    source = decompile_model_yaml(yaml.safe_load(model_yaml.read_text()), source=str(model_yaml))
    flow = tmp_path / f"{model_yaml.stem}_flow.py"
    flow.write_text(source, encoding="utf-8")
    pdef, _names = load_pipeline_file(str(flow))
    out = tmp_path / f"{model_yaml.stem}.yaml"
    out.write_text(yaml.safe_dump(lower_training(pdef.compile()).model_yaml, sort_keys=False))
    return flow, load_model_yaml(out), load_model_yaml(model_yaml)


# ── the round trip, on the real shipped pack ────────────────────────────────────────────────
def test_the_reference_jpcp_yaml_round_trips_through_the_dsl(tmp_path):
    """The headline claim: decompile → compile gives back the *same* ``ModelYAMLConfig``."""
    assert JPCP.is_file(), "the reference pack's jpcp.yaml is the fixture for this test"
    _flow, got, want = _round_trip(JPCP, tmp_path)
    assert got == want


@pytest.mark.parametrize("model_yaml", PACK_MODELS, ids=lambda p: p.stem)
def test_every_shipped_pack_model_round_trips(model_yaml, tmp_path):
    _flow, got, want = _round_trip(model_yaml, tmp_path)
    assert got == want


def test_the_decompiled_file_names_the_same_pipeline_and_steps(tmp_path):
    flow, _got, _want = _round_trip(JPCP, tmp_path)
    pdef, names = load_pipeline_file(str(flow))
    assert names == ["JPCP"]
    doc = pdef.compile()
    assert [n["kind"] for n in doc["nodes"]] == [
        "dataset",
        "dataset",
        "train",
        "evaluate",
        "promote",
    ]
    # the author's dataset order survives — it is the run order the lowering depends on
    assert [n["params"]["name"] for n in doc["nodes"] if n["kind"] == "dataset"] == [
        "PM100Dataset",
        "FDataDataset",
    ]


def test_decompiling_twice_gives_the_same_bytes():
    raw = yaml.safe_load(JPCP.read_text())
    assert decompile_model_yaml(raw, source="x") == decompile_model_yaml(raw, source="x")


# ── the generated file is real, lintable Python ─────────────────────────────────────────────
def _ruff() -> str | None:
    return shutil.which("ruff") or next(
        (
            str(p)
            for p in [Path(sys.executable).parent / "ruff", _ROOT / ".venv/bin/ruff"]
            if p.is_file()
        ),
        None,
    )


@pytest.mark.parametrize("model_yaml", PACK_MODELS, ids=lambda p: p.stem)
def test_the_generated_source_passes_ruff_check(model_yaml, tmp_path):
    """Emitting source a project's own linter rejects is a defect, not a style opinion."""
    ruff = _ruff()
    if ruff is None:
        pytest.skip("ruff is not installed in this environment")
    flow = tmp_path / f"{model_yaml.stem}_flow.py"
    flow.write_text(decompile_model_yaml(yaml.safe_load(model_yaml.read_text())))
    proc = subprocess.run(
        [ruff, "check", "--no-cache", "--config", str(_ROOT / "pyproject.toml"), str(flow)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    ast.parse(flow.read_text())  # and it really is Python, not merely lint-clean text


# ── refusal: never a lossy file ─────────────────────────────────────────────────────────────
def _jpcp() -> dict:
    return yaml.safe_load(JPCP.read_text())


def test_an_unknown_top_level_key_is_refused_by_name():
    raw = _jpcp()
    raw["quarantine_policy"] = {"on_fail": "hold"}
    with pytest.raises(NotRepresentableError, match=r"quarantine_policy"):
        decompile_model_yaml(raw)


def test_an_unknown_dataset_key_is_refused_by_name():
    raw = _jpcp()
    raw["datasets"][0]["shuffle_seed"] = 7
    with pytest.raises(NotRepresentableError, match=r"shuffle_seed"):
        decompile_model_yaml(raw)


def test_a_value_yaml_parsed_into_a_non_json_type_is_refused(tmp_path):
    """An *unquoted* date is a real YAML construct the JSON-shaped IR cannot carry."""
    text = JPCP.read_text().replace('[adt, ">=", "2023-12-01"]', '[adt, ">=", 2023-12-01]')
    raw = yaml.safe_load(text)
    with pytest.raises(NotRepresentableError, match="JSON"):
        decompile_model_yaml(raw)


def test_a_model_with_no_datasets_is_refused():
    raw = _jpcp()
    raw["datasets"] = []
    with pytest.raises(NotRepresentableError, match="at least one dataset"):
        decompile_model_yaml(raw)


@pytest.mark.parametrize("missing", ["config_class", "task_type"])
def test_a_yaml_missing_a_required_train_param_is_refused(missing):
    raw = _jpcp()
    del raw[missing]
    with pytest.raises(NotRepresentableError, match=missing):
        decompile_model_yaml(raw)


def test_a_nameless_or_non_mapping_document_is_refused():
    with pytest.raises(NotRepresentableError, match="must be a mapping"):
        decompile_model_yaml(["not", "a", "model"])
    raw = _jpcp()
    raw["name"] = ""
    with pytest.raises(NotRepresentableError, match="non-empty string 'name'"):
        decompile_model_yaml(raw)


def test_the_self_check_fires_when_lowering_would_not_reproduce_the_input(monkeypatch):
    """The second gate: even a YAML that partitions cleanly must lower back to itself."""
    import examlops.pipeline_dsl.decompile as d

    real = d.lower_training

    def lossy(doc):
        out = real(doc)
        out.model_yaml.pop("serving", None)
        return out

    monkeypatch.setattr(d, "lower_training", lossy)
    with pytest.raises(NotRepresentableError, match=r"does not reproduce the YAML.*serving"):
        decompile_model_yaml(_jpcp())


# ── drift guards: the decompiler's tables against the schema they mirror ─────────────────────
def test_the_emission_order_tuples_cover_their_step_kinds():
    assert set(_TRAIN_ORDER) == STEP_KINDS["train"].params
    assert set(_DATASET_ORDER) | {"name"} == STEP_KINDS["dataset"].params


def test_every_model_yaml_field_has_a_place_in_the_dsl():
    """A new ``ModelYAMLConfig`` field that the DSL has no home for would make decompile refuse
    every model carrying it. Fail here instead, where the fix (extend ``REGISTRY_KEYS``) is."""
    from pipelines.model_loader import ModelYAMLConfig

    fields = {f.name for f in ModelYAMLConfig.__dataclass_fields__.values()}
    assert fields <= _KNOWN_TOP_LEVEL, sorted(fields - _KNOWN_TOP_LEVEL)
    assert REGISTRY_KEYS <= _KNOWN_TOP_LEVEL


def test_a_registry_section_the_pack_does_not_use_still_round_trips(tmp_path):
    """Every ``REGISTRY_KEYS`` section, not only the ones jpcp.yaml happens to carry."""

    raw = _jpcp()
    raw["project"] = "research"
    raw["resources"] = {"hardware_profile": "gpu-small"}
    raw["fairness"] = {"protected_attributes": ["user_id"]}
    raw["autoscale"] = {"min_replicas": 1, "max_replicas": 4}
    src = tmp_path / "wide.yaml"
    src.write_text(yaml.safe_dump(raw))
    _flow, got, want = _round_trip(src, tmp_path)
    assert got == want
    assert want.project == "research" and want.resources == {"hardware_profile": "gpu-small"}


def test_every_dataset_entry_field_is_a_dataset_step_param():
    from pipelines.model_loader import DatasetEntry

    fields = {f.name for f in DatasetEntry.__dataclass_fields__.values()}
    assert fields == STEP_KINDS["dataset"].params


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    return tmp_path


def test_cli_prints_the_source_when_no_out_is_given(env):
    res = runner.invoke(app, ["pipeline", "decompile", str(JPCP)])
    assert res.exit_code == 0, res.output
    assert "@pipeline(" in res.output and "def jpcp():" in res.output


def test_cli_writes_the_file_and_the_written_file_round_trips(env):
    flow = env / "flows" / "jpcp.py"
    flow.parent.mkdir()
    res = runner.invoke(app, ["pipeline", "decompile", str(JPCP), "--out", str(flow)])
    assert res.exit_code == 0, res.output
    assert flow.is_file()

    from pipelines.model_loader import load_model_yaml

    pdef, _ = load_pipeline_file(str(flow))
    out = env / "back.yaml"
    out.write_text(yaml.safe_dump(lower_training(pdef.compile()).model_yaml, sort_keys=False))
    assert load_model_yaml(out) == load_model_yaml(JPCP)


def test_cli_refuses_to_overwrite_without_force(env):
    flow = env / "jpcp.py"
    flow.write_text("# hand-edited\n")
    res = runner.invoke(app, ["pipeline", "decompile", str(JPCP), "--out", str(flow)])
    assert res.exit_code == 1 and "already exists" in res.output
    assert flow.read_text() == "# hand-edited\n"
    res = runner.invoke(app, ["pipeline", "decompile", str(JPCP), "--out", str(flow), "--force"])
    assert res.exit_code == 0, res.output
    assert "@pipeline(" in flow.read_text()


def test_cli_json_mode_is_one_document(env):
    res = runner.invoke(app, ["--json", "pipeline", "decompile", str(JPCP)])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["name"] == "JPCP"
    assert doc["content_hash"] == ir_from_model_yaml(_jpcp())["content_hash"]
    assert [step["kind"] for step in doc["steps"]][-2:] == ["evaluate", "promote"]
    assert doc["source"].startswith('"""JPCP as pipeline-as-code')
    assert doc["flow_file"] is None


def test_cli_exits_1_and_writes_nothing_on_an_unrepresentable_yaml(env):
    bad = env / "bad.yaml"
    raw = _jpcp()
    raw["quarantine_policy"] = {"on_fail": "hold"}
    bad.write_text(yaml.safe_dump(raw))
    flow = env / "never.py"
    res = runner.invoke(app, ["pipeline", "decompile", str(bad), "--out", str(flow)])
    assert res.exit_code == 1
    assert "quarantine_policy" in res.output.replace("\n", "")
    assert not flow.exists()


def test_cli_exits_1_on_a_missing_or_unparsable_file(env):
    res = runner.invoke(app, ["pipeline", "decompile", str(env / "nope.yaml")])
    assert res.exit_code == 1 and "not found" in res.output
    broken = env / "broken.yaml"
    broken.write_text("name: [unclosed\n")
    res = runner.invoke(app, ["pipeline", "decompile", str(broken)])
    assert res.exit_code == 1 and "YAML" in res.output


# ── dashboard surface: tier + path containment ──────────────────────────────────────────────
@pytest.fixture(scope="module")
def descriptor() -> dict:
    return {c["path"]: c for c in s.build_catalog()["commands"]}["pipeline decompile"]


def test_the_new_command_is_tiered_and_paneled(descriptor):
    assert s.TIERS["pipeline decompile"] == s.READ
    assert descriptor["tier"] == s.READ
    assert descriptor["panel"] in s.build_catalog()["panels"]


def test_both_of_its_paths_are_contained_in_the_workspace(descriptor, tmp_path):
    kinds = {p["name"]: p for p in descriptor["params"]}
    assert kinds["file"]["path"] and kinds["out"]["path"], "both params must be treated as paths"
    inv = s.build_argv(
        descriptor, {"file": "models/jpcp.yaml", "out": "flows/jpcp.py"}, workspace=tmp_path
    )
    assert f"--out={tmp_path.resolve()}/flows/jpcp.py" in inv.argv
    assert inv.argv[-2:] == ["--", f"{tmp_path.resolve()}/models/jpcp.yaml"]
    assert sorted(inv.paths) == ["flows/jpcp.py", "models/jpcp.yaml"]
    # supplying a filesystem path escalates the read to admin (surface.py's own rule)
    assert inv.tier == s.ADMIN


@pytest.mark.parametrize("escape", ["/etc/passwd", "../outside.py"])
def test_an_out_path_outside_the_workspace_is_refused(descriptor, tmp_path, escape):
    with pytest.raises(s.SurfaceError, match="workspace"):
        s.build_argv(descriptor, {"file": "m.yaml", "out": escape}, workspace=tmp_path)
