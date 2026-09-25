"""ADR 0078 clause 1 — the read tranche of ``examlops.models`` returns typed objects.

MLflow is faked at the transport (``examlops.cli._client.get``), the same seam the CLI tests use,
so the SDK's URL building, paging and parsing all run for real.
"""

from __future__ import annotations

import pytest

import examlops
from examlops.cli import _client
from examlops.sdk import models
from examlops.sdk.errors import IncompleteReadError, NotFoundError, SDKError, UnavailableError

_PAGE_1 = {
    "registered_models": [
        {
            "name": "jpcp",
            "aliases": [{"alias": "Production", "version": "9"}],
            "latest_versions": [{"version": "9"}, {"version": "10"}],
        }
    ],
    "next_page_token": "p2",
}
_PAGE_2 = {"registered_models": [{"name": "mack", "aliases": [], "latest_versions": []}]}


def _registry(url, **_):
    if "registered-models/search" in url:
        return _PAGE_2 if "page_token=p2" in url else _PAGE_1
    raise AssertionError(url)


def test_list_reads_every_page_and_orders_versions_numerically(monkeypatch):
    monkeypatch.setattr(_client, "get", _registry)
    out = models.list()
    assert [m.name for m in out] == ["jpcp", "mack"], "the second page was dropped"
    jpcp = out[0]
    assert isinstance(jpcp, models.ModelSummary)
    # Lexically "9" > "10"; the newest version is 10.
    assert jpcp.latest_version == "10"
    assert jpcp.production_version == "9"
    assert out[1].latest_version is None and out[1].production_version is None


def test_the_namespace_is_reachable_from_the_package_root():
    assert examlops.models is models
    import examlops.models as by_import

    assert by_import is models


def test_list_refuses_a_partial_registry(monkeypatch):
    # A server that repeats its page token: the registry can never be read to the end.
    monkeypatch.setattr(_client, "get", lambda url, **_: {**_PAGE_1, "next_page_token": "p1"})
    with pytest.raises(IncompleteReadError):
        models.list()


def test_list_maps_an_unreachable_registry_to_unavailable(monkeypatch):
    def down(url, **_):
        raise _client.ClientError("Connection refused")

    monkeypatch.setattr(_client, "get", down)
    with pytest.raises(UnavailableError, match="Connection refused"):
        models.list()


def test_get_returns_the_record_and_404_is_not_found(monkeypatch):
    rm = {
        "name": "jpcp",
        "aliases": [{"alias": "Staging", "version": "4"}],
        "latest_versions": [{"version": "4"}],
    }
    monkeypatch.setattr(_client, "get", lambda url, **_: {"registered_model": rm})
    detail = models.get("jpcp")
    assert detail.aliases == {"Staging": "4"} and detail.versions == ["4"]
    assert detail.raw == rm

    def missing(url, **_):
        raise _client.ClientError("HTTP 404: RESOURCE_DOES_NOT_EXIST", status=404)

    monkeypatch.setattr(_client, "get", missing)
    with pytest.raises(NotFoundError) as info:
        models.get("nope")
    assert info.value.status == 404
    assert isinstance(info.value, SDKError)


def _mlflow_two_versions(url, **_):
    if "model-versions/get" in url:
        v = url.rsplit("version=", 1)[1]
        return {"model_version": {"run_id": f"run-{v}", "creation_timestamp": 1700}}
    if "run-17" in url:
        return {
            "run": {
                "data": {
                    "metrics": [{"key": "rmse", "value": 6.1}],
                    "params": [{"key": "lr", "value": "0.1"}],
                    "tags": [{"key": "dataset_revision", "value": "rev-a"}],
                }
            }
        }
    if "run-18" in url:
        return {
            "run": {
                "data": {
                    "metrics": [{"key": "rmse", "value": 4.9}, {"key": "mae", "value": 3.0}],
                    "params": [{"key": "lr", "value": "0.2"}],
                    "tags": [],
                }
            }
        }
    if "registered-models/get" in url:
        return {"registered_model": {"aliases": [{"alias": "Production", "version": "17"}]}}
    raise AssertionError(url)


def test_diff_pairs_each_metric_and_param(monkeypatch):
    monkeypatch.setattr(_client, "get", _mlflow_two_versions)
    d = models.diff("jpcp", "17", "18")
    assert d.metrics["rmse"] == models.ValuePair(6.1, 4.9)
    assert d.metrics["mae"] == models.ValuePair(None, 3.0), "absent in v1 must be None, not 0"
    assert d.to_dict()["params"]["lr"] == {"v1": "0.1", "v2": "0.2"}


def test_lineage_defaults_to_the_production_alias(monkeypatch):
    monkeypatch.setattr(_client, "get", _mlflow_two_versions)
    chain = models.lineage("jpcp")
    assert chain.model_version == "17"
    assert chain.run_id == "run-17"
    assert chain.dataset_version == "rev-a"
    assert chain.created_ms == 1700
    assert chain.to_dict()["prefect_flow_run_id"] == "unknown"


def test_lineage_of_a_model_without_aliases_is_not_found(monkeypatch):
    monkeypatch.setattr(_client, "get", lambda url, **_: {"registered_model": {"aliases": []}})
    with pytest.raises(NotFoundError, match="No versions found"):
        models.lineage("jpcp")


def test_cost_reads_recorded_history_oldest_first():
    from examlops.data import init_db
    from examlops.data.finops import record_model_cost

    init_db()
    record_model_cost("JPCP", 2, "r2", "j2", 3.0, 7.5, cpu_hours=1.0)
    record_model_cost("JPCP", 1, "r1", "j1", 1.0, 2.5)
    rows = models.cost("JPCP")
    assert [r.version for r in rows] == [1, 2]
    assert rows[1].gpu_hours == 3.0 and rows[1].cost_usd == 7.5 and rows[1].cpu_hours == 1.0
    assert models.cost("UNKNOWN") == []


def test_cli_models_list_renders_the_sdk_result(monkeypatch):
    """Clause 2: `exa models list` is a render over `examlops.models.list()`."""
    import json

    from typer.testing import CliRunner

    from examlops.cli.main import app

    monkeypatch.setattr(
        models,
        "list",
        lambda: [models.ModelSummary("only-from-sdk", "3", "5", {"Production": "3"})],
    )
    result = CliRunner().invoke(app, ["--json", "models", "list"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert "only-from-sdk" in json.dumps(doc)
