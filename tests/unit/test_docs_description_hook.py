"""The docs description hook gives each page its own summary without overriding front matter."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import yaml

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "docs" / "overrides" / "seo_hooks.py"


def _load():
    spec = importlib.util.spec_from_file_location("seo_hooks", HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


hooks = _load()
LONG = "ExaMLOps trains models as Slurm or Flux jobs and versions every run in the MLflow registry."


def test_first_real_paragraph_becomes_the_description():
    page = f"<h1>T</h1><p>Short.</p><p>{LONG}</p><p>Second paragraph that is also long enough.</p>"
    assert hooks.describe(page) == LONG


def test_markup_and_entities_are_flattened():
    page = (
        "<p>Run <code>exa pipeline run</code> &amp; watch <a href='x'>the map</a> — it is live.</p>"
    )
    assert hooks.describe(page) == "Run exa pipeline run & watch the map — it is live."


def test_admonition_titles_are_not_mistaken_for_the_summary():
    page = f'<div class="admonition"><p class="admonition-title">In short, this is a title line</p></div><p>{LONG}</p>'
    assert hooks.describe(page) == LONG


def test_long_paragraphs_are_cut_at_a_word_boundary():
    text = "word " * 60
    result = hooks.describe(f"<p>{text}</p>")
    assert result is not None
    assert len(result) <= hooks.MAX_LEN
    assert result.endswith("…")
    assert not result[:-1].endswith(" ")


def test_a_page_with_no_usable_paragraph_gets_nothing():
    assert hooks.describe("<h1>Only a heading</h1><p>Too short.</p>") is None


def test_front_matter_description_always_wins():
    page = SimpleNamespace(meta={"description": "Written by hand."})
    hooks.on_page_content(f"<p>{LONG}</p>", page=page, config=None, files=None)
    assert page.meta["description"] == "Written by hand."


def test_missing_description_is_filled_from_the_page():
    page = SimpleNamespace(meta={})
    out = hooks.on_page_content(f"<p>{LONG}</p>", page=page, config=None, files=None)
    assert out == f"<p>{LONG}</p>"
    assert page.meta["description"] == LONG


def test_the_hook_is_registered_in_mkdocs_yml():
    config = yaml.load((REPO / "mkdocs.yml").read_text(), Loader=yaml.BaseLoader)
    assert "docs/overrides/seo_hooks.py" in config.get("hooks", [])
