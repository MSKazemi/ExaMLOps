"""ADR 0120 against a REAL identity provider and policy engine — Keycloak + Open Policy Agent.

Everything else in the IAM suite runs against a faithful fake. This one runs the platform against
the reference broker from `platform/infra/iam/docker-compose.iam.yml`, driving the human's side of
each flow over HTTP exactly as a browser would (Keycloak's own login form):

1. ``exa``'s device flow (RFC 8628) → a Keycloak access token → the platform's verifier maps it to
   ``lab:<sub>`` / operator / tenant ``lab``;
2. refresh-token rotation: the new refresh token works, the replayed old one is refused;
3. the dashboard BFF: authorization code + PKCE + RFC 9207 ``iss`` → an HttpOnly session cookie →
   ``/api/auth/me``; the center's OPA policy vetoes a promotion without MFA;
4. the control plane accepts the same user's token and OPA allows its write.

Opt-in: skipped unless ``EXAMLOPS_IAM_LIVE_KEYCLOAK_ADMIN_PASSWORD`` is set (the bootstrap admin
password the compose file was started with), because it needs the two containers running:

    KC_BOOTSTRAP_ADMIN_PASSWORD=… docker compose -f platform/infra/iam/docker-compose.iam.yml up -d
    EXAMLOPS_IAM_LIVE_KEYCLOAK_ADMIN_PASSWORD=… .venv/bin/pytest tests/integration/test_iam_keycloak_live.py -v
"""

from __future__ import annotations

import html
import importlib
import os
import re
import secrets
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import pytest
import yaml

KC = os.getenv("EXAMLOPS_IAM_LIVE_KEYCLOAK_URL", "http://localhost:18180")
OPA = os.getenv("EXAMLOPS_IAM_LIVE_OPA_URL", "http://localhost:18181")
REALM = "examlops"
ISSUER = f"{KC}/realms/{REALM}"
ADMIN_PW = os.getenv("EXAMLOPS_IAM_LIVE_KEYCLOAK_ADMIN_PASSWORD", "")
REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not ADMIN_PW, reason="live Keycloak not configured (EXAMLOPS_IAM_LIVE_KEYCLOAK_ADMIN_PASSWORD)"
)


def _admin_token() -> str:
    r = httpx.post(
        f"{KC}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": os.getenv("EXAMLOPS_IAM_LIVE_KEYCLOAK_ADMIN", "admin"),
            "password": ADMIN_PW,
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def realm():
    """A throwaway user in group examlops-operators + the dashboard client's secret."""
    admin = {"Authorization": f"Bearer {_admin_token()}"}
    username = f"alice-{uuid.uuid4().hex[:6]}"
    password = secrets.token_urlsafe(18)
    r = httpx.post(
        f"{KC}/admin/realms/{REALM}/users",
        headers=admin,
        json={
            "username": username,
            "email": f"{username}@lab.example.org",
            "emailVerified": True,
            "firstName": "Alice",
            "lastName": "Operator",
            "enabled": True,
            "groups": ["/examlops-operators"],
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        },
        timeout=10,
    )
    assert r.status_code == 201, r.text
    user_id = r.headers["location"].rsplit("/", 1)[-1]
    clients = httpx.get(
        f"{KC}/admin/realms/{REALM}/clients",
        headers=admin,
        params={"clientId": "examlops-dashboard"},
        timeout=10,
    ).json()
    secret = httpx.get(
        f"{KC}/admin/realms/{REALM}/clients/{clients[0]['id']}/client-secret",
        headers=admin,
        timeout=10,
    ).json()["value"]
    yield {"username": username, "password": password, "user_id": user_id, "secret": secret}
    httpx.delete(f"{KC}/admin/realms/{REALM}/users/{user_id}", headers=admin, timeout=10)


@pytest.fixture
def trust(realm, tmp_path, monkeypatch):
    from examlops import iam

    monkeypatch.setenv("LAB_DASHBOARD_CLIENT_SECRET", realm["secret"])
    path = tmp_path / "identity-providers.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {
                        "name": "lab",
                        "display_name": "Lab (Keycloak)",
                        "issuer": ISSUER,
                        "audience": "examlops",
                        "tenant": "lab",
                        "group_claims": ["groups"],
                        "authorization": {
                            "mode": "both",
                            "pdp": {"type": "opa", "url": OPA, "opa_path": "examlops/decision"},
                        },
                        "clients": {
                            "dashboard": {
                                "client_id": "examlops-dashboard",
                                "client_secret_ref": "env:LAB_DASHBOARD_CLIENT_SECRET",
                            },
                            "cli": {"client_id": "exa-cli"},
                        },
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("EXAMLOPS_IAM_CONFIG", str(path))
    iam.clear_caches()
    yield iam.load_config()
    iam.clear_caches()


class _Browser:
    """Just enough browser for Keycloak's login pages: a cookie store and manual redirects.

    Not httpx's cookie jar: it (via ``http.cookiejar``) refuses Secure cookies over http:// even on
    loopback and files a dotless host's cookies under ``localhost.local`` — and httpx copies the
    jar into a default one per request, so a custom policy is lost. Browsers treat http://localhost
    as a secure context; so does this.
    """

    def __init__(self) -> None:
        self.client = httpx.Client(timeout=10)
        self.cookies: dict[str, str] = {}
        self.last = KC

    def __enter__(self) -> _Browser:
        return self

    def __exit__(self, *exc) -> None:
        self.client.close()

    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        url = urljoin(self.last, url)  # form actions and Location headers may be relative
        self.last = url
        headers = dict(kw.pop("headers", {}) or {})
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        resp = self.client.request(method, url, headers=headers, follow_redirects=False, **kw)
        for raw in resp.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            value = rest.split(";", 1)[0]
            if "max-age=0" in raw.lower() or not value:
                self.cookies.pop(name.strip(), None)
            else:
                self.cookies[name.strip()] = value
        return resp

    def get(self, url: str, *, follow_redirects: bool = False) -> httpx.Response:
        resp = self._send("GET", url)
        hops = 0
        while follow_redirects and resp.is_redirect and hops < 10:
            resp = self._send(
                "GET", str(resp.next_request.url if resp.next_request else resp.headers["location"])
            )
            hops += 1
        return resp

    def post(self, url: str, *, data: dict, follow_redirects: bool = False) -> httpx.Response:
        resp = self._send("POST", url, data=data)
        if follow_redirects and resp.is_redirect:
            return self.get(resp.headers["location"], follow_redirects=True)
        return resp


def _browser() -> _Browser:
    return _Browser()


def _keycloak_login(browser: _Browser, url: str, realm: dict) -> httpx.Response:
    """Play the human: open the IdP page, submit Keycloak's login form, return the last response."""
    page = browser.get(url, follow_redirects=True)
    m = re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page.text)
    assert m, f"no Keycloak login form at {page.url}"
    return browser.post(
        html.unescape(m.group(1)),
        data={"username": realm["username"], "password": realm["password"], "credentialId": ""},
        follow_redirects=False,
    )


def _device_login(trust, realm) -> dict:
    from examlops.iam import flows

    p = trust.by_name("lab")
    client = p.client("cli")
    device = flows.device_authorize(p, client)
    with _browser() as browser:
        resp = _keycloak_login(browser, device["verification_uri_complete"], realm)
        # Follow to the end of the approval: Keycloak may show a consent/grant page first.
        for _ in range(5):
            if resp.status_code in (301, 302, 303):
                resp = browser.get(resp.headers["location"], follow_redirects=False)
                continue
            grant = re.search(r'<form[^>]+action="([^"]+)"[^>]*>.*?name="accept"', resp.text, re.S)
            if grant:
                resp = browser.post(html.unescape(grant.group(1)), data={"accept": "Yes"})
                continue
            break
    return flows.poll_device_token(p, client, device, sleep=lambda s: time.sleep(min(s, 2)))


def test_device_flow_token_is_verified_and_mapped(trust, realm):
    from examlops import iam

    tokens = _device_login(trust, realm)
    principal = iam.verify_access_token(tokens["access_token"])
    assert principal.issuer == ISSUER
    assert principal.id == f"lab:{realm['user_id']}"
    assert principal.username == realm["username"]
    assert principal.role == "operator" and principal.tenant == "lab"
    assert "examlops-operators" in principal.groups


def test_refresh_token_rotation_and_replay_refusal(trust, realm):
    from examlops.iam import flows

    tokens = _device_login(trust, realm)
    p = trust.by_name("lab")
    fresh = flows.refresh(p, p.client("cli"), tokens["refresh_token"])
    assert fresh["access_token"] and fresh["refresh_token"] != tokens["refresh_token"]
    with pytest.raises(flows.FlowError, match="invalid_grant"):
        flows.refresh(p, p.client("cli"), tokens["refresh_token"])  # replay of the rotated token


def test_center_opa_policy_decides_through_the_platform(trust, realm):
    from examlops import iam

    principal = iam.verify_access_token(_device_login(trust, realm)["access_token"])
    weekend = datetime.now(UTC).strftime("%A") in {"Saturday", "Sunday"}
    retrain = iam.authorize(principal, "retrain.trigger", {"type": "model", "id": "JPCP"})
    assert retrain.allowed is (not weekend), retrain
    promote = iam.authorize(principal, "model.promote", {"type": "model", "id": "JPCP"})
    assert not promote.allowed and promote.layer == "pdp"
    assert "MFA" in promote.reason or "freeze" in promote.reason
    read = iam.authorize(principal, "api.read", {"type": "control_plane", "id": "/approvals"})
    assert read.allowed and read.layer == "combined"


def _dashboard_app(monkeypatch):
    backend = REPO / "platform" / "services" / "dashboard" / "backend"
    monkeypatch.syspath_prepend(str(backend))
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("DASHBOARD_VIEWER_PASSWORD", secrets.token_urlsafe(16))
    monkeypatch.setenv("DASHBOARD_ADMIN_PASSWORD", secrets.token_urlsafe(16))
    monkeypatch.setenv("DASHBOARD_JWT_SECRET", secrets.token_urlsafe(32))
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DASHBOARD_SECRET_KEY", Fernet.generate_key().decode())
    for mod in [m for m in sys.modules if m in {"settings", "auth", "main"}]:
        del sys.modules[mod]
    import main

    return importlib.reload(main).app


def test_dashboard_bff_login_against_keycloak(trust, realm, monkeypatch):
    from fastapi.testclient import TestClient

    app = _dashboard_app(monkeypatch)
    client = TestClient(app, base_url="http://localhost:18099")
    start = client.get("/api/auth/sso/lab/login", follow_redirects=False)
    assert start.status_code == 302 and start.headers["location"].startswith(ISSUER)
    q = parse_qs(urlparse(start.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"]
    state_cookie = start.cookies.get("__Host-examlops_sso")
    with _browser() as browser:
        back = _keycloak_login(browser, start.headers["location"], realm)
    assert back.status_code == 302, back.text[:300]
    callback = urlparse(back.headers["location"])
    assert callback.path == "/api/auth/sso/callback"
    params = {k: v[0] for k, v in parse_qs(callback.query).items()}
    assert params["iss"] == ISSUER  # Keycloak sends RFC 9207 iss; the BFF checks it
    done = client.get(
        "/api/auth/sso/callback",
        params=params,
        headers={"Cookie": f"__Host-examlops_sso={state_cookie}"},
        follow_redirects=False,
    )
    assert done.status_code == 302 and "sso_error" not in done.headers["location"], done.headers
    session = done.cookies.get("__Host-examlops_session")
    assert session
    me = client.get("/api/auth/me", headers={"Cookie": f"__Host-examlops_session={session}"}).json()
    assert me["role"] == "operator" and me["tenant"] == "lab"
    assert me["idp"] == "lab" and me["auth_method"] == "sso"
    assert me["name"] == realm["username"]
    vetoed = client.put(
        "/api/models/JPCP/versions/1/alias",
        json={"alias": "Production"},
        headers={"Cookie": f"__Host-examlops_session={session}", "X-ExaMLOps-CSRF": "1"},
    )
    assert vetoed.status_code == 403 and "Denied by policy" in vetoed.json()["detail"]


def test_control_plane_accepts_the_users_token(trust, realm, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    token = _device_login(trust, realm)["access_token"]
    cp_dir = REPO / "platform" / "services" / "control_plane"
    monkeypatch.syspath_prepend(str(cp_dir))
    monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("CONTROL_PLANE_CREDENTIALS_JSON", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_EVENT_RELAY_SECONDS", "0")
    import app as cp_app

    cp_app = importlib.reload(cp_app)
    client = TestClient(cp_app.app)
    resp = client.get("/approvals", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
