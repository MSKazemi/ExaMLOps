"""Unit tests for the stable examlops SDK facade (INC-2 / ADR 0078)."""

from __future__ import annotations

import examlops
from examlops import sdk


def test_public_surface_exports():
    """The documented public surface is importable from the package root."""
    for name in ("status", "place", "list_providers", "resolve_provider", "api_version"):
        assert hasattr(examlops, name), f"missing public export: {name}"
    assert isinstance(examlops.__all__, list)
    assert "status" in examlops.__all__


def test_version_and_api_version():
    assert isinstance(examlops.__version__, str) and examlops.__version__
    assert examlops.api_version() == "0.1"


def test_status_returns_typed_object_when_unreachable(monkeypatch):
    """status() returns a typed PlatformStatus (not a bare dict), degrading when unreachable."""
    from examlops.cli import _client

    def boom(*a, **k):
        raise _client.ClientError("unreachable")

    monkeypatch.setattr(_client, "get", boom)
    st = sdk.status()
    assert isinstance(st, sdk.PlatformStatus)
    assert st.reachable is False
    assert st.services == {}
    # Unknown, not zero. This assertion used to read `== 0` — the value that means "the approval
    # queue is empty", which a control plane nobody could reach has not told us. The endpoint
    # stopped fabricating it; the facade's failure path was still putting it back.
    assert st.pending_approvals is None
    assert st.production_models is None


def test_status_parses_typed_services(monkeypatch):
    from examlops.cli import _client

    payload = {
        "services": {"mlflow": {"ok": True}, "prefect": {"ok": False}},
        "pending_approvals": 2,
        "production_models": [{"name": "JPCP", "production_version": "17"}],
    }
    monkeypatch.setattr(_client, "get", lambda *a, **k: payload)
    st = sdk.status()
    assert st.reachable is True
    assert st.services["mlflow"].ok is True
    assert st.services["prefect"].ok is False
    assert isinstance(st.services["mlflow"], sdk.ServiceHealth)
    assert st.pending_approvals == 2
    assert st.production_models[0]["name"] == "JPCP"


# ── production models come from the registry, because /status has never carried them ────────────
#
# The payload above is the one this suite invented. The live endpoint returns exactly two keys —
# `services` and `pending_approvals` — verified against the running app, and `tests/unit/
# test_control_plane_status_contract.py` pins that so this can never silently drift back. Reading a
# key nobody sends made `production_models` an empty list on every real call, and an empty list
# renders as nothing at all: the platform looked like it had no models in production.

_LIVE_STATUS = {
    "services": {"mlflow": {"ok": True, "url": "http://mlflow:5000/health"}},
    "pending_approvals": 0,
}
_REGISTRY = {
    "registered_models": [
        {
            "name": "jpcp",
            "aliases": [
                {"alias": "Production", "version": "17"},
                {"alias": "Staging", "version": "18"},
            ],
        },
        {"name": "mack", "aliases": [{"alias": "Staging", "version": "4"}]},
        {"name": "mcbound", "aliases": []},
    ]
}


def _route(monkeypatch, registry):
    """Answer /status with the live shape and the registry search with `registry`."""
    from examlops.cli import _client

    def get(url, *a, **k):
        if "registered-models/search" in url:
            if isinstance(registry, Exception):
                raise registry
            return registry
        return _LIVE_STATUS

    monkeypatch.setattr(_client, "get", get)


def test_production_models_are_read_from_the_registry(monkeypatch):
    _route(monkeypatch, _REGISTRY)
    st = sdk.status()
    by_name = {m["name"]: m for m in st.production_models}
    assert set(by_name) == {"jpcp", "mack"}, "a model with no lifecycle alias is not in production"
    assert by_name["jpcp"]["production_version"] == "17"
    assert by_name["jpcp"]["staging_version"] == "18"
    assert by_name["mack"]["production_version"] is None


def test_an_unreadable_registry_is_unknown_not_empty(monkeypatch):
    from examlops.cli import _client

    _route(monkeypatch, _client.ClientError("registry down"))
    assert sdk.status().production_models is None


def test_a_registry_with_no_aliases_is_measured_and_empty(monkeypatch):
    _route(monkeypatch, {"registered_models": [{"name": "mcbound", "aliases": []}]})
    assert sdk.status().production_models == [], "measured-and-none is a list, not None"


def test_a_down_mlflow_is_not_probed_twice(monkeypatch):
    """The status ping already said MLflow is down; asking again buys a timeout, not an answer."""
    from examlops.cli import _client

    asked = []

    def get(url, *a, **k):
        asked.append(url)
        return {"services": {"mlflow": {"ok": False}}, "pending_approvals": 0}

    monkeypatch.setattr(_client, "get", get)
    st = sdk.status()
    assert st.production_models is None
    assert not any("registered-models" in u for u in asked), asked


def test_place_routes_through_placement_provider(monkeypatch):
    """sdk.place() returns a PlacementResult using the pluggable scorer, over injected inventory."""
    import examlops.sdk as sdkmod

    clusters = [
        {"name": "busy", "scheduler": "flux", "nodes": [{"state": "idle", "gpus": 1, "cpus": 8}]},
        {
            "name": "free",
            "scheduler": "slurm",
            "nodes": [{"state": "idle", "gpus": 8, "cpus": 8}],
        },
    ]
    monkeypatch.setattr(
        "examlops.hpc_registry.active_clusters_with_inventory", lambda: clusters, raising=False
    )
    result = sdkmod.place(gpus=2)
    assert result.cluster == "free"
    assert hasattr(result, "candidates")


def test_list_providers_reexport():
    infos = sdk.list_providers("placement")
    names = {i.name for i in infos}
    assert "least-loaded" in names


# ── model-zoo onboarding surface (shared by CLI/Dashboard/Jupyter) ────────────────────────────────
def test_onboarding_functions_are_exported():
    for name in ("list_zoo_models", "onboard_model", "onboard_all_models"):
        assert name in sdk.__all__ and hasattr(sdk, name)


def test_onboard_model_delegates_and_reports_steps(tmp_path, monkeypatch):
    import examlops.usecase as usecase

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    models = tmp_path / "models"
    models.mkdir()
    (models / "jpcp.yaml").write_text("name: JPCP\nenabled: true\n")
    monkeypatch.setattr(usecase, "models_dir", lambda default=None: models)

    assert sdk.list_zoo_models() == ["JPCP"]
    # dry-run: reports steps, persists nothing
    out = sdk.onboard_model("JPCP", dry_run=True)
    assert out["project"] == "jpcp" and out["steps"]["project"] == "would-create"
    from examlops.data.projects import get_project

    assert get_project("jpcp") is None
    # real onboarding creates the project + records a per-model result
    out2 = sdk.onboard_model("JPCP")
    assert out2["changed"] is True and get_project("jpcp") is not None


def test_onboard_all_models_covers_pack(tmp_path, monkeypatch):
    import examlops.usecase as usecase

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    models = tmp_path / "models"
    models.mkdir()
    (models / "jpcp.yaml").write_text("name: JPCP\nenabled: true\n")
    (models / "mack.yaml").write_text("name: MACK\nenabled: true\n")
    monkeypatch.setattr(usecase, "models_dir", lambda default=None: models)

    results = sdk.onboard_all_models()
    assert {r["project"] for r in results} == {"jpcp", "mack"}
