# tests/unit/test_ray_serve_aliases.py
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pipelines.registry_loader import ModelEntry  # noqa: E402


def _make_entry(name: str, aliases: list[str]) -> ModelEntry:
    return ModelEntry(
        name=name, model_class_name=name, config_class_name=None,
        datasets=[], backend="zenodo", dummy=False, enabled=True,
        lifecycle=[], serve_aliases=aliases, prefect={},
    )


def test_get_serve_aliases_returns_per_model_aliases(monkeypatch):
    import serving.ray_serving.app as app
    fake_entries = [
        _make_entry("jpcp", ["Production"]),
        _make_entry("mack", ["Production", "Canary"]),
    ]
    monkeypatch.setattr(app, "_REGISTRY_ENTRIES", fake_entries)
    assert app._get_serve_aliases_for("jpcp") == ["Production"]
    assert app._get_serve_aliases_for("mack") == ["Production", "Canary"]


def test_get_serve_aliases_falls_back_to_global(monkeypatch):
    import serving.ray_serving.app as app
    monkeypatch.setattr(app, "_REGISTRY_ENTRIES", None)
    result = app._get_serve_aliases_for("unknown_model")
    assert result == app.PRELOAD_ALIASES


def test_get_serve_aliases_falls_back_when_model_not_in_registry(monkeypatch):
    import serving.ray_serving.app as app
    monkeypatch.setattr(app, "_REGISTRY_ENTRIES", [_make_entry("jpcp", ["Production"])])
    result = app._get_serve_aliases_for("mack")   # "mack" not in registry
    assert result == app.PRELOAD_ALIASES


def test_get_serve_aliases_case_insensitive(monkeypatch):
    """MLflow uses lowercase names ('jpcp'); YAML typically uses class casing ('JPCP')."""
    import serving.ray_serving.app as app
    monkeypatch.setattr(app, "_REGISTRY_ENTRIES", [_make_entry("JPCP", ["Production"])])
    assert app._get_serve_aliases_for("jpcp") == ["Production"]
    assert app._get_serve_aliases_for("JPCP") == ["Production"]
