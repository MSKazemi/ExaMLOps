"""ADR 0079 decision 6 — datasheets as lint-able artifacts a policy can require before promotion.

The questionnaire linter, the file lookup (pack → config dir, env override), the ``datasheet``
promotion gate (off / monitor / enforce) through the real ``exa pipeline promote`` path, and the
``exa cards lint --datasheet`` / ``--template`` CLI. Only MLflow's HTTP client is faked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cards import datasheet as ds  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


def _complete() -> dict:
    return {
        section: {q.key: f"answer to {q.key}" for q in questions}
        for section, questions in ds.SECTIONS.items()
    }


@pytest.fixture
def pack(tmp_path, monkeypatch):
    """A throwaway use-case pack declaring model `demo` trained on dataset `DemoSet`."""
    root = tmp_path / "pack"
    (root / "models").mkdir(parents=True)
    (root / "datasheets").mkdir()
    (root / "pack.toml").write_text('name = "t"\n')
    (root / "models" / "demo.yaml").write_text(
        yaml.safe_dump({"model_name": "demo", "datasets": [{"name": "DemoSet"}]})
    )
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(root))
    monkeypatch.delenv("RAY_MODELS_DIR", raising=False)
    monkeypatch.delenv("MODELS_YAML_DIR", raising=False)
    monkeypatch.delenv("EXAMLOPS_DATASHEETS_DIR", raising=False)
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_ACTOR", "tester")
    monkeypatch.setattr("examlops.policy.POLICY_YAML", tmp_path / "policy.yaml")
    monkeypatch.delenv("EXAMLOPS_POLICY_GATES", raising=False)
    from examlops import platform_db

    platform_db.init_db()
    return root


def _write(pack: Path, doc: dict, name: str = "DemoSet") -> Path:
    p = pack / "datasheets" / f"{name}.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


# ── questionnaire ───────────────────────────────────────────────────────────────────────────
def test_seven_gebru_sections():
    assert list(ds.SECTIONS) == [
        "motivation",
        "composition",
        "collection_process",
        "preprocessing",
        "uses",
        "distribution",
        "maintenance",
    ]
    assert len(ds.REQUIRED) == sum(len(q) for q in ds.SECTIONS.values())


def test_complete_datasheet_has_no_findings():
    assert ds.lint_questionnaire(_complete()) == []
    assert ds.questionnaire_completeness(_complete()) == 1.0


@pytest.mark.parametrize("placeholder", ["", "TODO", "tbd", "not provided", None, [], "  ?  "])
def test_placeholders_are_unanswered(placeholder):
    doc = _complete()
    doc["uses"]["prohibited_uses"] = placeholder
    findings = ds.lint_questionnaire(doc)
    assert findings == [
        "uses.prohibited_uses unanswered: Are there tasks for which the dataset should not be used?"
    ]
    assert 0 < ds.questionnaire_completeness(doc) < 1


def test_structured_answers_count_and_a_missing_section_is_every_question():
    doc = _complete()
    doc["composition"]["count"] = 1200
    doc["maintenance"]["contact"] = ["curator@example.org"]
    del doc["motivation"]
    findings = ds.lint_questionnaire(doc)
    assert len(findings) == len(ds.SECTIONS["motivation"])
    assert all(f.startswith("motivation.") for f in findings)


def test_template_lists_every_question_unanswered():
    doc = yaml.safe_load(ds.template("DemoSet"))
    assert doc["dataset"] == "DemoSet"
    assert len(ds.lint_questionnaire(doc)) == len(ds.REQUIRED)


# ── lookup ──────────────────────────────────────────────────────────────────────────────────
def test_pack_datasheet_found_and_scored(pack):
    _write(pack, _complete())
    findings, score, path = ds.lint_dataset("DemoSet")
    assert findings == [] and score == 1.0 and path == pack / "datasheets" / "DemoSet.yaml"


def test_env_dir_wins(pack, tmp_path, monkeypatch):
    other = tmp_path / "sheets"
    other.mkdir()
    (other / "DemoSet.json").write_text(json.dumps(_complete()))
    monkeypatch.setenv("EXAMLOPS_DATASHEETS_DIR", str(other))
    assert ds.find_datasheet("DemoSet") == other / "DemoSet.json"


def test_missing_datasheet_is_one_finding(pack):
    findings, score, path = ds.lint_dataset("DemoSet")
    assert score == 0.0 and path is None
    assert len(findings) == 1 and findings[0].startswith("no datasheet for DemoSet")


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b", "", ".hidden"])
def test_names_cannot_traverse(pack, name):
    with pytest.raises(ds.DatasheetError):
        ds.find_datasheet(name)


def test_malformed_and_oversize_files_are_findings_not_crashes(pack, monkeypatch):
    (pack / "datasheets" / "DemoSet.yaml").write_text("motivation: [unclosed\n")
    findings, score, _ = ds.lint_dataset("DemoSet")
    assert score == 0.0 and "could not be parsed" in findings[0]
    (pack / "datasheets" / "DemoSet.yaml").write_text("- a list\n")
    assert "not a mapping" in ds.lint_dataset("DemoSet")[0][0]
    monkeypatch.setattr(ds, "_MAX_BYTES", 10)
    _write(pack, _complete())
    assert "larger than" in ds.lint_dataset("DemoSet")[0][0]


def test_model_datasets_and_promotion_reasons(pack):
    assert ds.model_datasets("DEMO") == ["DemoSet"]
    assert ds.promotion_reasons("demo")[0].startswith("datasheet for DemoSet: completeness 0.00")
    _write(pack, _complete())
    assert ds.promotion_reasons("demo") == []
    half = _complete()
    half["uses"] = {}
    _write(pack, half)
    assert ds.promotion_reasons("demo", floor=0.5) == []
    assert ds.promotion_reasons("demo", floor=1.0)
    assert "declares no training dataset" in ds.promotion_reasons("unknown")[0]


# ── the promotion gate, through `exa pipeline promote` ─────────────────────────────────────
_ALIAS = {"registered_model": {"aliases": [{"alias": "Staging", "version": "3"}]}}
_VER = {"model_version": {"run_id": "run-1", "version": "3"}}
_RUN = {"run": {"data": {"metrics": {"rmse": 1.0}, "params": {}, "tags": []}}}


def _get(url, **_):
    if "registered-models/get" in url:
        return _ALIAS
    if "model-versions/get" in url:
        return _VER
    if "runs/get" in url:
        return _RUN
    return {}


def _promote():
    with (
        patch("examlops.cli.commands.pipeline._client.get", side_effect=_get),
        patch("examlops.cli.commands.pipeline._client.post", return_value={"ok": True}) as post,
    ):
        res = runner.invoke(app, ["--yes", "pipeline", "promote", "demo", "--if-rmse-lt", "5"])
    return res, post


def _alias_moved(post) -> bool:
    return any("alias" in str(c.args[0]) for c in post.call_args_list)


def test_gate_off_by_default_promotes_without_a_datasheet(pack):
    res, post = _promote()
    assert res.exit_code == 0, res.output
    assert _alias_moved(post)


def test_gate_enforced_refuses_undocumented_data(pack, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "datasheet=enforce")
    res, post = _promote()
    assert res.exit_code == 1
    assert "datasheet" in res.output and "DemoSet" in res.output
    assert not _alias_moved(post)
    from examlops.data.audit import export_audit_events

    assert any(e["action"] == "policy_datasheet" for e in export_audit_events())


def test_gate_enforced_allows_documented_data(pack, monkeypatch):
    _write(pack, _complete())
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "datasheet=enforce")
    res, post = _promote()
    assert res.exit_code == 0, res.output
    assert _alias_moved(post)


def test_gate_floor_from_policy_yaml(pack, tmp_path):
    half = _complete()
    half["maintenance"] = {}
    _write(pack, half)
    (tmp_path / "policy.yaml").write_text("gates:\n  datasheet: {mode: enforce, floor: 0.8}\n")
    res, post = _promote()
    assert res.exit_code == 0, res.output  # 17/20 answered ≥ 0.8
    (tmp_path / "policy.yaml").write_text("gates:\n  datasheet: {mode: enforce, floor: 0.95}\n")
    res, post = _promote()
    assert res.exit_code == 1 and not _alias_moved(post)


def test_gate_monitor_never_blocks_but_records(pack, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_POLICY_GATES", "datasheet=monitor")
    res, post = _promote()
    assert res.exit_code == 0, res.output
    assert _alias_moved(post)
    from examlops.data.audit import export_audit_events

    assert any(e["action"] == "policy_gate_monitor:datasheet" for e in export_audit_events())


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def test_cards_lint_datasheet_flag(pack):
    res = runner.invoke(app, ["--json", "cards", "lint", "DemoSet", "--datasheet"])
    doc = json.loads(res.stdout)
    assert res.exit_code == 1
    assert doc["questionnaire"]["completeness"] == 0.0
    assert any(f.startswith("datasheet: no datasheet") for f in doc["findings"])


def test_cards_lint_template_prints_a_fillable_skeleton(pack):
    res = runner.invoke(app, ["cards", "lint", "DemoSet", "--template"])
    assert res.exit_code == 0, res.output
    assert "prohibited_uses: TODO" in res.stdout
    assert len(ds.lint_questionnaire(yaml.safe_load(res.stdout))) == len(ds.REQUIRED)
    bad = runner.invoke(app, ["cards", "lint", "../x", "--template"])
    assert bad.exit_code == 1
