"""ADR 0078 clause 4 + the typing gate — the SDK surface is reflected, documented and typed.

* ``exa docs --sdk`` and the MCP agent card render :func:`examlops.sdk.reference.describe`, so
  humans and agents read one contract derived from the code.
* Every public function is fully annotated (parameters and return) and every public name has a
  docstring — the static half of "typed interfaces" the ADR asks for, checked hermetically.
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path

import pytest

import examlops
from examlops.sdk.reference import NAMESPACES, describe, render_markdown


def _public_objects():
    for public, module_name in NAMESPACES:
        module = importlib.import_module(module_name)
        for name in module.__all__:
            if name.startswith("__"):
                continue
            yield f"{public}.{name}", getattr(module, name)


def test_every_exported_name_resolves_and_is_described():
    ref = describe()
    assert ref["api_version"] == examlops.api_version()
    for public, module_name in NAMESPACES:
        module = importlib.import_module(module_name)
        names = [n for n in module.__all__ if not n.startswith("__")]
        assert [e["name"] for e in ref["namespaces"][public]] == names
    kinds = {e["name"]: e["kind"] for e in ref["namespaces"]["examlops.models"]}
    assert kinds["retrain"] == "function" and kinds["ModelSummary"] == "type"
    json.dumps(ref)


@pytest.mark.parametrize(("qualname", "obj"), list(_public_objects()))
def test_the_public_surface_is_typed_and_documented(qualname, obj):
    if inspect.ismodule(obj):
        assert inspect.getdoc(obj), f"{qualname} has no module docstring"
        return
    assert inspect.getdoc(obj), f"{qualname} has no docstring"
    if inspect.isfunction(obj):
        sig = inspect.signature(obj)
        missing = [
            p.name
            for p in sig.parameters.values()
            if p.annotation is inspect.Parameter.empty and p.kind not in (p.VAR_POSITIONAL,)
        ]
        assert not missing, f"{qualname}: unannotated parameters {missing}"
        assert sig.return_annotation is not inspect.Signature.empty, (
            f"{qualname}: no return annotation"
        )


def test_signatures_read_like_the_code():
    ref = describe()
    [lineage] = [e for e in ref["namespaces"]["examlops.models"] if e["name"] == "lineage"]
    assert lineage["signature"] == "(name: str, version: str | None = None) -> Lineage"


def test_markdown_reference_lists_every_namespace():
    md = render_markdown()
    for public, _ in NAMESPACES:
        assert f"## `{public}`" in md
    assert "`retrain(" in md


def test_exa_docs_sdk(tmp_path):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--json", "docs", "--sdk"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["api_version"] == examlops.api_version()
    assert "examlops.models" in doc["namespaces"]

    out = tmp_path / "sdk.md"
    result = runner.invoke(app, ["docs", "--sdk", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_text().startswith("# `examlops` Python SDK reference")


def test_exa_docs_without_sdk_is_unchanged():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    result = CliRunner().invoke(app, ["--json", "docs"])
    assert result.exit_code == 0
    assert json.loads(result.output)["name"] == "exa"


def test_the_agent_card_carries_the_sdk_contract():
    from examlops.mcp.agent_card import build_agent_card

    card = build_agent_card(include_writes=False)
    sdk = card["sdk"]
    assert sdk["available"] is True and sdk["apiVersion"] == examlops.api_version()
    names = {e["name"] for e in sdk["namespaces"]["examlops.models"]}
    assert {"list", "get", "diff", "lineage", "cost", "retrain", "approve", "promote"} <= names
    assert sdk["reference"] == "exa docs --sdk"


def test_the_agent_card_survives_a_broken_reflection(monkeypatch):
    from examlops.mcp.agent_card import build_agent_card
    from examlops.sdk import reference

    # A real local path, derived at runtime: this checkout's own location is exactly the kind of
    # machine-specific detail an exception message carries and a public card must not publish.
    local_path = str(Path(__file__).resolve())

    def boom():
        raise RuntimeError(f"cannot import {local_path}")

    monkeypatch.setattr(reference, "describe", boom)
    card = build_agent_card(include_writes=False)
    # The card is public discovery metadata: the failure is named, its message (a local path
    # here) is not published.
    assert card["sdk"] == {"available": False, "error": "RuntimeError"}
    assert local_path not in json.dumps(card)


def test_py_typed_marker_ships():
    assert (Path(examlops.__file__).parent / "py.typed").is_file()
