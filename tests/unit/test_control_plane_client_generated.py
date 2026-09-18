"""The /v1 client is generated from the API contract, and says exactly what the contract says.

Plan P1.6. Hand-built URLs are how a renamed route used to break its callers one by one; the client
is generated from ``api-contract.json``, which the service's own contract test holds the live app
to. These tests fail while the generated file is stale and pin the URLs, query strings, headers and
bodies it produces.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "gen_cp_client", REPO / "platform/ci/gen_cp_client.py"
)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)  # type: ignore[union-attr]

from examlops import control_plane_api as api  # noqa: E402


def test_the_committed_client_matches_the_contract():
    rendered = gen.render(json.loads(gen.CONTRACT.read_text(encoding="utf-8")))
    assert gen.OUTPUT.read_text(encoding="utf-8") == rendered, (
        "control_plane_api.py is stale — run `make openapi-export`"
    )


def test_every_v1_operation_has_a_client_function():
    contract = json.loads(gen.CONTRACT.read_text(encoding="utf-8"))
    v1_ops = {(m, p) for p, ms in contract["paths"].items() if p.startswith("/v1/") for m in ms}
    assert v1_ops == set(gen.NAMES)
    assert all(callable(getattr(api, name)) for name in gen.NAMES.values())


def test_a_new_v1_route_without_a_name_stops_the_generator():
    contract = json.loads(gen.CONTRACT.read_text(encoding="utf-8"))
    contract["paths"]["/v1/new"] = {
        "get": {"parameters": [], "request_body_required": None, "responses": ["200"]}
    }
    with pytest.raises(SystemExit, match="/v1/new"):
        gen.render(contract)


@pytest.fixture()
def calls(monkeypatch):
    from examlops.cli import _client

    seen: list[tuple] = []
    monkeypatch.setattr(
        _client, "get", lambda url, token="": seen.append(("GET", url, token)) or {}
    )
    monkeypatch.setattr(
        _client,
        "post",
        lambda url, body, token="", idempotency_key=None, **_: (
            seen.append(("POST", url, token, body, idempotency_key)) or {}
        ),
    )
    monkeypatch.setattr(
        _client, "delete", lambda url, token=None: seen.append(("DELETE", url, token)) or {}
    )
    monkeypatch.setattr(
        _client, "put", lambda url, body, token=None: seen.append(("PUT", url, token, body)) or {}
    )
    return seen


def test_path_parameters_are_escaped(calls):
    api.approve("JP CP/1", base="http://cp:8002/", token="t")
    assert calls == [("POST", "http://cp:8002/v1/approvals/JP%20CP%2F1/approve", "t", {}, None)]


def test_query_parameters_are_sent_only_when_given(calls):
    api.list_commands(state="dead", limit=5, base="http://cp", token="t")
    api.list_commands(base="http://cp", token="t")
    assert calls[0][1] == "http://cp/v1/commands?limit=5&state=dead"
    assert calls[1][1] == "http://cp/v1/commands"


def test_bodies_and_the_idempotency_key_are_passed_through(calls):
    api.submit_retrain(
        body={"model_name": "JPCP"}, idempotency_key="k-1", base="http://cp", token="t"
    )
    api.reject("JPCP", body={"reason": "data review"}, base="http://cp", token="t")
    assert calls[0] == ("POST", "http://cp/v1/retrain", "t", {"model_name": "JPCP"}, "k-1")
    assert calls[1][3] == {"reason": "data review"}


def test_base_and_token_default_to_the_cli_configuration(calls, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://configured:18002")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "configured-token")
    api.run_status("55bec2da")
    assert calls == [("GET", "http://configured:18002/v1/runs/55bec2da", "configured-token")]
