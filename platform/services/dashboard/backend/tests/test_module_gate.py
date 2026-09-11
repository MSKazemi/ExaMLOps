"""Site feature profile in the dashboard (ADR 0128): routes 404, flags off, /api/v1/modules."""

import feature_flags as ff
import module_gate
import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture(autouse=True)
def _site(tmp_path, monkeypatch):
    """No site file, no data root, no env overlay — and a fresh profile cache each test."""
    monkeypatch.setenv("EXAMLOPS_SITE_PROFILE", str(tmp_path / "site.toml"))
    monkeypatch.delenv("EXAMLOPS_FEATURES", raising=False)
    monkeypatch.delenv("EXAMLOPS_DATA_DIR", raising=False)
    module_gate.reset_cache()
    yield
    module_gate.reset_cache()


def _features(monkeypatch, spec: str) -> None:
    monkeypatch.setenv("EXAMLOPS_FEATURES", spec)
    module_gate.reset_cache()


async def _token(client) -> str:
    r = await client.post("/api/auth/login", json={"password": VIEWER_PW})
    return r.json()["token"]


def test_path_ownership_respects_segment_boundaries():
    from examlops.lifecycle.modules import module_for_api_path

    assert module_for_api_path("/api/v1/finops/budgets") == "finops"
    assert module_for_api_path("/api/features/x") == "training"
    assert module_for_api_path("/api/feature-store/x") == "training"
    assert module_for_api_path("/api/featuresx") is None  # not a prefix match mid-segment
    assert module_for_api_path("/api/v1/projects") is None  # core surfaces are never gated


def test_default_profile_gates_nothing():
    assert module_gate.disabled_module_for_path("/api/v1/finops/budgets") is None


def test_disabled_module_is_reported_for_its_paths(monkeypatch):
    _features(monkeypatch, "-finops")
    assert module_gate.disabled_module_for_path("/api/v1/finops/budgets") == "finops"
    assert module_gate.disabled_module_for_path("/api/v1/projects") is None
    assert module_gate.disabled_module_for_path("/health") is None  # non-API paths untouched


@pytest.mark.asyncio
async def test_route_of_disabled_module_answers_404_module_disabled(client, monkeypatch):
    _features(monkeypatch, "-genai")
    token = await _token(client)
    r = await client.get("/api/v1/llmops/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "module_disabled" and body["module"] == "genai"


@pytest.mark.asyncio
async def test_route_of_enabled_module_is_not_intercepted(client, monkeypatch):
    _features(monkeypatch, "-finops")  # a different module is off
    token = await _token(client)
    r = await client.get("/api/v1/llmops/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.json().get("code") != "module_disabled"


def test_flag_of_disabled_module_evaluates_false_even_with_override(tmp_path, monkeypatch):
    from examlops import platform_db as pdb

    db = tmp_path / "p.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    assert ff.set_override(str(db), "facilityConsole", True, "admin") is True
    _features(monkeypatch, "preset:minimal")  # no hpc module
    decisions = ff.evaluate_all(str(db), role="admin", tenant="default", subject="u")
    assert decisions["facilityConsole"] is False
    assert decisions["mlopsConsole"] is True  # core flag unaffected
    view = {f["name"]: f for f in ff.admin_view(str(db))["flags"]}
    assert view["facilityConsole"]["module_enabled"] is False
    assert view["facilityConsole"]["effective"] is False


@pytest.mark.asyncio
async def test_modules_endpoint_lists_profile(client, monkeypatch):
    _features(monkeypatch, "preset:standard,+hpc")
    token = await _token(client)
    r = await client.get("/api/v1/modules", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["preset"] == "standard"
    state = {m["id"]: m["enabled"] for m in body["modules"]}
    assert state["hpc"] is True and state["genai"] is False and state["core"] is True
    # the navigation hides exactly the pages of the modules that are off
    assert "/serve/llmops" in body["disabled_pages"]  # genai: off under `standard`
    assert "/operate/facility" not in body["disabled_pages"]  # hpc: enabled on top
    assert "/operate/finops" not in body["disabled_pages"]  # finops: part of `standard`


@pytest.mark.asyncio
async def test_modules_endpoint_requires_login(client):
    r = await client.get("/api/v1/modules")
    assert r.status_code in (401, 403)
