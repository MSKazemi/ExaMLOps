"""The instance-data root and the paths that derive from it (ADR 0128)."""

from __future__ import annotations

from pathlib import Path

import pytest

from examlops import platform_db as pdb
from examlops.lifecycle import datadir


def test_platform_db_resolution_order(tmp_path, monkeypatch):
    monkeypatch.delenv("PLATFORM_DB")
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path / "data"))
    assert pdb._db_path() == str(tmp_path / "data" / "platform.db")
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "explicit.db"))
    assert pdb._db_path() == str(tmp_path / "explicit.db")  # an explicit store still wins
    monkeypatch.delenv("PLATFORM_DB")
    monkeypatch.delenv("EXAMLOPS_DATA_DIR")
    assert pdb._db_path() == pdb._legacy_db_default()


def test_source_checkout_keeps_repo_root_default():
    repo = Path(pdb.__file__).resolve().parents[4]
    assert pdb._legacy_db_default() == str(repo / "platform.db")


def test_installed_wheel_defaults_to_the_user_data_dir(tmp_path, monkeypatch):
    fake = (
        tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "examlops" / "platform_db.py"
    )
    monkeypatch.setattr(pdb, "__file__", str(fake))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert pdb._legacy_db_default.__wrapped__() == str(
        tmp_path / "xdg" / "examlops" / "platform.db"
    )


def test_config_dir_rule(tmp_path, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_CONFIG_DIR", raising=False)
    assert datadir.config_dir() == Path.home() / ".config" / "examlops"
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))
    assert datadir.config_dir() == tmp_path / "config"
    monkeypatch.setenv("EXAMLOPS_CONFIG_DIR", str(tmp_path / "shared"))
    assert datadir.config_dir() == tmp_path / "shared"
    # the operator's CLI config never relocates the *site* configuration
    monkeypatch.delenv("EXAMLOPS_CONFIG_DIR")
    monkeypatch.delenv("EXAMLOPS_DATA_DIR")
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "private" / "config.toml"))
    assert datadir.config_dir() == Path.home() / ".config" / "examlops"


def test_hpc_registry_follows_the_data_root(tmp_path, monkeypatch):
    from examlops import hpc_registry

    monkeypatch.delenv("EXAMLOPS_HPC_REGISTRY", raising=False)
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))
    assert hpc_registry.registry_path() == tmp_path / "config" / "clusters.yaml"


def _fake_pack(root: Path) -> Path:
    (root / "models").mkdir(parents=True)
    (root / "models" / "demo.yaml").write_text("name: demo\n")
    (root / "pack.toml").write_text('[pack]\nname = "demo"\n')
    return root


def test_init_layout_seeds_a_pack_once(tmp_path):
    pack = _fake_pack(tmp_path / "src-pack")
    root = tmp_path / "data"
    out = datadir.init_layout(root, pack=pack)
    assert set(out["created"]) == {"usecase/", "config/", ".providers/", "backups/", "agent/"}
    assert (root / "usecase" / "models" / "demo.yaml").is_file()
    (root / "usecase" / "models" / "mine.yaml").write_text("user edit\n")
    again = datadir.init_layout(root, pack=pack)
    assert again["created"] == [] and again["pack_seeded_from"] is None
    assert (root / "usecase" / "models" / "mine.yaml").is_file()  # user content untouched
    with pytest.raises(ValueError, match="not a use-case pack"):
        datadir.init_layout(root, pack=tmp_path)


def test_both_pack_loaders_prefer_the_site_pack(tmp_path, monkeypatch):
    from examlops import usecase as cli_usecase
    from pipelines import usecase as engine_usecase

    monkeypatch.delenv("EXAMLOPS_USECASE_DIR", raising=False)
    monkeypatch.delenv("RAY_MODELS_DIR", raising=False)
    monkeypatch.delenv("MODELS_YAML_DIR", raising=False)
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))
    before = engine_usecase.usecase_dir()
    assert before != tmp_path / "usecase"  # no pack.toml yet: fall through to the default
    _fake_pack(tmp_path / "usecase")
    assert engine_usecase.usecase_dir() == (tmp_path / "usecase").resolve()
    assert cli_usecase.models_dir() == tmp_path / "usecase" / "models"
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(tmp_path / "other"))
    assert engine_usecase.usecase_dir() == (tmp_path / "other").resolve()


def test_inventory_names_every_kind_of_user_data(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))
    names = {loc.name for loc in datadir.inventory()}
    for expected in (
        "platform datastore",
        "model registry + run metadata (MLflow)",
        "site profile (modules)",
        "site configuration",
        "backup bundles",
    ):
        assert expected in names


def test_inventory_redacts_credentials(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    monkeypatch.setenv("EXAMLOPS_POSTGRES_DSN", "postgresql://app:s3cret@db:5432/examlops")
    locs = {loc.name: loc for loc in datadir.inventory()}
    assert "s3cret" not in locs["platform datastore"].where
    assert locs["platform datastore"].where == "postgresql://app:***@db:5432/examlops"


def test_agent_files_follow_the_data_root(tmp_path, monkeypatch):
    from examlops.backup import sqlite_tier

    for var in ("AGENT_DB", "AGENT_MEMORY_DB", "AGENT_MEMORY_REVIEW_DB"):
        monkeypatch.delenv(var, raising=False)
    review = next(s for s in sqlite_tier._SQLITE_DBS if s["name"] == "skipper_review")
    assert sqlite_tier._resolve_db_path(review) == "./skipper_review.db"  # no root: unchanged
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(tmp_path))
    assert sqlite_tier._resolve_db_path(review) == str(tmp_path / "agent" / "skipper_review.db")
    locs = {loc.name: loc for loc in datadir.inventory()}
    assert locs["agent checkpoints"].where == str(tmp_path / "agent" / "agent_memory.db")
    assert locs["agent checkpoints"].source == "data-root"
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_DB", "/data/r.db")  # an explicit path still wins
    assert sqlite_tier._resolve_db_path(review) == "/data/r.db"


def test_deployment_kind(monkeypatch):
    monkeypatch.setenv(datadir.DEPLOYMENT_ENV, "compose")
    assert datadir.deployment_kind() == "compose"
    monkeypatch.delenv(datadir.DEPLOYMENT_ENV)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert datadir.deployment_kind() == "kubernetes"


def test_backup_captures_data_root_content_and_restores_it(tmp_path, monkeypatch):
    from examlops.backup import create_bundle, restore_bundle

    root = tmp_path / "data"
    monkeypatch.setenv("EXAMLOPS_DATA_DIR", str(root))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "cli" / "config.toml"))
    datadir.init_layout(root, pack=_fake_pack(tmp_path / "pack"))
    (root / "site.toml").write_text('[features]\npreset = "minimal"\n')
    res = create_bundle(str(tmp_path / "bk"), tiers=["config"])
    names = {i.get("name") for i in res.manifest["tiers"]["config"]["items"]}
    assert {"data-root:site.toml", "data-root:usecase"} <= names

    (root / "site.toml").unlink()
    import shutil

    shutil.rmtree(root / "usecase")
    restore_bundle(res.bundle_dir, tiers=["config"])
    assert (root / "site.toml").read_text().startswith("[features]")
    assert (root / "usecase" / "models" / "demo.yaml").is_file()
