from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli._config import load_config


def test_defaults(tmp_path, monkeypatch):
    # Hermetic: ignore any real ~/.config/examlops/config.toml on the dev machine.
    monkeypatch.setattr("examlops.cli._config.CONFIG_PATH", tmp_path / "nonexistent.toml")
    with patch.dict(os.environ, {}, clear=True):
        cfg = load_config()
    assert cfg.control_plane_url == "http://localhost:18002"
    assert cfg.ray_serve_url == "http://localhost:18001"
    assert cfg.mlflow_url == "http://localhost:15000"
    assert cfg.prefect_url == "http://localhost:14200"
    assert cfg.control_plane_token == ""

def test_env_overrides_defaults():
    env = {"CONTROL_PLANE_URL": "http://n1:18002", "CONTROL_PLANE_TOKEN": "secret"}
    with patch.dict(os.environ, env):
        cfg = load_config()
    assert cfg.control_plane_url == "http://n1:18002"
    assert cfg.control_plane_token == "secret"

def test_toml_overrides_defaults(tmp_path, monkeypatch):
    toml_content = b'[urls]\ncontrol_plane = "http://remote:18002"\n[auth]\ncontrol_plane_token = "tok"\n'
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_bytes(toml_content)
    monkeypatch.setattr("examlops.cli._config.CONFIG_PATH", cfg_file)
    with patch.dict(os.environ, {}, clear=False):
        cfg = load_config()
    assert cfg.control_plane_url == "http://remote:18002"
    assert cfg.control_plane_token == "tok"

def test_env_overrides_toml(tmp_path, monkeypatch):
    toml_content = b'[urls]\ncontrol_plane = "http://toml:18002"\n'
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_bytes(toml_content)
    monkeypatch.setattr("examlops.cli._config.CONFIG_PATH", cfg_file)
    env = {"CONTROL_PLANE_URL": "http://env:18002"}
    with patch.dict(os.environ, env):
        cfg = load_config()
    assert cfg.control_plane_url == "http://env:18002"
