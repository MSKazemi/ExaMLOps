# tests/unit/test_prompt_mlflow_backend.py
"""ADR 0009 clause 1 — the MLflow Prompt Registry as the prompt backend.

Runs against a real MLflow registry on a local SQLite store (no server, no network). The contract
is parity: with ``EXAMLOPS_PROMPT_BACKEND=mlflow`` every helper of ``examlops.data.prompts``
returns what the ``platform_db`` backend returns, a prompt renders byte-identically on both, and
``migrate_prompts`` moves a registry across with every version number and label intact.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

pytest.importorskip("mlflow")

from examlops import prompts  # noqa: E402
from examlops.data import prompts as store  # noqa: E402

_counter = itertools.count()


@pytest.fixture(scope="module")
def mlflow_uri(tmp_path_factory):
    # One registry for the module: creating an MLflow store runs its migrations (seconds).
    return f"sqlite:///{tmp_path_factory.mktemp('mlflow') / 'mlflow.db'}"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch, mlflow_uri):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("MLFLOW_TRACKING_URI", mlflow_uri)
    monkeypatch.delenv("EXAMLOPS_PROMPT_MLFLOW_URI", raising=False)
    monkeypatch.setenv("EXAMLOPS_PROMPT_BACKEND", "mlflow")
    prompts.clear_cache()
    from examlops.platform_db import init_db

    init_db()


def _name(stem: str) -> str:
    return f"{stem}-{next(_counter)}"  # the registry is shared across the module's tests


def test_versions_labels_and_rows_have_the_platform_db_shape():
    n = _name("triage")
    assert store.create_prompt_version(n, "Classify: {text}", variables=["text"], actor="ana") == 1
    assert store.create_prompt_version(n, "Label: {text}", tags={"team": "hpc"}) == 2
    store.set_prompt_label(n, "prod", 1)
    store.set_prompt_label(n, "dev", 2)
    row = store.get_prompt_by_label(n, "prod")
    assert row["name"] == n and row["version"] == 1 and row["template"] == "Classify: {text}"
    assert json.loads(row["variables"]) == ["text"] and row["actor"] == "ana"
    assert json.loads(store.get_prompt_version(n, 2)["tags"]) == {"team": "hpc"}
    assert [r["version"] for r in store.list_prompt_versions(n)] == [2, 1]
    assert [(r["label"], r["version"]) for r in store.list_prompt_labels(n)] == [
        ("dev", 2),
        ("prod", 1),
    ]
    assert n in store.list_prompt_names()


def test_missing_things_are_none_not_errors():
    n = _name("absent")
    assert store.get_prompt_by_label(n, "prod") is None
    assert store.get_prompt_version(n, 1) is None
    assert store.list_prompt_versions(n) == [] and store.list_prompt_labels(n) == []
    store.create_prompt_version(n, "x {y}")
    assert store.get_prompt_by_label(n, "nope") is None
    assert store.get_prompt_version(n, 9) is None


def test_a_template_renders_byte_identically_on_both_backends(monkeypatch):
    # Format specs, escaped braces and repeated fields: the edges a syntax conversion would lose.
    template = "Job {job:>8} | {{not a var}} | {job}:{score:.2f}"
    n = _name("render")
    store.create_prompt_version(n, template, variables=["job", "score"])
    store.set_prompt_label(n, "prod", 1)
    via_mlflow = prompts.render(prompts.get_prompt(n, "prod"), job="4711", score=0.5)
    monkeypatch.setenv("EXAMLOPS_PROMPT_BACKEND", "platform_db")
    prompts.clear_cache()
    store.create_prompt_version(n, template, variables=["job", "score"])
    store.set_prompt_label(n, "prod", 1)
    via_db = prompts.render(prompts.get_prompt(n, "prod"), job="4711", score=0.5)
    assert via_mlflow == via_db == "Job     4711 | {not a var} | 4711:0.50"


def test_the_template_syntax_is_recorded_for_mlflow_users():
    from mlflow import MlflowClient

    n = _name("syntax")
    store.create_prompt_version(n, "Hi {name}")
    uri = __import__("os").environ["MLFLOW_TRACKING_URI"]
    pv = MlflowClient(tracking_uri=uri, registry_uri=uri).get_prompt_version(n, 1)
    assert pv.tags["examlops.template_syntax"] == "python-format"
    assert pv.template == "Hi {name}"  # verbatim, never converted


def test_migration_preserves_version_numbers_and_labels(monkeypatch):
    n = _name("migrate")
    with store.use_backend("platform_db"):
        for i in range(3):
            store.create_prompt_version(n, f"v{i + 1}: {{q}}", variables=["q"], actor="bob")
        store.set_prompt_label(n, "prod", 2)
        store.set_prompt_label(n, "staging", 3)
    dry = prompts.migrate_prompts(to="mlflow", dry_run=True)
    assert n in dry["migrated"] and store.list_prompt_versions(n) == []  # nothing written
    report = prompts.migrate_prompts(to="mlflow")
    assert n in report["migrated"]
    assert [r["template"] for r in reversed(store.list_prompt_versions(n))] == [
        "v1: {q}",
        "v2: {q}",
        "v3: {q}",
    ]
    assert store.get_prompt_by_label(n, "prod")["version"] == 2
    assert store.get_prompt_by_label(n, "staging")["template"] == "v3: {q}"
    again = prompts.migrate_prompts(to="mlflow")
    assert n in {s["name"] for s in again["skipped"]}  # never merged, never renumbered
    with pytest.raises(ValueError):
        prompts.migrate_prompts(to="mlflow", source="mlflow")


def test_the_backend_must_be_named_correctly_and_mlflow_needs_a_uri(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_PROMPT_BACKEND", "mlfow")
    with pytest.raises(ValueError, match="not one of"):
        store.list_prompt_names()
    monkeypatch.setenv("EXAMLOPS_PROMPT_BACKEND", "mlflow")
    monkeypatch.delenv("MLFLOW_TRACKING_URI")
    from examlops.prompts.mlflow_backend import PromptBackendError

    with pytest.raises(PromptBackendError, match="MLFLOW_TRACKING_URI"):
        store.list_prompt_names()


def test_the_cli_works_unchanged_on_the_mlflow_backend():
    from typer.testing import CliRunner

    from examlops.cli.commands.prompt_cmd import app

    runner = CliRunner()
    n = _name("cli")
    assert runner.invoke(app, ["create", n, "--template", "Q: {q}"]).exit_code == 0
    assert runner.invoke(app, ["create", n, "--template", "Q2: {q}"]).exit_code == 0
    res = runner.invoke(app, ["label", n, "prod", "2", "--force"])
    assert res.exit_code == 0, res.output
    assert store.get_prompt_by_label(n, "prod")["template"] == "Q2: {q}"
    back = runner.invoke(app, ["backend"])
    assert "mlflow" in back.output
