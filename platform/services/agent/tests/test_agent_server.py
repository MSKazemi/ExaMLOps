"""Entrypoint defaults keep a developer-launched agent on the local machine."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _entrypoint():
    path = Path(__file__).resolve().parents[1] / "agent_server.py"
    spec = importlib.util.spec_from_file_location("agent_server_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_loopback_bind_is_the_default():
    source = (Path(__file__).resolve().parents[1] / "agent_server.py").read_text()
    assert 'os.environ.get("AGENT_SERVER_HOST", "127.0.0.1")' in source


def test_remote_bind_warning_distinguishes_protected_and_open():
    entrypoint = _entrypoint()
    protected = entrypoint.exposure_warning("0.0.0.0", "secret")
    open_warning = entrypoint.exposure_warning("0.0.0.0", "")
    assert protected and "protects" in protected and "TLS" in protected
    assert open_warning and "without authentication" in open_warning
