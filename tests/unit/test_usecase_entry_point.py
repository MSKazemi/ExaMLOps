"""Platform-usecase Stage 5 — installable pack discovery via entry points (ADR 0094, BL-005).

The platform resolves the active use-case pack generically from the
``examlops.usecase_packs`` entry-point group, so a pip-installed pack is discovered with no
env var and no code change — while the platform still names no concrete use-case.
"""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


class _FakeEP:
    def __init__(self, target):
        self._target = target

    def load(self):
        return self._target


def _make_pack(tmp_path: Path) -> Path:
    pack = tmp_path / "installed_pack"
    (pack / "models").mkdir(parents=True)
    (pack / "datasets").mkdir()
    return pack


def _patch_entry_points(monkeypatch, eps: list):
    from examlops import usecase

    def fake_entry_points(*, group=None):
        return eps if group == usecase.USECASE_PACK_GROUP else []

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("RAY_MODELS_DIR", "MODELS_YAML_DIR", "EXAMLOPS_USECASE_DIR"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_installed_pack_is_discovered_when_no_env(tmp_path, monkeypatch):
    from examlops import usecase

    pack = _make_pack(tmp_path)
    _patch_entry_points(monkeypatch, [_FakeEP(lambda: str(pack))])

    assert usecase.models_dir() == pack / "models"
    assert usecase._pack_dir() == pack


def test_callable_and_plain_path_targets_both_work(tmp_path, monkeypatch):
    from examlops import usecase

    pack = _make_pack(tmp_path)
    # entry point resolving directly to a path (not a callable)
    _patch_entry_points(monkeypatch, [_FakeEP(str(pack))])
    assert usecase.models_dir() == pack / "models"


def test_env_var_takes_precedence_over_entry_point(tmp_path, monkeypatch):
    from examlops import usecase

    ep_pack = _make_pack(tmp_path)
    env_pack = tmp_path / "env_pack"
    (env_pack / "models").mkdir(parents=True)
    _patch_entry_points(monkeypatch, [_FakeEP(lambda: str(ep_pack))])
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(env_pack))

    assert usecase.models_dir() == env_pack / "models"  # env wins


def test_no_entry_point_falls_back_to_default(monkeypatch):
    from examlops import usecase

    _patch_entry_points(monkeypatch, [])
    assert usecase.models_dir() == Path(usecase.DEFAULT_MODELS_DIR)
    assert usecase._entry_point_pack_root() is None


def test_broken_entry_point_fails_open(tmp_path, monkeypatch):
    from examlops import usecase

    def _boom():
        raise RuntimeError("bad pack")

    good = _make_pack(tmp_path)
    # a broken EP is skipped, a later good one still resolves
    _patch_entry_points(monkeypatch, [_FakeEP(_boom), _FakeEP(lambda: str(good))])
    assert usecase._entry_point_pack_root() == good


def test_nonexistent_pack_path_is_ignored(tmp_path, monkeypatch):
    from examlops import usecase

    _patch_entry_points(monkeypatch, [_FakeEP(lambda: str(tmp_path / "does_not_exist"))])
    assert usecase._entry_point_pack_root() is None
    assert usecase.models_dir() == Path(usecase.DEFAULT_MODELS_DIR)
