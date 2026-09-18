"""The serving gateway's authorization decision (plan P4.4, ADR 0126 decision 2).

``examlops.serving_gateway.decide`` is what Envoy's ``ext_authz`` asks for every request. These
tests pin its rules without Envoy (``tests/integration/test_serving_gateway_live.py`` runs the real
proxy): open health paths, a credential for everything else, virtual keys with their allow-list and
budget, IdP tokens through the platform policy, the per-tenant quota, the identity headers an
allowed request always carries, and the static-stability behaviour when the datastore is gone.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from examlops import gateway, serving_gateway
from examlops.serving_gateway import PRINCIPAL_HEADER, PROJECT_HEADER, TENANT_HEADER, decide

INFER = "/v2/models/jpcp/infer"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    serving_gateway._key_cache.clear()
    monkeypatch.setenv("EXAMLOPS_GATEWAY_TENANT_RPM", "0")  # quota off unless a test sets it


def _key(models=None, budget=None, tenant="acme", project="research") -> str:
    return gateway.issue_virtual_key(tenant, project, models, budget, "test")


# ─── what needs a credential ─────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/v2", "/v2/health/live", "/v2/health/ready"])
def test_health_and_server_metadata_are_open(path):
    verdict = decide("GET", path, None)
    assert verdict.allowed
    # Identity headers are still set (empty), so a client's own copy is overwritten.
    assert verdict.headers == {TENANT_HEADER: "", PRINCIPAL_HEADER: "", PROJECT_HEADER: ""}


@pytest.mark.parametrize(
    ("method", "path", "auth"),
    [
        ("POST", INFER, None),
        ("POST", INFER, "Basic dXNlcjpwYXNz"),
        ("POST", INFER, "Bearer "),
        ("GET", "/v2/models/jpcp", None),  # model metadata is not open
        ("POST", "/v2/health/live", None),  # only GET/HEAD of health is open
    ],
)
def test_everything_else_needs_a_bearer_credential(method, path, auth):
    verdict = decide(method, path, auth)
    assert verdict.status == 401
    assert verdict.headers.get("www-authenticate", "").startswith("Bearer")


# ─── virtual keys ─────────────────────────────────────────────────────────────


def test_a_virtual_key_is_allowed_with_its_tenant_and_project():
    verdict = decide("POST", INFER, f"Bearer {_key()}")
    assert verdict.allowed
    assert verdict.headers[TENANT_HEADER] == "acme"
    assert verdict.headers[PROJECT_HEADER] == "research"
    assert verdict.headers[PRINCIPAL_HEADER].startswith("key:")


def test_an_unknown_key_is_401():
    fake = "-".join(("exa", "not", "issued"))
    assert decide("POST", INFER, f"Bearer {fake}").status == 401


def test_an_allow_list_is_enforced_per_model():
    key = _key(models=["jpcp"])
    assert decide("POST", INFER, f"Bearer {key}").allowed
    assert decide("POST", "/v2/models/other/infer", f"Bearer {key}").status == 403
    # A route that names no model cannot be checked against an allow-list, so it is refused.
    refused = decide("POST", "/infer-pipeline/infer", f"Bearer {key}")
    assert refused.status == 403 and "limited to models on its allow-list" in refused.reason


def test_an_exhausted_budget_is_403():
    from examlops.data.gateway import get_virtual_key
    from examlops.platform_db import get_db

    key = _key(budget=0.01)
    digest = serving_gateway.hashlib.sha256(key.encode()).hexdigest()
    assert get_virtual_key(digest) is not None
    with get_db() as conn:
        conn.execute("UPDATE virtual_keys SET spent_usd=1.0 WHERE key_hash=?", (digest,))
    verdict = decide("POST", INFER, f"Bearer {key}")
    assert verdict.status == 403 and "budget" in verdict.reason


def test_a_verified_key_rides_out_a_datastore_outage_for_a_while(monkeypatch):
    key = _key()
    assert decide("POST", INFER, f"Bearer {key}").allowed  # verified once: cached

    def down(*_a, **_k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(gateway, "authorize", down)
    assert decide("POST", INFER, f"Bearer {key}").allowed
    monkeypatch.setenv("EXAMLOPS_GATEWAY_KEY_CACHE_SECONDS", "0")
    assert decide("POST", INFER, f"Bearer {key}").status == 503  # the cache has limits
    never_seen = "-".join(("exa", "never", "verified"))
    monkeypatch.setenv("EXAMLOPS_GATEWAY_KEY_CACHE_SECONDS", "60")
    assert decide("POST", INFER, f"Bearer {never_seen}").status == 503  # never allowed blind


# ─── per-tenant quota ─────────────────────────────────────────────────────────


def test_the_tenant_quota_is_shared_and_refused_with_429(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GATEWAY_TENANT_RPM", "2")
    first, second = _key(), _key()  # two keys, one tenant: one budget
    assert decide("POST", INFER, f"Bearer {first}").allowed
    assert decide("POST", INFER, f"Bearer {second}").allowed
    refused = decide("POST", INFER, f"Bearer {first}")
    assert refused.status == 429 and refused.headers["retry-after"] == "60"
    assert decide("POST", INFER, f"Bearer {_key(tenant='other')}").allowed  # its own budget


def test_an_unreachable_quota_does_not_take_inference_down(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_GATEWAY_TENANT_RPM", "2")
    import examlops.coordination as coordination

    def broken():
        raise RuntimeError("coordinator down")

    monkeypatch.setattr(coordination, "get_coordinator", broken)
    assert decide("POST", INFER, f"Bearer {_key()}").allowed


# ─── IdP access tokens ────────────────────────────────────────────────────────


def test_a_token_without_an_identity_provider_is_401(monkeypatch):
    import examlops.iam.config as iam_config

    monkeypatch.setattr(
        iam_config, "load_config", lambda *a, **k: type("C", (), {"providers": ()})()
    )
    verdict = decide("POST", INFER, "Bearer a.b.c")
    assert verdict.status == 401 and "virtual key" in verdict.reason


@pytest.mark.parametrize("allowed", [True, False])
def test_a_token_is_authorized_by_the_platform_policy(monkeypatch, allowed):
    import examlops.iam.config as iam_config
    from examlops.iam import pdp, tokens

    principal = tokens.Principal(
        provider="center",
        issuer="https://idp",
        subject="u1",
        tenant="acme",
        role="viewer",
        username="alice",
    )
    monkeypatch.setattr(
        iam_config, "load_config", lambda *a, **k: type("C", (), {"providers": (1,)})()
    )
    monkeypatch.setattr(tokens, "verify_access_token", lambda token, config: principal)
    seen: dict = {}

    def authorize(p, action, resource, **kw):
        seen.update(action=action, resource=resource)
        return pdp.Decision(allowed, "policy says so")

    monkeypatch.setattr(pdp, "authorize", authorize)
    verdict = decide("POST", INFER, "Bearer a.b.c")
    assert seen == {"action": "serving.infer", "resource": {"type": "model", "id": "jpcp"}}
    if allowed:
        assert verdict.allowed and verdict.headers[TENANT_HEADER] == "acme"
        assert verdict.headers[PRINCIPAL_HEADER] == principal.actor
    else:
        assert verdict.status == 403


def test_serving_infer_is_open_to_any_authenticated_role():
    from examlops.iam.pdp import min_role_for

    assert min_role_for("serving.infer") == "viewer"


# ─── the ext_authz service ────────────────────────────────────────────────────


def test_the_service_answers_envoy():
    client = TestClient(serving_gateway.create_app())
    key = _key()
    allowed = client.post(f"/check{INFER}", headers={"authorization": f"Bearer {key}"})
    assert allowed.status_code == 200
    assert allowed.headers[TENANT_HEADER] == "acme"
    denied = client.post(f"/check{INFER}")
    assert denied.status_code == 401 and denied.json() == {
        "error": "a bearer credential is required"
    }
    metrics = client.get("/metrics").text
    assert 'examlops_gateway_decisions_total{status="200"} 1' in metrics
    assert 'examlops_gateway_decisions_total{status="401"} 1' in metrics


# ─── project scope at serve time (plan P4.9) ──────────────────────────────────


@pytest.fixture()
def tenancy(monkeypatch):
    """Multi-tenancy on; JPCP (registry spelling) belongs to project `research`."""
    from examlops.data.projects import assign_model_to_project, create_project

    monkeypatch.setenv("EXAMLOPS_MULTITENANCY", "1")
    serving_gateway._project_cache.clear()
    create_project("research")
    create_project("other")
    assert assign_model_to_project("research", "JPCP")


def test_a_key_of_the_models_project_is_allowed(tenancy):
    verdict = decide("POST", INFER, f"Bearer {_key(project='research')}")  # jpcp, lower-case
    assert verdict.allowed and verdict.headers[PROJECT_HEADER] == "research"


def test_a_cross_project_key_is_refused(tenancy):
    verdict = decide("POST", INFER, f"Bearer {_key(project='other')}")
    assert verdict.status == 403 and "belongs to project 'research'" in verdict.reason


def test_an_unscoped_model_is_open_to_every_project(tenancy):
    assert decide("POST", "/v2/models/unowned/infer", f"Bearer {_key(project='other')}").allowed


def test_a_route_that_names_no_model_cannot_be_scoped(tenancy):
    verdict = decide("POST", "/infer-pipeline/infer", f"Bearer {_key(project='research')}")
    assert verdict.status == 403 and "multi-tenancy" in verdict.reason


def test_without_multitenancy_projects_do_not_restrict(tenancy, monkeypatch):
    monkeypatch.delenv("EXAMLOPS_MULTITENANCY")
    assert decide("POST", INFER, f"Bearer {_key(project='other')}").allowed


def _token_principal(monkeypatch, projects=None):
    import examlops.iam.config as iam_config
    from examlops.iam import pdp, tokens

    principal = tokens.Principal(
        provider="center",
        issuer="https://idp",
        subject="u1",
        tenant="acme",
        role="viewer",
        projects=dict(projects or {}),
    )
    monkeypatch.setattr(
        iam_config, "load_config", lambda *a, **k: type("C", (), {"providers": (1,)})()
    )
    monkeypatch.setattr(tokens, "verify_access_token", lambda token, config: principal)
    seen: dict = {}

    def authorize(p, action, resource, **kw):
        seen.update(resource)
        return pdp.Decision(True, "ok")

    monkeypatch.setattr(pdp, "authorize", authorize)
    return principal, seen


def test_a_token_needs_a_role_in_the_models_project(tenancy, monkeypatch):
    _token_principal(monkeypatch)
    refused = decide("POST", INFER, "Bearer a.b.c")
    assert refused.status == 403 and "not in" in refused.reason


def test_a_project_role_from_the_token_admits_it(tenancy, monkeypatch):
    _, seen = _token_principal(monkeypatch, projects={"research": "viewer"})
    verdict = decide("POST", INFER, "Bearer a.b.c")
    assert verdict.allowed and verdict.headers[PROJECT_HEADER] == "research"
    assert seen["project"] == "research"  # the policy sees the project too


def test_a_d6_relation_on_the_project_admits_it(tenancy, monkeypatch):
    from examlops.data.projects import add_project_member

    principal, _ = _token_principal(monkeypatch)
    add_project_member("research", principal.id, "viewer")
    assert decide("POST", INFER, "Bearer a.b.c").allowed


def test_an_unreadable_scope_is_refused_not_opened(tenancy, monkeypatch):
    key = _key(project="other")

    def down(_model):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(serving_gateway, "_project_of", down)
    serving_gateway._project_cache.clear()
    monkeypatch.setattr(gateway, "authorize", lambda *a: {"tenant": "acme", "project": "other"})
    assert decide("POST", INFER, f"Bearer {key}").status == 503


def test_every_decision_status_is_exported_before_it_first_happens():
    """rate() over a series with no earlier sample is 0: a 503 first exported at 1 would hide the
    start of an outage from ServingGatewayCredentialStoreUnavailable."""
    metrics = TestClient(serving_gateway.create_app()).get("/metrics").text
    for status in serving_gateway.DECISION_STATUSES:
        assert f'examlops_gateway_decisions_total{{status="{status}"}} 0' in metrics


# ─── the Open Inference Protocol over gRPC (ADR 0126) ────────────────────────

GRPC = serving_gateway.GRPC_SERVICE_PREFIX


@pytest.mark.parametrize("rpc", ["ServerLive", "ServerReady", "ServerMetadata"])
def test_grpc_health_and_server_metadata_are_open_like_rest(rpc):
    verdict = decide("POST", GRPC + rpc, None)  # every gRPC call is a POST
    assert verdict.allowed
    assert verdict.headers == {TENANT_HEADER: "", PRINCIPAL_HEADER: "", PROJECT_HEADER: ""}
    assert decide("GET", GRPC + rpc, None).status == 401  # not gRPC, not open


@pytest.mark.parametrize("rpc", ["ModelInfer", "ModelMetadata", "ModelReady", "ServerLiveX"])
def test_grpc_model_calls_need_a_credential(rpc):
    assert decide("POST", GRPC + rpc, None).status == 401


def test_a_key_without_an_allow_list_may_infer_over_grpc():
    verdict = decide("POST", GRPC + "ModelInfer", f"Bearer {_key()}")
    assert verdict.allowed and verdict.headers[TENANT_HEADER] == "acme"


def test_an_allow_listed_key_cannot_infer_over_grpc():
    """gRPC names its model in the body, which the gateway never reads: an allow-list cannot be
    checked, so it is refused rather than bypassed."""
    verdict = decide("POST", GRPC + "ModelInfer", f"Bearer {_key(models=['jpcp'])}")
    assert verdict.status == 403 and "allow-list" in verdict.reason


def test_grpc_cannot_be_scoped_to_a_project(tenancy):
    verdict = decide("POST", GRPC + "ModelInfer", f"Bearer {_key(project='research')}")
    assert verdict.status == 403
    assert (
        "gRPC cannot be authorized" in verdict.reason
        and "/v2/models/{name}/infer" in verdict.reason
    )
    assert decide("POST", GRPC + "ServerLive", None).allowed  # health stays open under tenancy


# ─── readiness ────────────────────────────────────────────────────────────────
# The chart pointed BOTH probes at a constant `/healthz`, so it had no readiness signal at all:
# a replica whose credential store it has never reached refuses every request with 503, and the
# rollout that created it completes as a success. `/healthz` stays constant — it is liveness, and
# a store outage must never restart a pod whose key cache is carrying traffic — and `/readyz`
# carries the readiness question.


def test_readyz_is_not_ready_until_the_credential_store_has_answered_once(monkeypatch):
    from fastapi.testclient import TestClient

    from examlops.data import gateway as data_gateway

    serving_gateway.reset_store_readiness()

    def down(*_a, **_k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(data_gateway, "get_virtual_key", down)
    client = TestClient(serving_gateway.create_app())
    response = client.get("/readyz")
    assert response.status_code == 503, response.text
    assert response.json()["status"] == "starting"


def test_readyz_stays_ready_through_a_later_outage_so_replicas_are_not_all_evicted(monkeypatch):
    from fastapi.testclient import TestClient

    from examlops.data import gateway as data_gateway

    serving_gateway.reset_store_readiness()
    client = TestClient(serving_gateway.create_app())
    assert client.get("/readyz").json() == {"status": "ok"}

    # The store goes away *after* this replica proved it could read it. Readiness must not flap:
    # every replica would leave the Service at once, turning refusals into connection errors,
    # and the key cache is what carries verified traffic through a blip.
    def down(*_a, **_k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(data_gateway, "get_virtual_key", down)
    assert client.get("/readyz").status_code == 200


def test_healthz_stays_a_pure_liveness_check(monkeypatch):
    """A store outage must not fail liveness: the chart would restart-loop the pod."""
    from fastapi.testclient import TestClient

    from examlops.data import gateway as data_gateway

    serving_gateway.reset_store_readiness()

    def down(*_a, **_k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(data_gateway, "get_virtual_key", down)
    client = TestClient(serving_gateway.create_app())
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_against_a_real_broken_store_not_a_patched_one(tmp_path, monkeypatch):
    """The tests above patch `get_virtual_key`; this one breaks the store for real.

    Patching the seam only proves the branch is wired. What it cannot show is that a genuinely
    broken store *raises* rather than answering — and if it answered, `/readyz` would report a
    dead replica ready. An empty-but-working store must still be ready: nothing is wrong with a
    platform that has issued no keys yet.
    """
    from fastapi.testclient import TestClient

    monkeypatch.delenv("EXAMLOPS_DB_BACKEND", raising=False)

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite file, no header, nothing like one")
    monkeypatch.setenv("PLATFORM_DB", str(corrupt))
    serving_gateway.reset_store_readiness()
    assert TestClient(serving_gateway.create_app()).get("/readyz").status_code == 503

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "fresh.db"))
    serving_gateway.reset_store_readiness()
    assert TestClient(serving_gateway.create_app()).get("/readyz").status_code == 200
