"""ADR 0080 — ``exa pipeline compile | explain | run --ir``: real CLI runner, real policy engine.

Only the generator subprocess is faked (``_run_generator``); the IR, lowering, policy gate,
tiering and argv are the production code. A slower live run of the lowered pipeline through the
real generator lives in ``tests/integration/test_pipeline_dsl_run_equivalence.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "platform" / "cli" / "src"))
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()
EXAMPLE = str(_ROOT / "examples" / "pipeline-as-code" / "jpcp_flow.py")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.delenv("EXAMLOPS_HPC_SCHEDULER", raising=False)
    monkeypatch.delenv("EXAMLOPS_SLURM_MODE", raising=False)
    monkeypatch.setattr("examlops.policy.POLICY_YAML", tmp_path / "policy.yaml")
    from examlops import platform_db

    platform_db.init_db()
    return tmp_path


def _policy(env, text):
    (env / "policy.yaml").write_text(text)


def _compile(env, *extra):
    ir = env / "j.ir.json"
    res = runner.invoke(app, ["pipeline", "compile", EXAMPLE, "--out", str(ir), *extra])
    return res, ir


def test_compile_writes_ir_and_yaml_and_reports_the_hash(env):
    y = env / "j.yaml"
    res, ir = _compile(env, "--yaml", str(y))
    assert res.exit_code == 0, res.output
    doc = json.loads(ir.read_text())
    assert doc["content_hash"].startswith("sha256:") and doc["name"] == "JPCP"
    assert yaml.safe_load(y.read_text())["name"] == "JPCP"
    assert doc["content_hash"].replace("\n", "") in res.output.replace("\n", "")


def test_compile_json_mode_is_one_document(env):
    res = runner.invoke(app, ["--json", "pipeline", "compile", EXAMPLE])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["name"] == "JPCP" and out["ir"]["content_hash"] == out["content_hash"]


def test_compile_exits_1_on_a_validation_error(env, tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from examlops.sdk import pipeline, dataset\n"
        "@pipeline(name='B')\ndef b():\n    dataset('D', bogus=1)\n"
    )
    res = runner.invoke(app, ["pipeline", "compile", str(bad)])
    assert res.exit_code == 1 and "unknown param" in res.output


def test_compile_yaml_refuses_an_unlowerable_pipeline(env, tmp_path):
    f = tmp_path / "h.py"
    f.write_text(
        "from examlops.sdk import pipeline, dataset, hpo\n"
        "@pipeline(name='H')\ndef h():\n    hpo(dataset('D'), config_class='a.B')\n"
    )
    y = tmp_path / "h.yaml"
    res = runner.invoke(app, ["pipeline", "compile", str(f), "--yaml", str(y)])
    assert res.exit_code == 1 and "not lowerable" in res.output.lower()
    assert not y.exists()
    # ... while plain compile + explain of the same file work: refusal is at lowering, not before
    ir = tmp_path / "h.json"
    assert runner.invoke(app, ["pipeline", "compile", str(f), "--out", str(ir)]).exit_code == 0
    ex = runner.invoke(app, ["--json", "pipeline", "explain", str(ir)])
    assert ex.exit_code == 0 and json.loads(ex.stdout)["runnable"] is False


def test_untrusted_flag_uses_the_ast_gate(env, tmp_path):
    f = tmp_path / "i.py"
    f.write_text("import os\n" + Path(EXAMPLE).read_text())
    res = runner.invoke(app, ["pipeline", "compile", str(f), "--untrusted"])
    assert res.exit_code == 1 and "untrusted-mode gate" in res.output


def test_explain_prints_the_topological_plan_and_is_read_only(env):
    _, ir = _compile(env)
    before = ir.read_bytes()
    res = runner.invoke(app, ["--json", "pipeline", "explain", str(ir)])
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert [r["kind"] for r in out["plan"]][-3:] == ["train", "evaluate", "promote"]
    assert out["runnable"] is True
    assert ir.read_bytes() == before


def test_explain_rejects_a_tampered_ir(env):
    _, ir = _compile(env)
    doc = json.loads(ir.read_text())
    doc["name"] = "EVIL"
    ir.write_text(json.dumps(doc))
    res = runner.invoke(app, ["pipeline", "explain", str(ir)])
    assert res.exit_code == 1 and "content_hash" in res.output


# ── policy hooks ─────────────────────────────────────────────────────────────────────────────
def test_no_policy_means_no_policy_audit_row(env):
    from examlops.data.audit import export_audit_events

    res, _ = _compile(env)
    assert res.exit_code == 0
    assert [e for e in export_audit_events() if str(e["action"]).startswith("policy:")] == []


def test_policy_can_deny_compile_and_the_deny_is_audited(env):
    from examlops.data.audit import export_audit_events

    _policy(
        env,
        "policies:\n  - name: no-jpcp\n    action: pipeline_compile\n"
        "    when: \"pipeline == 'JPCP'\"\n    effect: deny\n",
    )
    res, ir = _compile(env)
    assert res.exit_code == 1 and "no-jpcp" in res.output
    assert not ir.exists()
    assert any(e["action"] == "policy:pipeline_compile" for e in export_audit_events())


# ── run --ir ─────────────────────────────────────────────────────────────────────────────────
def _run(env, *extra):
    _, ir = _compile(env)
    captured: list[list[str]] = []
    seen_yaml: list[dict] = []

    def fake(args):
        captured.append(list(args))
        p = args[args.index("--model-yaml") + 1] if "--model-yaml" in args else None
        if p:
            seen_yaml.append(yaml.safe_load(Path(p).read_text()))

    with patch("examlops.cli.commands.pipeline._run_generator", side_effect=fake):
        res = runner.invoke(app, ["pipeline", "run", "--ir", str(ir), *extra])
    return res, captured, seen_yaml


def test_run_ir_passes_the_lowered_yaml_to_the_real_generator_argv(env):
    res, captured, seen = _run(env, "--dummy", "--dataset", "PM100Dataset")
    assert res.exit_code == 0, res.output
    (args,) = captured
    assert (
        args[:1] == ["--dummy"] and "--model" in args and args[args.index("--model") + 1] == "JPCP"
    )
    assert "--dataset" in args and "--model-yaml" in args
    assert seen[0]["name"] == "JPCP" and len(seen[0]["datasets"]) == 2
    # the temp YAML is cleaned up after the run
    assert not Path(args[args.index("--model-yaml") + 1]).exists()


def test_run_without_ir_argv_is_unchanged(env):
    captured: list[list[str]] = []
    with patch("examlops.cli.commands.pipeline._run_generator", side_effect=captured.append):
        res = runner.invoke(app, ["pipeline", "run", "--model", "JPCP", "--dummy"])
    assert res.exit_code == 0, res.output
    assert captured == [["--dummy", "--model", "JPCP"]]


def test_run_ir_model_mismatch_and_unknown_dataset_are_refused(env):
    res, captured, _ = _run(env, "--model", "OTHER")
    assert res.exit_code == 1 and "does not match" in res.output and not captured
    res, captured, _ = _run(env, "--dataset", "Nope")
    assert res.exit_code == 1 and "not in the IR" in res.output and not captured


def test_run_ir_refuses_an_unlowerable_ir(env, tmp_path):
    f = tmp_path / "h.py"
    f.write_text(
        "from examlops.sdk import pipeline, dataset, hpo\n"
        "@pipeline(name='H')\ndef h():\n    hpo(dataset('D'), config_class='a.B')\n"
    )
    ir = tmp_path / "h.json"
    runner.invoke(app, ["pipeline", "compile", str(f), "--out", str(ir)])
    with patch("examlops.cli.commands.pipeline._run_generator") as gen:
        res = runner.invoke(app, ["pipeline", "run", "--ir", str(ir)])
    assert res.exit_code == 1 and "not lowerable" in res.output.lower()
    gen.assert_not_called()


def test_run_ir_is_portable_to_a_remote_scheduler(env, monkeypatch):
    """ADR 0080 decision 3: no longer refused — the generator stages the YAML with the job
    (the staging itself is covered in tests/unit/test_pipeline_ir_placement.py)."""
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "slurm")
    res, captured, seen = _run(env)
    assert res.exit_code == 0, res.output
    assert "staged to the job's working directory" in " ".join(res.output.split())
    assert len(captured) == 1 and "--model-yaml" in captured[0] and seen[0]["name"] == "JPCP"


def test_policy_can_deny_run_ir_before_anything_runs(env):
    _policy(
        env,
        "policies:\n  - name: no-ir-runs\n    action: pipeline_run_ir\n    effect: deny\n",
    )
    _, ir = _compile(env)
    with patch("examlops.cli.commands.pipeline._run_generator") as gen:
        res = runner.invoke(app, ["pipeline", "run", "--ir", str(ir)])
    assert res.exit_code == 1 and "no-ir-runs" in res.output
    gen.assert_not_called()


# ── surface tiers ────────────────────────────────────────────────────────────────────────────
def test_tiers_are_declared():
    from examlops.cli import surface

    assert surface.TIERS["pipeline explain"] == "read"
    assert surface.TIERS["pipeline compile"] == "admin"  # executes an operator-written file
    assert surface.TIERS["pipeline run"] == "admin"
