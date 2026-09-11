"""ADR 0120 — federated identity & delegated authorization: the `examlops.iam` core.

Everything runs against a real-HTTP fake IdP/PDP on loopback (tests/unit/_iam_fakes.py), so the
verifier does genuine discovery, JWKS fetches, rotation refetches, introspection and AuthZEN/OPA
calls. Each security property is asserted from the attacker's side: the token that must NOT pass.
"""

from __future__ import annotations

import json
import os
import stat
import time

import jwt
import pytest
import yaml

from examlops import iam
from examlops.iam import claims as iam_claims
from examlops.iam import config as iam_config
from examlops.iam import flows, pdp, session, stepup
from examlops.iam.tokens import AuthenticationError, verify_access_token
from tests.unit._iam_fakes import FakeIdP, FakePdp


@pytest.fixture
def idp():
    server = FakeIdP()
    yield server
    server.stop()


@pytest.fixture
def idp_b():
    server = FakeIdP(issuer_suffix="/realms/b")
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_IAM_CONFIG", raising=False)
    monkeypatch.delenv("EXAMLOPS_OIDC_ISSUER", raising=False)
    iam.clear_caches()
    yield
    iam.clear_caches()


def _provider(idp: FakeIdP, **extra) -> dict:
    return {
        "name": "jsc",
        "display_name": "Jülich",
        "issuer": idp.issuer,
        "audience": "examlops",
        "tenant": "jsc",
        "default_role": None,
        **extra,
    }


def _write(tmp_path, monkeypatch, providers: list[dict]) -> None:
    path = tmp_path / "identity-providers.yaml"
    path.write_text(yaml.safe_dump({"providers": providers}))
    monkeypatch.setenv("EXAMLOPS_IAM_CONFIG", str(path))
    iam.clear_caches()


# ── configuration ─────────────────────────────────────────────────────────────


def _errors(provider: dict) -> list[str]:
    return iam_config.parse_config({"providers": [provider]})[1]


def test_valid_trust_file_parses_with_defaults():
    cfg, errors = iam_config.parse_config(
        {"providers": [{"name": "jsc", "issuer": "https://idp.fz-juelich.de/", "audience": "x"}]}
    )
    assert errors == []
    p = cfg.by_name("jsc")
    assert p.tenant == "jsc"  # the center is its own tenant by default
    assert p.discovery is True
    assert "entitlements" in p.group_claims  # AARC-G069 claim looked at by default
    assert cfg.by_issuer("https://idp.fz-juelich.de") is None  # exact match, no normalisation


@pytest.mark.parametrize(
    "patch, needle",
    [
        ({"algorithms": ["HS256"]}, "only asymmetric"),
        ({"algorithms": ["none"]}, "only asymmetric"),
        ({"issuer": "http://idp.example.org"}, "plain HTTP"),
        ({"audience": None}, "audience: required"),
        ({"tenant": None, "tenant_claim": "org"}, "tenants_allowed: required"),
        ({"tenant_claim": "org"}, "tenant OR tenant_claim"),
        ({"surprise": 1}, "unknown keys"),
        ({"default_role": "root"}, "default_role"),
        ({"role_rules": [{"value": "x", "role": "superuser"}]}, "role"),
        ({"role_rules": [{"value": "(", "role": "admin", "match": "regex"}]}, "invalid regex"),
        ({"authorization": {"mode": "both"}}, "needs a pdp"),
        ({"clients": {"dashboard": {"client_id": "d"}}}, "confidential"),
        ({"leeway_s": 3600}, "leeway_s"),
    ],
)
def test_invalid_trust_entries_are_refused(patch, needle):
    base = {"name": "c", "issuer": "https://idp.example.org", "audience": "examlops", "tenant": "c"}
    base.update(patch)
    errs = _errors({k: v for k, v in base.items() if v is not None})
    assert any(needle in e for e in errs), errs


def test_duplicate_issuer_is_refused():
    p = {"issuer": "https://idp.example.org", "audience": "a"}
    _, errs = iam_config.parse_config({"providers": [{"name": "a", **p}, {"name": "b", **p}]})
    assert any("duplicate issuer" in e for e in errs)


def test_invalid_trust_file_fails_closed(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [{"name": "x", "issuer": "https://i", "algorithms": ["HS256"]}])
    with pytest.raises(iam.IamConfigError):
        iam.load_config()
    assert iam.is_enabled() is False
    with pytest.raises(AuthenticationError, match="misconfigured"):
        verify_access_token("a.b.c")


def test_legacy_env_config_still_works(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_OIDC_ISSUER", "https://idp.example.org/")
    monkeypatch.setenv("EXAMLOPS_OIDC_AUDIENCE", "examlops")
    cfg = iam.load_config()
    assert cfg.source == "env"
    assert cfg.by_name("default").tenants_allowed == ("*",)


# ── token verification ────────────────────────────────────────────────────────


def test_valid_token_maps_to_principal(idp, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_provider(idp)])
    p = verify_access_token(idp.mint(groups=["examlops-operators"], email="a@fz-juelich.de"))
    assert p.id == "jsc:u-123" and p.actor == "jsc:alice"
    assert p.tenant == "jsc" and p.role == "operator"
    assert p.email == "a@fz-juelich.de" and p.token_type == "jwt"
    assert "raw" not in json.dumps(p.summary())  # summary is JSON-safe and carries no raw claims


def test_es256_accepted_when_allowed(idp, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_provider(idp)])
    assert verify_access_token(idp.mint(alg="ES256")).subject == "u-123"


def test_untrusted_issuer_refused(idp, idp_b, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_provider(idp)])
    with pytest.raises(AuthenticationError, match="not a trusted"):
        verify_access_token(idp_b.mint())


def test_center_b_key_cannot_vouch_for_center_a(idp, idp_b, tmp_path, monkeypatch):
    """B signs a token claiming A's issuer: A's keys must be used, so the signature fails."""
    _write(tmp_path, monkeypatch, [_provider(idp), _provider(idp_b, name="b", tenant="b")])
    forged = idp_b.mint(iss=idp.issuer, kid="k1")
    with pytest.raises(AuthenticationError):
        verify_access_token(forged)


def test_hs256_with_public_key_alg_confusion_refused(idp, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_provider(idp)])
    now = int(time.time())
    forged = jwt.encode(
        {"iss": idp.issuer, "aud": "examlops", "sub": "x", "exp": now + 60},
        "public-key-material-as-hmac-secret",
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(AuthenticationError, match="algorithm"):
        verify_access_token(forged)


@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"aud": "some-other-app"}, "verification failed"),
        ({"exp": 1}, "verification failed"),
        ({"nbf": int(time.time()) + 3600}, "verification failed"),
    ],
)
def test_bad_claims_refused(idp, tmp_path, monkeypatch, overrides, needle):
    _write(tmp_path, monkeypatch, [_provider(idp)])
    with pytest.raises(AuthenticationError, match=needle):
        verify_access_token(idp.mint(**overrides))


def test_tampered_token_refused(idp, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_provider(idp)])
    header, payload, sig = idp.mint().split(".")
    evil = json.loads(jwt.utils.base64url_decode(payload))
    evil["groups"] = ["examlops-admins"]
    forged_payload = jwt.utils.base64url_encode(json.dumps(evil).encode()).decode()
    with pytest.raises(AuthenticationError):
        verify_access_token(f"{header}.{forged_payload}.{sig}")


def test_rfc9068_typ_enforced_when_required(idp, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_provider(idp, require_typ=True)])
    with pytest.raises(AuthenticationError, match="at\\+jwt"):
        verify_access_token(idp.mint())
    assert verify_access_token(idp.mint(headers={"typ": "at+jwt"})).subject == "u-123"


def test_key_rotation_is_picked_up_and_refetch_is_rate_limited(idp, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_provider(idp)])
    verify_access_token(idp.mint())
    hits = idp.jwks_hits
    idp.rotate("k2")
    assert verify_access_token(idp.mint(kid="k2")).subject == "u-123"  # one forced refetch
    assert idp.jwks_hits == hits + 1
    for _ in range(5):  # random-kid flood: no further refetches inside the min-refresh window
        with pytest.raises(AuthenticationError):
            verify_access_token(idp.mint(kid="k2", headers={"kid": "nope"}))
    assert idp.jwks_hits == hits + 1


def test_discovery_issuer_mismatch_refused(idp, tmp_path, monkeypatch):
    idp.discovery_issuer_override = "https://evil.example.org"
    _write(tmp_path, monkeypatch, [_provider(idp)])
    with pytest.raises(AuthenticationError, match="Discovery"):
        verify_access_token(idp.mint())


def test_issuer_tenant_binding(idp, tmp_path, monkeypatch):
    _write(
        tmp_path,
        monkeypatch,
        [_provider(idp, tenant=None, tenant_claim="org", tenants_allowed=["jsc", "jsc-lab"])],
    )
    assert verify_access_token(idp.mint(org="jsc-lab")).tenant == "jsc-lab"
    with pytest.raises(AuthenticationError, match="not trusted for tenant"):
        verify_access_token(idp.mint(org="cineca"))


def test_opaque_token_introspection(idp, tmp_path, monkeypatch):
    monkeypatch.setenv("RS_SECRET", "rs-secret")
    _write(
        tmp_path,
        monkeypatch,
        [_provider(idp, introspection={"client_id": "rs", "client_secret_ref": "env:RS_SECRET"})],
    )
    idp.opaque["opaque-good"] = {
        "active": True,
        "iss": idp.issuer,
        "aud": "examlops",
        "sub": "u-9",
        "exp": int(time.time()) + 60,
        "groups": ["examlops-admins"],
    }
    p = verify_access_token("opaque-good")
    assert p.token_type == "opaque" and p.role == "admin"
    verify_access_token("opaque-good")
    assert len(idp.introspect_calls) == 1  # cached within its lifetime
    with pytest.raises(AuthenticationError, match="not active"):
        verify_access_token("opaque-unknown")
    idp.opaque["opaque-other-aud"] = {**idp.opaque["opaque-good"], "aud": "other"}
    with pytest.raises(AuthenticationError, match="audience"):
        verify_access_token("opaque-other-aud")


def test_opaque_token_never_sent_to_an_ambiguous_center(idp, idp_b, tmp_path, monkeypatch):
    intr = {"client_id": "rs", "client_secret_ref": "env:RS_SECRET"}
    _write(
        tmp_path,
        monkeypatch,
        [
            _provider(idp, introspection=intr),
            _provider(idp_b, name="b", tenant="b", introspection=intr),
        ],
    )
    with pytest.raises(AuthenticationError, match="ambiguous"):
        verify_access_token("opaque-x")
    assert idp.introspect_calls == [] and idp_b.introspect_calls == []


# ── claim mapping ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, group, role, authority",
    [
        (
            "urn:geant:helmholtz.de:group:examlops:admins#login.helmholtz.de",
            "examlops:admins",
            None,
            "login.helmholtz.de",
        ),
        (
            "urn:mace:egi.eu:group:vo.ml.eu:role=manager#aai.egi.eu",
            "vo.ml.eu",
            "manager",
            "aai.egi.eu",
        ),
        ("urn:geant:example.org:group:ml%20ops", "ml ops", None, None),  # G069: no #authority
    ],
)
def test_aarc_entitlement_parsing(value, group, role, authority):
    ent = iam_claims.parse_entitlement(value)
    assert (ent.group, ent.role, ent.authority) == (group, role, authority)


def _map(provider_patch: dict, token_claims: dict):
    cfg, errs = iam_config.parse_config(
        {
            "providers": [
                {"name": "c", "issuer": "https://i.example", "audience": "a", **provider_patch}
            ]
        }
    )
    assert errs == []
    return iam_claims.map_roles(cfg.providers[0], token_claims)


def test_role_rules_across_idp_dialects():
    rules = {
        "role_rules": [
            {"value": "examlops:admins", "role": "admin"},  # entitlement group path
            {"value": "mlops-ops", "role": "operator"},  # LDAP CN / Keycloak leaf
            {"match": "glob", "value": "hpc-*", "role": "viewer"},
        ]
    }
    ent = "urn:geant:helmholtz.de:group:examlops:admins#login.helmholtz.de"
    assert _map(rules, {"entitlements": [ent]}).role == "admin"
    assert (
        _map(rules, {"groups": ["cn=mlops-ops,ou=groups,dc=fz-juelich,dc=de"]}).role == "operator"
    )
    assert _map(rules, {"groups": ["/jsc/mlops-ops"]}).role == "operator"
    assert _map(rules, {"realm_access": {"roles": ["hpc-users"]}}).role == "viewer"
    assert _map(rules, {"groups": ["unrelated"]}).role is None  # authenticated, not authorized


def test_strongest_role_wins_and_scopes_map():
    m = _map({}, {"groups": ["examlops-viewers"], "scope": "openid examlops.admin"})
    assert m.role == "admin"
    assert any("scope examlops.admin" in why for why in m.matched)


def test_project_scoped_rule_does_not_grant_global_role():
    m = _map(
        {"role_rules": [{"value": "proj-a-leads", "role": "admin", "projects": ["proj-a"]}]},
        {"groups": ["proj-a-leads"]},
    )
    assert m.role is None and m.projects == {"proj-a": "admin"}


def test_assurance_caps_role():
    patch = {
        "role_rules": [{"value": "examlops-admins", "role": "admin"}],
        "default_role": "viewer",
        "role_assurance": {"admin": ["https://refeds.org/assurance/IAP/medium"]},
    }
    low = _map(
        patch,
        {
            "groups": ["examlops-admins"],
            "eduperson_assurance": ["https://refeds.org/assurance/IAP/low"],
        },
    )
    # Capped to the strongest role with no unmet requirement: operator (a subset of admin).
    assert low.role == "operator"
    strict = {
        **patch,
        "role_assurance": {
            "admin": ["https://refeds.org/assurance/IAP/medium"],
            "operator": ["https://refeds.org/assurance/IAP/medium"],
        },
    }
    lowest = _map(
        strict,
        {
            "groups": ["examlops-admins"],
            "eduperson_assurance": ["https://refeds.org/assurance/IAP/low"],
        },
    )
    assert lowest.role == "viewer"
    assert any("assurance cap" in why for why in lowest.matched)
    high = _map(
        patch,
        {
            "groups": ["examlops-admins"],
            "eduperson_assurance": ["https://refeds.org/assurance/IAP/medium"],
        },
    )
    assert high.role == "admin"


# ── authorization ─────────────────────────────────────────────────────────────


def _principal(role="operator", tenant="jsc", provider="jsc", **kw):
    return iam.Principal(
        provider=provider,
        issuer="https://i",
        subject="u1",
        tenant=tenant,
        role=role,
        username="alice",
        **kw,
    )


def _cfg(pdp_cfg: dict | None = None, mode: str = "local"):
    p = {
        "name": "jsc",
        "issuer": "https://i.example",
        "audience": "a",
        "tenant": "jsc",
        "allow_insecure_http": True,
    }
    if pdp_cfg:
        p["authorization"] = {"mode": mode, "pdp": pdp_cfg}
    cfg, errs = iam_config.parse_config({"providers": [p]})
    assert errs == []
    return cfg


def test_local_role_ladder():
    cfg = _cfg()
    assert pdp.authorize(_principal("viewer"), "view", config=cfg).allowed
    assert not pdp.authorize(_principal("viewer"), "model.promote", config=cfg).allowed
    assert pdp.authorize(_principal("operator"), "model.promote", config=cfg).allowed
    assert not pdp.authorize(_principal("operator"), "secret.reveal", config=cfg).allowed
    assert pdp.authorize(_principal("admin"), "never.classified", config=cfg).allowed is not False
    assert not pdp.authorize(_principal("operator"), "never.classified", config=cfg).allowed
    assert not pdp.authorize(_principal(None), "view", config=cfg).allowed


def test_project_role_raises_local_role():
    cfg = _cfg()
    p = _principal("viewer", projects={"proj-a": "operator"})
    assert pdp.authorize(p, "retrain.trigger", {"project": "proj-a"}, config=cfg).allowed
    assert not pdp.authorize(p, "retrain.trigger", {"project": "proj-b"}, config=cfg).allowed


def test_tenant_isolation_is_an_invariant_even_for_admin():
    from examlops.data.audit import export_audit_events

    cfg = _cfg()
    d = pdp.authorize(_principal("admin"), "view", {"tenant": "cineca", "id": "JPCP"}, config=cfg)
    assert not d.allowed and d.layer == "tenant"
    events = [e for e in export_audit_events() if e.get("action") == "authz_denied"]
    assert events and events[0]["actor"] == "jsc:alice"


@pytest.fixture
def center_pdp():
    server = FakePdp()
    yield server
    server.stop()


def test_authzen_request_shape_and_deny_overrides(center_pdp):
    center_pdp.policy = lambda req: req["action"]["name"] != "model.promote"
    cfg = _cfg({"type": "authzen", "url": center_pdp.url}, mode="both")
    ok = pdp.authorize(
        _principal("operator"), "retrain.trigger", {"type": "model", "id": "JPCP"}, config=cfg
    )
    assert ok.allowed and ok.layer == "combined"
    path, body = center_pdp.requests[-1]
    assert path == "/access/v1/evaluation"
    assert body["subject"]["type"] == "user" and body["subject"]["id"] == "jsc:u1"
    assert body["resource"] == {"type": "model", "id": "JPCP", "properties": {"tenant": "jsc"}}
    assert body["action"] == {"name": "retrain.trigger"} and "time" in body["context"]
    no = pdp.authorize(
        _principal("operator"), "model.promote", {"type": "model", "id": "JPCP"}, config=cfg
    )
    assert not no.allowed and no.layer == "pdp" and "center policy" in no.reason
    before = len(center_pdp.requests)
    local_no = pdp.authorize(_principal("viewer"), "model.promote", config=cfg)
    assert not local_no.allowed and len(center_pdp.requests) == before  # local deny short-circuits


def test_external_mode_lets_the_center_decide(center_pdp):
    center_pdp.policy = lambda req: req["subject"]["properties"]["username"] == "alice"
    cfg = _cfg({"type": "authzen", "url": center_pdp.url}, mode="external")
    assert pdp.authorize(_principal("viewer"), "secret.reveal", config=cfg).allowed


@pytest.mark.parametrize(
    "verdict, allowed",
    [(True, True), (False, False), ({"allow": True, "reason": "ok"}, True), (None, False)],
)
def test_opa_result_shapes(center_pdp, verdict, allowed):
    center_pdp.policy = lambda inp: verdict
    cfg = _cfg(
        {"type": "opa", "url": center_pdp.url, "opa_path": "hpc/examlops/allow"}, mode="external"
    )
    d = pdp.authorize(_principal("admin"), "view", config=cfg)
    assert d.allowed is allowed
    assert center_pdp.requests[-1][0] == "/v1/data/hpc/examlops/allow"
    assert "input" in center_pdp.requests[-1][1]


def test_pdp_outage_fails_closed_unless_local_fallback_opted_in(center_pdp):
    center_pdp.fail_with = 503
    closed = _cfg({"type": "authzen", "url": center_pdp.url}, mode="both")
    d = pdp.authorize(_principal("admin"), "view", config=closed)
    assert not d.allowed and "fail closed" in d.reason
    fallback = _cfg({"type": "authzen", "url": center_pdp.url, "on_error": "local"}, mode="both")
    assert pdp.authorize(_principal("admin"), "view", config=fallback).allowed


def test_only_permits_are_cached_and_pdp_bearer_is_sent(center_pdp, monkeypatch):
    monkeypatch.setenv("PDP_TOKEN", "pdp-bearer")
    decisions = iter([True, False, False])
    center_pdp.policy = lambda req: (
        next(decisions) if req["action"]["name"] == "retrain.trigger" else False
    )
    cfg = _cfg(
        {"type": "authzen", "url": center_pdp.url, "token_ref": "env:PDP_TOKEN"}, mode="both"
    )
    p = _principal("operator")
    assert pdp.authorize(p, "retrain.trigger", config=cfg).allowed
    assert pdp.authorize(p, "retrain.trigger", config=cfg).allowed  # permit served from cache
    assert len(center_pdp.requests) == 1
    assert center_pdp.bearer == "Bearer pdp-bearer"
    assert not pdp.authorize(p, "model.promote", config=cfg).allowed
    assert not pdp.authorize(p, "model.promote", config=cfg).allowed
    assert len(center_pdp.requests) == 3  # denies are re-asked, never cached


# ── client flows ──────────────────────────────────────────────────────────────


def test_auth_code_pkce_and_id_token(idp, tmp_path, monkeypatch):
    monkeypatch.setenv("DASH_SECRET", "dash-secret")
    _write(
        tmp_path,
        monkeypatch,
        [
            _provider(
                idp,
                clients={
                    "dashboard": {
                        "client_id": "examlops-dashboard",
                        "client_secret_ref": "env:DASH_SECRET",
                    }
                },
            )
        ],
    )
    provider = iam.load_config().by_name("jsc")
    client = provider.client("dashboard")
    pk = flows.new_pkce()
    assert 43 <= len(pk.verifier) <= 128
    url = flows.authorization_url(
        provider,
        client,
        redirect_uri="http://127.0.0.1/cb",
        state="s",
        nonce="n",
        pkce=pk,
        acr_values=("mfa",),
        max_age=0,
    )
    assert "code_challenge_method=S256" in url and "acr_values=mfa" in url and "max_age=0" in url
    code = idp.issue_code(
        challenge=pk.challenge,
        nonce="n",
        redirect_uri="http://127.0.0.1/cb",
        claims={"groups": ["examlops-admins"]},
    )
    tokens = flows.exchange_code(
        provider, client, code=code, redirect_uri="http://127.0.0.1/cb", pkce=pk
    )
    claims = flows.verify_id_token(provider, client, tokens["id_token"], nonce="n")
    assert claims["sub"] == "u-123"
    with pytest.raises(flows.FlowError, match="nonce"):
        flows.verify_id_token(provider, client, tokens["id_token"], nonce="other")
    # A stolen code is useless without the verifier (PKCE).
    code2 = idp.issue_code(
        challenge=pk.challenge, nonce="n", redirect_uri="http://127.0.0.1/cb", claims={}
    )
    with pytest.raises(flows.FlowError, match="invalid_grant"):
        flows.exchange_code(
            provider, client, code=code2, redirect_uri="http://127.0.0.1/cb", pkce=flows.new_pkce()
        )
    flows.check_authorization_response_issuer(provider, idp.issuer)
    with pytest.raises(flows.FlowError):
        flows.check_authorization_response_issuer(provider, "https://mix-up.example")


def test_device_flow_polls_through_pending_and_slow_down(idp, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_provider(idp, clients={"cli": {"client_id": "exa-cli"}})])
    provider = iam.load_config().by_name("jsc")
    client = provider.client("cli")
    idp.device_pending_error = "slow_down"
    device = flows.device_authorize(provider, client)
    assert device["user_code"] == "ABCD-EFGH"
    sleeps: list[float] = []
    tokens = flows.poll_device_token(provider, client, device, sleep=sleeps.append)
    assert tokens["access_token"]
    assert sleeps == [1, 6, 11]  # interval, then +5 s per slow_down (RFC 8628 §3.5)


def test_device_flow_denied(idp, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [_provider(idp, clients={"cli": {"client_id": "exa-cli"}})])
    provider = iam.load_config().by_name("jsc")
    device = flows.device_authorize(provider, provider.client("cli"))
    idp.devices[device["device_code"]]["state"] = "denied"
    with pytest.raises(flows.FlowError, match="access_denied"):
        flows.poll_device_token(provider, provider.client("cli"), device, sleep=lambda s: None)


# ── CLI session ───────────────────────────────────────────────────────────────


def test_session_store_is_private_and_refreshes_with_rotation(idp, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    rt = "rt-initial"
    idp.refresh_tokens[rt] = {"client_id": "exa-cli", "claims": {"groups": ["examlops-viewers"]}}
    session.save(
        {
            "provider": "jsc",
            "issuer": idp.issuer,
            "client_id": "exa-cli",
            "access_token": "expired",
            "refresh_token": rt,
            "expires_at": int(time.time()) - 10,
        }
    )
    path = session.credentials_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    token = session.current_access_token()
    assert token and token != "expired"
    rec = session.load()
    assert rec["refresh_token"] != rt  # rotated token stored
    assert session.current_access_token() == token  # now fresh: no second refresh
    assert len([r for r in idp.token_requests if r["grant_type"] == "refresh_token"]) == 1
    assert session.delete() and session.current_access_token() == ""


def test_session_delegates_to_oidc_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "oidc-token"
    script.write_text('#!/bin/sh\n[ "$1" = "helmholtz" ] && echo "agent-token-123"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    session.save(
        {
            "provider": "helmholtz",
            "issuer": "https://login.helmholtz.de/oauth2",
            "client_id": "",
            "oidc_agent_account": "helmholtz",
        }
    )
    assert session.current_access_token() == "agent-token-123"
    assert "access_token" not in session.load()  # nothing secret on disk


# ── step-up ───────────────────────────────────────────────────────────────────


def test_step_up_evaluation_and_header():
    policy = iam_config.StepUpConfig(
        enabled=True, acr_values=("https://refeds.org/profile/mfa",), max_age_s=300
    )
    now = time.time()
    assert (
        stepup.evaluate(policy, acr="https://refeds.org/profile/mfa", auth_time=now - 10, now=now)
        is None
    )
    weak = stepup.evaluate(policy, acr="pwd", auth_time=now - 10, now=now)
    assert (
        weak is not None
        and 'acr_values="https://refeds.org/profile/mfa"' in weak.www_authenticate()
    )
    stale = stepup.evaluate(
        policy, acr="https://refeds.org/profile/mfa", auth_time=now - 3600, now=now
    )
    assert stale is not None and "max_age=300" in stale.www_authenticate()
    assert 'error="insufficient_user_authentication"' in stale.www_authenticate()
    assert stepup.evaluate(iam_config.StepUpConfig(), acr=None, auth_time=None) is None  # opt-in


def test_a_permit_for_an_mfa_session_is_not_served_to_a_weaker_session(center_pdp):
    """The permit cache keys on everything the PDP saw about the subject, acr included."""
    center_pdp.policy = lambda req: req["subject"]["properties"]["acr"] == "mfa"
    cfg = _cfg({"type": "authzen", "url": center_pdp.url}, mode="both")
    strong = _principal("operator", acr="mfa")
    weak = _principal("operator", acr=None)
    assert pdp.authorize(strong, "model.promote", config=cfg).allowed
    assert not pdp.authorize(weak, "model.promote", config=cfg).allowed
