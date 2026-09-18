"""ADR 0120 — dashboard SSO with a data center's IdP (BFF, RFC 10017) and delegated authorization.

A real-HTTP fake IdP/PDP on loopback (repo `tests/unit/_iam_fakes.py`) plays the center. The test
drives the browser's side by hand: follow the 302 to the IdP, have the IdP issue a code bound to
the PKCE challenge the BFF sent, come back to the callback, and use the session cookie.
"""

from __future__ import annotations

import importlib.util
import sys
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import yaml


def _load_fakes():
    # By file path: this suite's own `tests` package shadows the repo-root one.
    path = Path(__file__).resolve().parents[5] / "tests" / "unit" / "_iam_fakes.py"
    spec = importlib.util.spec_from_file_location("_iam_fakes", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_iam_fakes"] = module
    spec.loader.exec_module(module)
    return module


_fakes = _load_fakes()
FakeIdP, FakePdp = _fakes.FakeIdP, _fakes.FakePdp

REDIRECT = "http://localhost:18099/api/auth/sso/callback"


@pytest.fixture
def center(tmp_path, monkeypatch):
    idp, pdp = FakeIdP(), FakePdp()
    monkeypatch.setenv("JSC_DASH_SECRET", "dash-secret")
    # Every fake IdP signs the same subject, and sign-in records it in the account directory
    # (ADR 0132). A shared datastore lets one test's deactivation lock another worker's user out
    # under `pytest -n auto`, so each test gets its own.
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))

    def write(**extra):
        entry = {
            "name": "jsc",
            "display_name": "Jülich Supercomputing Centre",
            "issuer": idp.issuer,
            "audience": "examlops",
            "tenant": "jsc",
            "role_rules": [
                {"value": "mlops-admins", "role": "admin"},
                {"value": "mlops-ops", "role": "operator"},
            ],
            "clients": {
                "dashboard": {
                    "client_id": "examlops-dashboard",
                    "client_secret_ref": "env:JSC_DASH_SECRET",
                }
            },
            **extra,
        }
        path = tmp_path / "identity-providers.yaml"
        path.write_text(yaml.safe_dump({"providers": [entry]}))
        monkeypatch.setenv("EXAMLOPS_IAM_CONFIG", str(path))
        from examlops import iam

        iam.clear_caches()

    write()
    yield idp, pdp, write
    idp.stop()
    pdp.stop()
    from examlops import iam

    iam.clear_caches()


def _cookies(resp) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for header in resp.headers.get_list("set-cookie"):
        c = SimpleCookie()
        c.load(header)
        for name, morsel in c.items():
            out[name] = {
                "value": morsel.value,
                "httponly": "httponly" in header.lower(),
                "samesite": (morsel["samesite"] or "").lower(),
                "secure": "secure" in header.lower(),
                "max_age": morsel["max-age"],
            }
    return out


async def _sso_login(client, idp: Any, *, groups, return_to="/models", acr=None, iss="ok"):
    start = await client.get("/api/auth/sso/jsc/login", params={"return_to": return_to})
    assert start.status_code == 302
    authz = urlparse(start.headers["location"])
    q = {k: v[0] for k, v in parse_qs(authz.query).items()}
    state_cookie = _cookies(start)["__Host-examlops_sso"]
    claims = {"groups": groups, "email": "alice@fz-juelich.de"}
    if acr:
        claims["acr"] = acr
    code = idp.issue_code(
        challenge=q["code_challenge"],
        nonce=q["nonce"],
        redirect_uri=q["redirect_uri"],
        claims=claims,
    )
    params = {"code": code, "state": q["state"]}
    if iss == "ok":
        params["iss"] = idp.issuer
    elif iss:
        params["iss"] = iss
    cb = await client.get(
        "/api/auth/sso/callback",
        params=params,
        headers={"Cookie": f"__Host-examlops_sso={state_cookie['value']}"},
    )
    return start, q, state_cookie, cb


async def test_providers_listed_for_the_login_page(client, center):
    body = (await client.get("/api/auth/sso/providers")).json()
    assert body["local_login"] is True
    assert body["providers"] == [
        {
            "name": "jsc",
            "display_name": "Jülich Supercomputing Centre",
            "login_url": "/api/auth/sso/jsc/login",
            "step_up": False,
        }
    ]


async def test_full_authorization_code_pkce_login(client, center):
    idp, _, _ = center
    start, q, state_cookie, cb = await _sso_login(client, idp, groups=["mlops-ops"])
    # The IdP was asked for a code bound to an S256 PKCE challenge, for our registered redirect.
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["client_id"] == "examlops-dashboard" and q["redirect_uri"] == REDIRECT
    assert state_cookie["httponly"] and state_cookie["samesite"] == "lax"
    assert cb.status_code == 302 and cb.headers["location"] == "/models"
    session = _cookies(cb)["__Host-examlops_session"]
    assert session["httponly"] and session["secure"] and session["samesite"] == "strict"
    # The browser never holds an IdP token: the session is the dashboard's own, in a cookie.
    assert "access_token" not in cb.text and idp.issuer not in cb.headers["location"]
    me = await client.get(
        "/api/auth/me", headers={"Cookie": f"__Host-examlops_session={session['value']}"}
    )
    assert me.status_code == 200
    body = me.json()
    assert body["role"] == "operator" and body["tenant"] == "jsc"
    assert body["idp"] == "jsc" and body["auth_method"] == "sso" and body["name"] == "alice"
    assert "model.promote" in body["capabilities"] and "secret.reveal" not in body["capabilities"]


@pytest.mark.parametrize("iss, reason", [("https://mix-up.example", "login_rejected")])
async def test_rfc9207_issuer_mismatch_is_rejected(client, center, iss, reason):
    idp, _, _ = center
    *_, cb = await _sso_login(client, idp, groups=["mlops-ops"], iss=iss)
    assert cb.status_code == 302 and f"sso_error={reason}" in cb.headers["location"]
    assert "__Host-examlops_session" not in _cookies(cb)


async def test_state_mismatch_and_missing_state_cookie(client, center):
    idp, _, _ = center
    start = await client.get("/api/auth/sso/jsc/login")
    state_cookie = _cookies(start)["__Host-examlops_sso"]["value"]
    forged = await client.get(
        "/api/auth/sso/callback",
        params={"code": "x", "state": "attacker-state"},
        headers={"Cookie": f"__Host-examlops_sso={state_cookie}"},
    )
    assert "sso_error=state_mismatch" in forged.headers["location"]
    no_cookie = await client.get("/api/auth/sso/callback", params={"code": "x", "state": "y"})
    assert "sso_error=session_expired" in no_cookie.headers["location"]


async def test_user_without_a_mapped_role_gets_no_session(client, center):
    idp, _, _ = center
    *_, cb = await _sso_login(client, idp, groups=["students"])
    assert "sso_error=no_role" in cb.headers["location"]
    assert "__Host-examlops_session" not in _cookies(cb)


async def test_open_redirect_is_neutralised(client, center):
    idp, _, _ = center
    *_, cb = await _sso_login(client, idp, groups=["mlops-ops"], return_to="//evil.example/x")
    assert cb.headers["location"] == "/"


async def test_cookie_session_needs_same_origin_for_writes(client, center):
    idp, _, _ = center
    *_, cb = await _sso_login(client, idp, groups=["mlops-ops"])
    cookie = f"__Host-examlops_session={_cookies(cb)['__Host-examlops_session']['value']}"
    cross = await client.post(
        "/api/auth/logout", headers={"Cookie": cookie, "Sec-Fetch-Site": "cross-site"}
    )
    assert cross.status_code == 403 and "CSRF" in cross.json()["detail"]
    bare = await client.post("/api/auth/logout", headers={"Cookie": cookie})
    assert bare.status_code == 403  # no origin evidence at all → refused
    same = await client.post(
        "/api/auth/logout", headers={"Cookie": cookie, "Sec-Fetch-Site": "same-origin"}
    )
    assert same.status_code == 204
    spa = await client.post("/api/auth/logout", headers={"Cookie": cookie, "X-ExaMLOps-CSRF": "1"})
    assert spa.status_code == 204


async def test_idp_bearer_token_works_for_api_clients(client, center):
    idp, _, _ = center
    token = idp.mint(groups=["mlops-admins"])
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["auth_method"] == "idp-bearer" and me.json()["role"] == "admin"
    forged = FakeIdP(issuer_suffix="/realms/evil")
    try:
        bad = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged.mint()}"})
    finally:
        forged.stop()
    assert bad.status_code == 401


async def test_removing_the_center_from_the_trust_file_ends_its_sessions(
    client, center, tmp_path, monkeypatch
):
    idp, _, _ = center
    *_, cb = await _sso_login(client, idp, groups=["mlops-ops"])
    cookie = f"__Host-examlops_session={_cookies(cb)['__Host-examlops_session']['value']}"
    assert (await client.get("/api/auth/me", headers={"Cookie": cookie})).status_code == 200
    other = tmp_path / "other.yaml"
    other.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {"name": "cineca", "issuer": "https://login.cineca.it", "audience": "examlops"}
                ]
            }
        )
    )
    monkeypatch.setenv("EXAMLOPS_IAM_CONFIG", str(other))
    resp = await client.get("/api/auth/me", headers={"Cookie": cookie})
    assert resp.status_code == 401 and "no longer trusted" in resp.json()["detail"]


async def test_step_up_challenge_rfc9470(client, center):
    idp, _, write = center
    write(step_up={"acr_values": ["https://refeds.org/profile/mfa"], "max_age_s": 600})
    *_, cb = await _sso_login(client, idp, groups=["mlops-admins"])  # no MFA acr
    cookie = f"__Host-examlops_session={_cookies(cb)['__Host-examlops_session']['value']}"
    resp = await client.put(
        "/api/models/JPCP/versions/1/alias",
        json={"alias": "Production"},
        headers={"Cookie": cookie, "X-ExaMLOps-CSRF": "1"},
    )
    assert resp.status_code == 401
    www = resp.headers["www-authenticate"]
    assert 'error="insufficient_user_authentication"' in www
    assert 'acr_values="https://refeds.org/profile/mfa"' in www and "max_age=600" in www
    # Asking for step-up sends the IdP max_age=0, prompt=login and the acr values.
    start = await client.get("/api/auth/sso/jsc/login", params={"step_up": "true"})
    q = parse_qs(urlparse(start.headers["location"]).query)
    assert q["max_age"] == ["0"] and q["prompt"] == ["login"]
    assert q["acr_values"] == ["https://refeds.org/profile/mfa"]


async def test_local_password_step_up_is_opt_in(client, monkeypatch):
    from tests.conftest import ADMIN_PW

    token = (await client.post("/api/auth/login", json={"password": ADMIN_PW})).json()["token"]
    monkeypatch.setenv("EXAMLOPS_IAM_STEP_UP", "enforce")
    monkeypatch.setenv("EXAMLOPS_IAM_STEP_UP_MAX_AGE", "0")
    import time

    time.sleep(1.1)
    resp = await client.put(
        "/api/models/JPCP/versions/1/alias",
        json={"alias": "Production"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (
        resp.status_code == 401
        and "insufficient_user_authentication" in resp.headers["www-authenticate"]
    )


async def test_the_centers_pdp_can_veto_dashboard_actions(client, center):
    idp, pdp, write = center
    pdp.policy = lambda req: req["action"]["name"] not in {"model.promote"}
    write(authorization={"mode": "both", "pdp": {"type": "authzen", "url": pdp.url}})
    *_, cb = await _sso_login(client, idp, groups=["mlops-admins"])
    cookie = f"__Host-examlops_session={_cookies(cb)['__Host-examlops_session']['value']}"
    assert (await client.get("/api/auth/me", headers={"Cookie": cookie})).status_code == 200
    vetoed = await client.put(
        "/api/models/JPCP/versions/1/alias",
        json={"alias": "Production"},
        headers={"Cookie": cookie, "X-ExaMLOps-CSRF": "1"},
    )
    assert vetoed.status_code == 403 and "center policy" in vetoed.json()["detail"]
    actions = {body["action"]["name"] for _, body in pdp.requests}
    assert {"api.read", "api.write", "model.promote"} <= actions


async def test_local_login_can_be_disabled(client, monkeypatch):
    import routers.auth as auth_router
    import routers.sso as sso_router

    from tests.conftest import ADMIN_PW

    # Patch the object each router holds: another test may reload the `settings` module, and a
    # fresh `from settings import settings` would then patch a copy nobody reads (order-dependent).
    monkeypatch.setattr(auth_router.settings, "dashboard_local_login", False)
    monkeypatch.setattr(sso_router.settings, "dashboard_local_login", False)
    resp = await client.post("/api/auth/login", json={"password": ADMIN_PW})
    assert resp.status_code == 403 and "organisation" in resp.json()["detail"]
    assert (await client.get("/api/auth/sso/providers")).json()["local_login"] is False


async def test_many_entitlements_do_not_blow_the_session_cookie(client, center):
    """Browsers drop cookies over ~4 KB silently; a user with many groups must still get in."""
    idp, _, _ = center
    groups = ["mlops-ops"] + [
        f"urn:geant:helmholtz.de:group:some-very-long-collaboration-name-{i}#login.helmholtz.de"
        for i in range(200)
    ]
    *_, cb = await _sso_login(client, idp, groups=groups)
    session = _cookies(cb)["__Host-examlops_session"]["value"]
    assert len(session) < 3500, len(session)
    me = await client.get("/api/auth/me", headers={"Cookie": f"__Host-examlops_session={session}"})
    assert me.status_code == 200 and me.json()["role"] == "operator"


async def test_step_up_covers_the_challenger_promote_door_too(client, monkeypatch):
    """Same authority, same gate — `/api/challenger/{model}/promote` promotes to production.

    `model.promote` is a step-up capability, and the challenger router said so in its own module
    docstring: it reuses `model.promote` "which is a step-up capability … rather than inventing a
    third name". It reused the capability *name* and checked it with a bare `can()`, which never
    reaches `iam_gate.enforce` — where both RFC 9470 step-up and the federated centre's PDP veto
    live. So with step-up enforced, `/api/models/…/alias` demanded re-authentication (the test
    above) while this door to the same action did not.
    """
    from tests.conftest import ADMIN_PW

    token = (await client.post("/api/auth/login", json={"password": ADMIN_PW})).json()["token"]
    monkeypatch.setenv("EXAMLOPS_IAM_STEP_UP", "enforce")
    monkeypatch.setenv("EXAMLOPS_IAM_STEP_UP_MAX_AGE", "0")
    import time

    time.sleep(1.1)
    resp = await client.post(
        "/api/challenger/JPCP/promote", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401, f"promotion ran without step-up: {resp.status_code}"
    assert "insufficient_user_authentication" in resp.headers.get("www-authenticate", "")


async def test_the_pdp_is_asked_about_traffic_manage_not_only_api_write(client, center):
    """A centre policy written in capability terms must reach the routes that use that capability.

    Two PDP questions exist at different granularities: `require_role` asks `api.read`/`api.write`
    for every authenticated route, and `require_capability` asks about the **named capability**. A
    centre that forbids `traffic.manage` — the vocabulary ADR 0120's own example uses — had no way
    to stop an A/B start, because that route checked the capability with a bare `can()` and the PDP
    only ever saw `api.write`.
    """
    idp, pdp, write = center
    pdp.policy = lambda req: req["action"]["name"] not in {"traffic.manage"}
    write(authorization={"mode": "both", "pdp": {"type": "authzen", "url": pdp.url}})
    *_, cb = await _sso_login(client, idp, groups=["mlops-admins"])
    cookie = f"__Host-examlops_session={_cookies(cb)['__Host-examlops_session']['value']}"

    vetoed = await client.post(
        "/api/v1/traffic/ab/start",
        json={"model": "JPCP", "champion": "1", "challenger": "2"},
        headers={"Cookie": cookie, "X-ExaMLOps-CSRF": "1"},
    )
    assert vetoed.status_code == 403, f"the centre's veto did not reach this route: {vetoed.text}"
    assert "traffic.manage" in {body["action"]["name"] for _, body in pdp.requests}


async def test_the_pdp_veto_reaches_a_converted_router(client, center):
    """A second capability, on one of the 42 routes converted in the same batch.

    The traffic test above proves the mechanism; this proves the batch. `secrets.manage` governs
    writing a secret, and a centre that forbids it must be able to stop the write — which it could
    not while the route checked the capability with a bare `can()` and the PDP saw only `api.write`.
    """
    idp, pdp, write = center
    pdp.policy = lambda req: req["action"]["name"] not in {"secrets.manage"}
    write(authorization={"mode": "both", "pdp": {"type": "authzen", "url": pdp.url}})
    *_, cb = await _sso_login(client, idp, groups=["mlops-admins"])
    cookie = f"__Host-examlops_session={_cookies(cb)['__Host-examlops_session']['value']}"

    vetoed = await client.post(
        "/api/secrets",
        json={"path": "prod/token", "value": "whatever"},
        headers={"Cookie": cookie, "X-ExaMLOps-CSRF": "1"},
    )
    assert vetoed.status_code == 403, f"the centre's veto did not reach this route: {vetoed.text}"
    assert "secrets.manage" in {body["action"]["name"] for _, body in pdp.requests}


async def test_the_pdp_sees_the_cli_capability_the_command_actually_needs(client, center):
    """The console's capability is per request, and the centre's policy has to see the real one.

    `cli.py` cannot name its capability in a route-level dependency: `cli.run` or `cli.write` is
    chosen from the tier of the `exa` command inside the request, and that is only known after the
    argv is built. So it calls `iam_gate.enforce` itself once the answer exists. Before that, a
    federated caller's centre saw only the coarse `api.write` for `/api/v1/cli/runs`, whatever
    command was in the body — a centre could not permit read-only `exa` use while forbidding the
    mutating kind, which is the whole point of having two capabilities.
    """
    idp, pdp, write = center
    pdp.policy = lambda req: req["action"]["name"] not in {"cli.write"}
    write(authorization={"mode": "both", "pdp": {"type": "authzen", "url": pdp.url}})
    *_, cb = await _sso_login(client, idp, groups=["mlops-admins"])
    cookie = f"__Host-examlops_session={_cookies(cb)['__Host-examlops_session']['value']}"
    headers = {"Cookie": cookie, "X-ExaMLOps-CSRF": "1"}

    # A read-tier command: the centre permits `cli.run`, so this is not refused by policy.
    allowed = await client.post(
        "/api/v1/cli/runs", json={"command": "status", "args": {}}, headers=headers
    )
    assert allowed.status_code != 403, f"a permitted read was refused: {allowed.text}"
    assert "cli.run" in {body["action"]["name"] for _, body in pdp.requests}


async def test_the_pdp_can_forbid_only_the_mutating_half_of_the_cli_console(client, center):
    """The other side of the same policy: `cli.write` refused, and the refusal is the centre's."""
    idp, pdp, write = center
    pdp.policy = lambda req: req["action"]["name"] not in {"cli.write"}
    write(authorization={"mode": "both", "pdp": {"type": "authzen", "url": pdp.url}})
    *_, cb = await _sso_login(client, idp, groups=["mlops-admins"])
    cookie = f"__Host-examlops_session={_cookies(cb)['__Host-examlops_session']['value']}"

    vetoed = await client.get(
        "/api/v1/cli/workspace", headers={"Cookie": cookie, "X-ExaMLOps-CSRF": "1"}
    )
    assert vetoed.status_code == 403, f"the centre's veto did not reach this route: {vetoed.text}"
    assert "cli.write" in {body["action"]["name"] for _, body in pdp.requests}
