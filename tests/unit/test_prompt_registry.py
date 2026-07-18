# tests/unit/test_prompt_registry.py
"""B1 — Prompt registry (ADR 0009, spec B1).

GWT-1 immutability · GWT-2 resolution · GWT-3 missing var · GWT-4 label move ·
GWT-5 fail-safe cache · GWT-6 rollback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import prompts  # noqa: E402
from examlops.platform_db import (  # noqa: E402
    create_prompt_version,
    get_prompt_by_label,
    init_db,
    list_prompt_versions,
    set_prompt_label,
)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    init_db()
    prompts.clear_cache()
    yield
    prompts.clear_cache()


def test_gwt1_immutability():
    v1 = create_prompt_version("P", "Hello {x}", variables=["x"], actor="me")
    v2 = create_prompt_version("P", "Hi {x}!", variables=["x"], actor="me")
    assert v1 == 1 and v2 == 2
    versions = list_prompt_versions("P")
    assert len(versions) == 2
    # v1 unchanged
    v1_row = next(r for r in versions if r["version"] == 1)
    assert v1_row["template"] == "Hello {x}"


def test_gwt2_resolution_by_label():
    v = create_prompt_version("P", "answer {q}", variables=["q"], actor="me")
    set_prompt_label("P", "prod", v)
    pv = prompts.get_prompt("P", "prod")
    assert pv.version == v
    assert prompts.render(pv, q="42") == "answer 42"


def test_gwt3_missing_var_raises_before_call():
    v = create_prompt_version("P", "need {x} and {y}", variables=["x", "y"], actor="me")
    set_prompt_label("P", "prod", v)
    pv = prompts.get_prompt("P", "prod")
    with pytest.raises(ValueError, match="missing variable"):
        prompts.render(pv, x="1")


def test_declared_variables_autodetect():
    assert prompts.declared_variables("a {b} c {d} {b}") == ["b", "d"]


def test_gwt4_label_move_changes_resolution():
    v1 = create_prompt_version("P", "v1 {x}", variables=["x"], actor="me")
    v2 = create_prompt_version("P", "v2 {x}", variables=["x"], actor="me")
    set_prompt_label("P", "prod", v1)
    prompts.clear_cache()
    assert prompts.get_prompt("P", "prod").version == v1
    set_prompt_label("P", "prod", v2)
    prompts.clear_cache()
    assert prompts.get_prompt("P", "prod").version == v2


def test_gwt5_failsafe_last_known_good(monkeypatch):
    v = create_prompt_version("P", "hi {x}", variables=["x"], actor="me")
    set_prompt_label("P", "prod", v)
    pv = prompts.get_prompt("P", "prod")  # populates cache
    assert pv.version == v

    # Registry now "unreachable": make the resolver raise.
    import examlops.platform_db as db

    def _boom(*_a, **_k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(db, "get_prompt_by_label", _boom)
    prompts._cache[("P", "prod")] = (pv, 0.0)  # expire TTL to force a fetch
    # Fail-safe returns last-known-good rather than raising.
    assert prompts.get_prompt("P", "prod").version == v


def test_gwt5_no_cache_raises(monkeypatch):
    # get_prompt_by_label's body now lives in examlops.data.prompts (item 4.5 relocation); patch there.
    monkeypatch.setattr(
        "examlops.data.prompts.get_prompt_by_label",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError()),
    )
    with pytest.raises(RuntimeError):
        prompts.get_prompt("Unknown", "prod")


def test_gwt6_rollback_keeps_history():
    v1 = create_prompt_version("P", "v1", actor="me")
    v2 = create_prompt_version("P", "v2", actor="me")
    set_prompt_label("P", "prod", v2)
    # rollback == point label back to v1
    set_prompt_label("P", "prod", v1)
    assert get_prompt_by_label("P", "prod")["version"] == v1
    assert len(list_prompt_versions("P")) == 2  # v2 history remains


def test_render_treats_vars_as_data():
    # A variable value containing braces must not be re-interpreted as a placeholder.
    v = create_prompt_version("P", "value: {x}", variables=["x"], actor="me")
    set_prompt_label("P", "prod", v)
    pv = prompts.get_prompt("P", "prod")
    assert prompts.render(pv, x="{y}") == "value: {y}"
