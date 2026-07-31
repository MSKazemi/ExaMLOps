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
    assert st.pending_approvals == 0
    assert st.production_models == []


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
