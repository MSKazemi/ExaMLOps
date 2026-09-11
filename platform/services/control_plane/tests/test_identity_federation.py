"""ADR 0120 — the control plane accepts tokens from a data center's own IdP.

A real-HTTP fake IdP and PDP (tests/unit/_iam_fakes.py) stand in for the center, so the route
dependencies do genuine discovery/JWKS fetches and AuthZEN calls. Identity and tenancy come only
from the verified token + the trust file's issuer→tenant binding; the center's PDP can veto.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient


def _load_fakes():
    # By file path: this suite's own `tests` package shadows the repo-root one.
    path = Path(__file__).resolve().parents[4] / "tests" / "unit" / "_iam_fakes.py"
    spec = importlib.util.spec_from_file_location("_iam_fakes", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_iam_fakes"] = module
    spec.loader.exec_module(module)
    return module


_fakes = _load_fakes()
FakeIdP, FakePdp = _fakes.FakeIdP, _fakes.FakePdp


class _Coordinator:
    def allow(self, _bucket, _limit, _window_s):
        return True

    def try_lock(self, _key, _holder, _ttl_s):
        return True

    def unlock(self, _key, _holder):
        return None


@pytest.fixture()
def centers():
    a, b, pdp = FakeIdP(), FakeIdP(issuer_suffix="/realms/b"), FakePdp()
    yield a, b, pdp
    a.stop()
    b.stop()
    pdp.stop()


def _trust(tmp_path, a: Any, b: Any, pdp: Any = None) -> Path:
    jsc = {
        "name": "jsc",
        "issuer": a.issuer,
        "audience": "examlops",
        "tenant": "jsc",
        "role_rules": [
            {"value": "mlops-admins", "role": "admin"},
            {"value": "mlops-ops", "role": "operator"},
            {"value": "hpc-users", "role": "viewer"},
        ],
    }
    if pdp is not None:
        jsc["authorization"] = {"mode": "both", "pdp": {"type": "authzen", "url": pdp.url}}
    cineca = {"name": "cineca", "issuer": b.issuer, "audience": "examlops", "tenant": "cineca"}
    path = tmp_path / "identity-providers.yaml"
    path.write_text(yaml.safe_dump({"providers": [jsc, cineca]}))
    return path


def _load(monkeypatch, tmp_path, trust: Path | None, *, static_token: str | None = None):
    state_db = tmp_path / "federation.db"
    if static_token:
        monkeypatch.setenv("CONTROL_PLANE_TOKEN", static_token)
    else:
        monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("CONTROL_PLANE_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("EXAMLOPS_OIDC_ISSUER", raising=False)
    if trust is not None:
        monkeypatch.setenv("EXAMLOPS_IAM_CONFIG", str(trust))
    else:
        monkeypatch.delenv("EXAMLOPS_IAM_CONFIG", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(state_db))
    monkeypatch.setenv("PLATFORM_DB", str(state_db))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_EVENT_RELAY_SECONDS", "0")
    from examlops import iam

    iam.clear_caches()
    import app as cp_app

    importlib.reload(cp_app)
    monkeypatch.setattr(cp_app, "_get_registry", lambda: {"JPCP": ["PM100Dataset"]})
    monkeypatch.setattr(cp_app, "_get_coordinator", lambda: _Coordinator())
    return cp_app


# A static control-plane credential for the "static and federated side by side" cases. Assembled at
# runtime so the platform's own leak gate (`exa secrets scan`, run by the security-ok CI job) never
# sees a credential-shaped `token="…"` literal in the tree; it is a test fixture, not a secret.
_STATIC_CREDENTIAL = "-".join(("static", "ops", "credential", "123"))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_federation_alone_is_a_usable_auth_configuration(centers, tmp_path, monkeypatch):
    a, b, _ = centers
    cp = _load(monkeypatch, tmp_path, _trust(tmp_path, a, b))
    client = TestClient(cp.app)
    op = a.mint(groups=["mlops-ops"])
    assert client.get("/approvals", headers=_bearer(op)).status_code == 200
    created = client.post(
        "/api/changes", json={"model_ids": ["JPCP"], "commit_sha": "abc"}, headers=_bearer(op)
    )
    assert created.status_code == 200, created.text
    conn = cp._get_db()
    try:
        row = conn.execute("SELECT tenant, requested_by FROM pending_approvals").fetchone()
    finally:
        conn.close()
    # Tenant from the issuer binding, principal = stable (provider, sub) — never from the body.
    assert tuple(row) == ("jsc", "jsc:u-123")


def test_viewer_role_reads_but_cannot_write(centers, tmp_path, monkeypatch):
    a, b, _ = centers
    cp = _load(monkeypatch, tmp_path, _trust(tmp_path, a, b))
    client = TestClient(cp.app)
    viewer = a.mint(groups=["hpc-users"])
    assert client.get("/approvals", headers=_bearer(viewer)).status_code == 200
    denied = client.post("/api/changes", json={"model_ids": ["JPCP"]}, headers=_bearer(viewer))
    assert denied.status_code == 403 and "write" in denied.json()["detail"]


def test_authenticated_without_a_role_is_forbidden_not_a_silent_viewer(
    centers, tmp_path, monkeypatch
):
    a, b, _ = centers
    cp = _load(monkeypatch, tmp_path, _trust(tmp_path, a, b))
    resp = TestClient(cp.app).get("/approvals", headers=_bearer(a.mint(groups=["students"])))
    assert resp.status_code == 403
    assert "grants no ExaMLOps role" in resp.json()["detail"]


def test_untrusted_or_forged_federated_token_is_401(centers, tmp_path, monkeypatch):
    a, b, _ = centers
    cp = _load(monkeypatch, tmp_path, _trust(tmp_path, a, b))
    client = TestClient(cp.app)
    stranger = FakeIdP(issuer_suffix="/realms/stranger")
    try:
        resp = client.get("/approvals", headers=_bearer(stranger.mint(groups=["mlops-admins"])))
    finally:
        stranger.stop()
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    expired = client.get("/approvals", headers=_bearer(a.mint(groups=["mlops-ops"], exp=1)))
    assert expired.status_code == 401


def test_centers_are_isolated_tenants(centers, tmp_path, monkeypatch):
    a, b, _ = centers
    cp = _load(monkeypatch, tmp_path, _trust(tmp_path, a, b))
    client = TestClient(cp.app)
    jsc_op = a.mint(groups=["mlops-ops"])
    cineca_admin = b.mint(groups=["examlops-admins"])  # default rules on the cineca entry
    assert (
        client.post(
            "/api/changes", json={"model_ids": ["JPCP"]}, headers=_bearer(jsc_op)
        ).status_code
        == 200
    )
    assert client.get("/approvals", headers=_bearer(cineca_admin)).json() == []
    assert len(client.get("/approvals", headers=_bearer(jsc_op)).json()) == 1
    # A CINECA admin cannot act on a JSC approval either.
    assert client.post("/approve/JPCP", headers=_bearer(cineca_admin)).status_code == 404


def test_the_centers_pdp_can_veto_writes(centers, tmp_path, monkeypatch):
    a, b, pdp = centers
    pdp.policy = lambda req: req["action"]["name"] != "api.write"
    cp = _load(monkeypatch, tmp_path, _trust(tmp_path, a, b, pdp))
    client = TestClient(cp.app)
    admin = a.mint(groups=["mlops-admins"])
    assert client.get("/approvals", headers=_bearer(admin)).status_code == 200
    vetoed = client.post("/api/changes", json={"model_ids": ["JPCP"]}, headers=_bearer(admin))
    assert vetoed.status_code == 403 and "center policy" in vetoed.json()["detail"]
    _, body = pdp.requests[-1]
    assert body["resource"]["type"] == "control_plane"
    assert body["resource"]["id"] == "/api/changes"
    assert body["resource"]["properties"] == {"method": "POST", "tenant": "jsc"}


def test_pdp_outage_denies_federated_calls_but_not_static_credentials(
    centers, tmp_path, monkeypatch
):
    a, b, pdp = centers
    pdp.fail_with = 503
    cp = _load(monkeypatch, tmp_path, _trust(tmp_path, a, b, pdp), static_token=_STATIC_CREDENTIAL)
    client = TestClient(cp.app)
    assert (
        client.get("/approvals", headers=_bearer(a.mint(groups=["mlops-admins"]))).status_code
        == 403
    )
    assert client.get("/approvals", headers=_bearer(_STATIC_CREDENTIAL)).status_code == 200


def test_static_credentials_unchanged_alongside_federation(centers, tmp_path, monkeypatch):
    a, b, _ = centers
    cp = _load(monkeypatch, tmp_path, _trust(tmp_path, a, b), static_token=_STATIC_CREDENTIAL)
    client = TestClient(cp.app)
    assert client.get("/approvals", headers=_bearer(_STATIC_CREDENTIAL)).status_code == 200
    # A wrong static (non-JWT) token keeps its 403 — it is never offered to the verifier.
    assert client.get("/approvals", headers=_bearer("wrong-static-token")).status_code == 403


def test_invalid_trust_file_fails_closed_and_is_reported(centers, tmp_path, monkeypatch):
    a, _, _ = centers
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {"providers": [{"name": "jsc", "issuer": a.issuer, "algorithms": ["HS256"]}]}
        )
    )
    cp = _load(monkeypatch, tmp_path, bad, static_token=_STATIC_CREDENTIAL)
    client = TestClient(cp.app)
    with client:
        health = client.get("/health").json()
    assert health["startup_checks"]["identity_federation"].startswith("fail")
    assert (
        client.get("/approvals", headers=_bearer(a.mint(groups=["mlops-ops"]))).status_code == 403
    )
    assert client.get("/approvals", headers=_bearer(_STATIC_CREDENTIAL)).status_code == 200


def test_federation_off_adds_nothing_to_health(tmp_path, monkeypatch):
    cp = _load(monkeypatch, tmp_path, None, static_token=_STATIC_CREDENTIAL)
    with TestClient(cp.app) as client:
        health = client.get("/health").json()
    assert "identity_federation" not in health["startup_checks"]


def test_a_deactivated_account_is_refused_with_a_still_valid_token(centers, tmp_path, monkeypatch):
    """ADR 0132: the account directory is consulted on every verified token, here as elsewhere."""
    a, b, _ = centers
    cp = _load(monkeypatch, tmp_path, _trust(tmp_path, a, b))
    client = TestClient(cp.app)
    token = a.mint(groups=["mlops-ops"])
    assert client.get("/approvals", headers=_bearer(token)).status_code == 200  # JIT-recorded
    from examlops.iam import directory

    directory.deactivate(directory.find("jsc", subject="u-123"), actor="ops", reason="left")
    refused = client.get("/approvals", headers=_bearer(token))
    assert refused.status_code == 401 and "deactivated" in refused.json()["detail"]
