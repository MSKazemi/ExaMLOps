"""The control plane accepts SPIFFE JWT-SVIDs from mapped workloads (ADR 0125 phase 1).

A workload sends a short-lived JWT-SVID instead of a static secret. The control plane verifies
it against the trust bundle and gives it exactly the scopes its SPIFFE ID is mapped to in
``CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON``. An SVID that fails verification, or names a
workload nobody mapped, is refused and never handed on to the IdP path.
"""

from __future__ import annotations

import importlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm

DOMAIN = "examlops.internal"
STATIC = "-".join(("static", "token", "0123456789"))


@pytest.fixture()
def world(tmp_path, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = {**json.loads(ECAlgorithm.to_jwk(key.public_key())), "kid": "k1", "use": "jwt-svid"}
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"keys": [jwk]}))
    monkeypatch.setenv("EXAMLOPS_SPIFFE_TRUST_DOMAIN", DOMAIN)
    monkeypatch.setenv("EXAMLOPS_SPIFFE_BUNDLE", str(bundle))
    monkeypatch.setenv(
        "CONTROL_PLANE_WORKLOAD_IDENTITIES_JSON",
        json.dumps(
            {
                f"spiffe://{DOMAIN}/autopilot": {
                    "principal": "autopilot",
                    "tenant": "default",
                    "scopes": ["read", "retrain"],
                }
            }
        ),
    )
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", STATIC)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "wi.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_WORKERS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    return {"cp": cp_app, "key": key, "client": TestClient(cp_app.app)}


def _svid(key, *, path="autopilot", aud="control-plane", exp=300, domain=DOMAIN) -> str:
    claims = {"sub": f"spiffe://{domain}/{path}", "aud": [aud], "exp": int(time.time()) + exp}
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": "k1"})


def _get(world, token: str, path: str = "/v1/commands"):
    return world["client"].get(path, headers={"Authorization": f"Bearer {token}"})


def test_a_mapped_workload_acts_with_its_own_scopes(world):
    assert _get(world, _svid(world["key"])).status_code == 200  # read
    denied = world["client"].put(
        "/v1/modelzoo/config",
        json={"auto_retrain": True},
        headers={"Authorization": f"Bearer {_svid(world['key'])}"},
    )
    assert denied.status_code == 403 and "admin" in denied.text  # not mapped to admin


def test_the_request_is_attributed_to_the_workload(world):
    context = world["cp"]._request_context(f"Bearer {_svid(world['key'])}")
    assert (context.principal, context.tenant) == ("autopilot", "default")
    assert context.scopes == frozenset({"read", "retrain"})
    assert not context.is_legacy


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"path": "someone-else"}, "not a known workload"),
        ({"aud": "dashboard"}, "Invalid workload identity"),
        ({"exp": -120}, "Invalid workload identity"),
        ({"domain": "another.domain"}, "Invalid workload identity"),
    ],
)
def test_a_bad_or_unmapped_svid_is_refused(world, kw, reason):
    answer = _get(world, _svid(world["key"], **kw))
    assert answer.status_code == 403 and reason in answer.text


def test_static_credentials_keep_working_alongside(world):
    assert _get(world, STATIC).status_code == 200


def _auth_count(principal: str, method: str) -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(
        "control_plane_authentications_total", {"principal": principal, "method": method}
    )
    return -1.0 if value is None else value


def test_each_authentication_is_counted_by_principal_and_method(world):
    """The signal for retiring a service's static secret: its `static` series stops growing."""
    assert _auth_count("autopilot", "workload") >= 0  # exported before the first use
    assert _auth_count("legacy", "legacy") >= 0
    before = (_auth_count("autopilot", "workload"), _auth_count("legacy", "legacy"))
    assert _get(world, _svid(world["key"])).status_code == 200
    assert _get(world, STATIC).status_code == 200
    assert _auth_count("autopilot", "workload") == before[0] + 1
    assert _auth_count("legacy", "legacy") == before[1] + 1


class _Gateway:
    def find_deployment_id(self, _name: str) -> str:
        return "deployment-1"

    def create_flow_run(self, _deployment_id, _parameters, *, idempotency_key=None) -> str:
        return "flow-1"


def test_the_audit_names_the_workload_that_acted(world, monkeypatch):
    cp = world["cp"]
    monkeypatch.setattr(cp, "_get_registry", lambda: {"JPCP": ["PM100Dataset"]})
    monkeypatch.setattr(cp, "_get_gateway", lambda: _Gateway())
    body = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}
    answer = world["client"].post(
        "/retrain", json=body, headers={"Authorization": f"Bearer {_svid(world['key'])}"}
    )
    assert answer.status_code == 200, answer.text
    conn = cp._get_db()
    try:
        actor, details = conn.execute(
            "SELECT actor, details FROM audit_events WHERE action='retrain_dispatched'"
        ).fetchone()
    finally:
        conn.close()
    assert actor == "autopilot"
    recorded = json.loads(details)
    assert recorded["credential"] == "workload"
    assert recorded["spiffe_id"] == f"spiffe://{DOMAIN}/autopilot"


def test_a_static_credential_is_audited_as_such(world):
    context = world["cp"]._request_context(f"Bearer {STATIC}")
    assert world["cp"]._credential_details(context) == {"credential": "legacy"}


def test_mapped_workloads_without_a_bundle_fail_the_startup_check(world, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_SPIFFE_BUNDLE")
    world["cp"]._run_startup_checks()
    assert world["cp"]._startup_checks["workload_identity"].startswith("fail")


def test_a_malformed_map_is_reported(tmp_path, monkeypatch, world):
    cp = world["cp"]
    workloads, error = cp._parse_workload_identities(json.dumps({"autopilot": {}}))
    assert workloads == {} and "SPIFFE ID" in error
    _, error = cp._parse_workload_identities(
        json.dumps({f"spiffe://{DOMAIN}/x": {"principal": "x", "tenant": "t", "scopes": ["root"]}})
    )
    assert "scopes" in error


def test_a_queued_command_is_audited_with_the_credential_that_submitted_it(world, monkeypatch):
    """/v1 commands are dispatched later by a worker, which has no request; the kind travels."""
    cp = world["cp"]
    monkeypatch.setattr(cp, "_get_registry", lambda: {"JPCP": ["PM100Dataset"]})
    monkeypatch.setattr(cp, "_get_gateway", lambda: _Gateway())
    body = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}
    headers = {"Authorization": f"Bearer {_svid(world['key'])}", "Idempotency-Key": "q-1"}
    first = world["client"].post("/v1/retrain", json=body, headers=headers)
    assert first.status_code == 202, first.text
    # The autopilot retries the same submission with its own static secret (its SVID file was
    # gone): same principal, same key, so the same command, not a conflicting request.
    own_static = "-".join(("autopilot", "static", "0123456789"))
    monkeypatch.setitem(
        cp._structured_credentials,
        own_static,
        cp.RequestContext("autopilot", "default", frozenset({"read", "retrain"})),
    )
    again = world["client"].post(
        "/v1/retrain", json=body, headers={**headers, "Authorization": f"Bearer {own_static}"}
    )
    assert again.status_code == 202, again.text
    assert again.json()["command_id"] == first.json()["command_id"]
    assert cp._work_commands_once() == 1
    conn = cp._get_db()
    try:
        details = conn.execute(
            "SELECT details FROM audit_events WHERE action='retrain_dispatched'"
        ).fetchone()[0]
    finally:
        conn.close()
    recorded = json.loads(details)
    assert recorded["credential"] == "workload"
    assert recorded["spiffe_id"] == f"spiffe://{DOMAIN}/autopilot"
