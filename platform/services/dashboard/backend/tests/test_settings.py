"""Settings contract: new vars are required; legacy DASHBOARD_TOKEN is gone."""

import importlib
from pathlib import Path

import pytest


def _reload_settings():
    import settings as settings_module

    return importlib.reload(settings_module)


@pytest.fixture(autouse=True)
def restore_settings_module():
    """Tests in this file mutate env and reload `settings`. Without a
    teardown reload, downstream tests get the polluted module. After each
    test, force a clean reload using the conftest's env vars (which
    monkeypatch has already restored at this point)."""
    yield
    _reload_settings()


def test_settings_load_with_required_env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_VIEWER_PASSWORD", "v")
    monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", "a")
    monkeypatch.setenv("DASHBOARD_JWT_SECRET", "x" * 32)
    monkeypatch.setenv(
        "DASHBOARD_SECRET_KEY", "TVk4sP_ws6A6sRz38Kw1jJZX0d3Jcq3V0z0b6n6kE-c="
    )
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.chdir(Path("/tmp"))  # avoid picking up project .env

    mod = _reload_settings()
    assert mod.settings.dashboard_viewer_password == "v"
    assert mod.settings.dashboard_admin_password == "a"
    assert mod.settings.dashboard_jwt_secret == "x" * 32
    assert mod.settings.dashboard_jwt_ttl_hours == 12  # default
    assert mod.settings.dashboard_secret_key == \
        "TVk4sP_ws6A6sRz38Kw1jJZX0d3Jcq3V0z0b6n6kE-c="
    # Legacy field removed:
    assert not hasattr(mod.settings, "dashboard_token")


def test_settings_fail_fast_on_missing_required(monkeypatch):
    for k in (
        "DASHBOARD_VIEWER_PASSWORD",
        "DASHBOARD_ADMIN_PASSWORD",
        "DASHBOARD_JWT_SECRET",
        "DASHBOARD_SECRET_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(Path("/tmp"))
    with pytest.raises(Exception):  # pydantic ValidationError
        _reload_settings()
