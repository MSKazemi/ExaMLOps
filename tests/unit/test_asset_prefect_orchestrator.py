# tests/unit/test_asset_prefect_orchestrator.py
"""ADR 0036 clause 1 — "a thin asset layer generating **Prefect** runs", behind the seam.

The recorded finding: the seam had two implementations, local and the HPC scheduler, and clause 1
offers "either Dagster or a thin asset layer generating Prefect runs" — neither existed. These
tests run real Prefect flow runs against a throwaway Prefect server (`prefect_test_harness`), and
hold the one rule that makes an optional engine safe: Prefect's absence falls back to a local
build and says so, while the *asset's* own failure propagates exactly as it does under `local`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

prefect = pytest.importorskip("prefect")

from prefect.settings import PREFECT_API_URL, temporary_settings  # noqa: E402

from examlops import assets  # noqa: E402
from examlops.assets import (  # noqa: E402
    AssetOrchestrator,
    PrefectOrchestrator,
    declare_asset,
    get_orchestrator,
    materialize,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "EXAMLOPS_ASSET_ORCHESTRATOR",
        "EXAMLOPS_ASSET_PREFECT_RETRIES",
        "EXAMLOPS_ASSET_PREFECT_RETRY_DELAY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="module")
def prefect_server():
    """A real, throwaway Prefect API (temporary database) for this module's flow runs."""
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        yield


def _def(name, fn):
    class _Def:
        pass

    d = _Def()
    d.name, d.fn = name, fn
    return d


def _flow_run(flow_run_id):
    from prefect.client.orchestration import get_client

    with get_client(sync_client=True) as client:
        return client.read_flow_run(flow_run_id)


# ── the seam ──────────────────────────────────────────────────────────────────


def test_prefect_is_a_third_implementation_of_the_seam(monkeypatch):
    assert isinstance(PrefectOrchestrator(), AssetOrchestrator)
    monkeypatch.setenv("EXAMLOPS_ASSET_ORCHESTRATOR", "prefect")
    assert get_orchestrator().name == "prefect"
    assert get_orchestrator("PREFECT").name == "prefect"


# ── a build is a real Prefect flow run ────────────────────────────────────────


def test_a_build_is_a_completed_prefect_flow_run_named_for_the_asset(prefect_server):
    calls = []
    result = PrefectOrchestrator().run(_def("pf_features", lambda **kw: calls.append(kw)), {"u": 3})

    assert calls == [{"u": 3}], "the production fn runs once, with its upstream versions"
    assert "fallback" not in result
    run = _flow_run(result["prefect_flow_run_id"])
    assert run.name == "asset:pf_features"
    assert run.state.is_completed()


def test_the_assets_own_failure_propagates_and_is_not_retried_by_default(prefect_server):
    """Falling back to local here would run a failing production function a second time and
    report the asset built by 'prefect' when it was not."""
    calls = []

    def fn(**kw):
        calls.append(1)
        raise ValueError("bad input partition")

    with pytest.raises(ValueError, match="bad input partition"):
        PrefectOrchestrator().run(_def("pf_bad", fn), {})
    assert calls == [1]


def test_retries_are_opt_in(prefect_server, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_ASSET_PREFECT_RETRIES", "1")
    monkeypatch.setenv("EXAMLOPS_ASSET_PREFECT_RETRY_DELAY", "0")
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient")

    result = PrefectOrchestrator().run(_def("pf_flaky", flaky), {})

    assert calls == [1, 1]
    assert _flow_run(result["prefect_flow_run_id"]).state.is_completed()


def test_materialize_bumps_once_and_links_the_version_to_its_flow_run(prefect_server, monkeypatch):
    declare_asset("PfSeam", kind="model")
    monkeypatch.setitem(
        assets._REGISTRY, "PfSeam", assets.AssetDef(name="PfSeam", kind="model", deps=[], fn=None)
    )

    materialize("PfSeam", force=True, orchestrator="prefect")

    from examlops.platform_db import get_asset, get_db

    assert get_asset("PfSeam")["current_version"] == 1, "one build, one version — no double bump"
    with get_db() as conn:
        row = conn.execute(
            "SELECT facets_json FROM lineage_events WHERE job='asset:PfSeam' ORDER BY id DESC"
        ).fetchone()
    facets = str(row["facets_json"])
    assert "prefect" in facets and "prefect_flow_run_id" in facets
    assert "fallback" not in facets


# ── Prefect's absence is an environment fact, not an asset failure ───────────


def test_no_prefect_api_builds_locally_and_says_why():
    """Rather than let Prefect start an ephemeral server whose runs nobody can see."""
    calls = []
    with temporary_settings(restore_defaults={PREFECT_API_URL}):
        result = PrefectOrchestrator().run(_def("pf_noapi", lambda **kw: calls.append(1)), {})

    assert calls == [1], "the asset must still get built"
    assert "PREFECT_API_URL" in result["fallback"]
    assert "prefect_flow_run_id" not in result


def test_an_unreachable_prefect_server_builds_locally_and_says_why():
    calls = []
    with temporary_settings(updates={PREFECT_API_URL: "http://127.0.0.1:1/api"}):
        result = PrefectOrchestrator().run(_def("pf_down", lambda **kw: calls.append(1)), {})

    assert calls == [1], "built exactly once — locally, because the flow never started"
    assert result["fallback"].startswith("local (Prefect unavailable")


def test_a_missing_prefect_package_builds_locally(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_prefect(name, *args, **kwargs):
        if name == "prefect" or name.startswith("prefect."):
            raise ImportError("No module named 'prefect'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_prefect)
    calls = []
    result = PrefectOrchestrator().run(_def("pf_nopkg", lambda **kw: calls.append(1)), {})

    assert calls == [1]
    assert "not installed" in result["fallback"]
