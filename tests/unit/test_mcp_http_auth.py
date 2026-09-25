"""ADR 0082 layer 3 — OAuth 2.1 resource server on the MCP Streamable-HTTP transport.

Tokens are minted and verified for real: a loopback fake IdP (tests/unit/_iam_fakes.py) serves
discovery + JWKS, and the guard verifies through the platform verifier (ADR 0120). The FastMCP app
behind the guard is a recording ASGI stub, so what is asserted is what the guard let through.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import yaml

from examlops import iam
from examlops.mcp import http_auth
from examlops.mcp.http_auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    HttpAuthSettings,
    McpAuthConfigError,
    McpHttpGuard,
    load_settings,
)
from tests.unit._iam_fakes import FakeIdP

RESOURCE = "https://mcp.example.org/mcp"
#: The user's platform role (ADR 0120) — scope tests run as an admin so only the scope decides.
ADMIN_GROUPS = ["examlops-admins"]


@pytest.fixture
def idp():
    server = FakeIdP()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    for var in (
        "EXAMLOPS_IAM_CONFIG",
        "EXAMLOPS_OIDC_ISSUER",
        "EXAMLOPS_MCP_AUTH",
        "EXAMLOPS_MCP_RESOURCE",
        "EXAMLOPS_MCP_ALLOWED_ORIGINS",
        "EXAMLOPS_MCP_MAX_BODY_BYTES",
        "EXAMLOPS_PRINCIPAL_KIND",
    ):
        monkeypatch.delenv(var, raising=False)
    import examlops.policy as policy

    monkeypatch.setattr(policy, "_load_policies", lambda path=None: [])
    iam.clear_caches()
    yield
    iam.clear_caches()


def _trust(tmp_path, monkeypatch, idp: FakeIdP) -> None:
    path = tmp_path / "identity-providers.yaml"
    provider = {"name": "c", "issuer": idp.issuer, "audience": "examlops", "tenant": "c"}
    path.write_text(yaml.safe_dump({"providers": [provider]}))
    monkeypatch.setenv("EXAMLOPS_IAM_CONFIG", str(path))
    iam.clear_caches()


def _oauth(tmp_path, monkeypatch, idp: FakeIdP) -> HttpAuthSettings:
    _trust(tmp_path, monkeypatch, idp)
    monkeypatch.setenv("EXAMLOPS_MCP_AUTH", "oauth")
    monkeypatch.setenv("EXAMLOPS_MCP_RESOURCE", RESOURCE)
    return load_settings("0.0.0.0")


class Recorder:
    """The app behind the guard: records what reached it and echoes the body it received."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send) -> None:
        body = b""
        if scope["type"] == "http":
            msg = await receive()
            body = msg.get("body", b"")
        self.calls.append({"path": scope.get("path"), "body": body})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"inner":true}'})


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test")


def _call(name: str, arguments: dict[str, Any] | None = None, *, id_: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _audit_actions() -> list[dict[str, Any]]:
    from examlops.mcp import tools

    return tools.recent_audit_events(limit=100)["events"]


# ── settings ──────────────────────────────────────────────────────────────────


def test_default_mode_is_none_and_origin_checking_still_applies():
    s = load_settings("127.0.0.1")
    assert s.mode == "none" and not s.oauth and s.allow_loopback_origins


@pytest.mark.parametrize(
    "env, needle",
    [
        ({"EXAMLOPS_MCP_AUTH": "basic"}, "must be one of"),
        ({"EXAMLOPS_MCP_AUTH": "oauth"}, "EXAMLOPS_MCP_RESOURCE"),
        (
            {"EXAMLOPS_MCP_AUTH": "oauth", "EXAMLOPS_MCP_RESOURCE": "http://mcp.example.org/mcp"},
            "https",
        ),
        (
            {"EXAMLOPS_MCP_AUTH": "oauth", "EXAMLOPS_MCP_RESOURCE": "https://x.org/mcp#frag"},
            "fragment",
        ),
        ({"EXAMLOPS_MCP_AUTH": "oauth", "EXAMLOPS_MCP_RESOURCE": RESOURCE}, "identity provider"),
    ],
)
def test_unsafe_or_incomplete_oauth_config_refuses_to_start(monkeypatch, env, needle):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(McpAuthConfigError, match=needle):
        load_settings("0.0.0.0")


def test_oauth_settings_name_the_trusted_issuers(tmp_path, monkeypatch, idp):
    s = _oauth(tmp_path, monkeypatch, idp)
    assert s.oauth and s.resource == RESOURCE
    assert s.authorization_servers == (idp.issuer,)
    assert "https://mcp.example.org" in s.allowed_origins
    assert not s.allow_loopback_origins  # bound to 0.0.0.0


# ── scope catalogue ───────────────────────────────────────────────────────────


def test_scopes_follow_the_tool_tier():
    assert SCOPE_READ in http_auth.required_scopes("list_models")
    assert http_auth.required_scopes("set_traffic_split") == (
        SCOPE_WRITE,
        SCOPE_ADMIN,
        "mcp:tool:set_traffic_split",
    )
    assert http_auth.required_scopes("set_promotion_rule") == (
        SCOPE_ADMIN,
        "mcp:tool:set_promotion_rule",
    )
    assert http_auth.required_scopes("no_such_tool") == (SCOPE_ADMIN, "mcp:tool:no_such_tool")


def test_plan_tools_inherit_the_scope_of_what_they_plan():
    assert http_auth.required_scopes_for_call(
        "plan_change", {"tool": "set_promotion_rule"}
    ) == http_auth.required_scopes("set_promotion_rule")
    from examlops.mcp import tools

    plan = tools.plan_change(
        "set_promotion_rule", {"model": "JPCP", "metric": "rmse", "operator": "<", "threshold": 5}
    )["plan"]
    assert http_auth.required_scopes_for_call(
        "apply_plan", {"plan_hash": plan["plan_hash"]}
    ) == http_auth.required_scopes("set_promotion_rule")


def test_scope_catalog_covers_every_tool():
    from examlops.mcp.tools import iter_tools

    cat = http_auth.scope_catalog()
    assert set(cat["coarse"]) == {SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN}
    assert set(cat["tools"]) == {s.name for s in iter_tools(include_writes=True)}


# ── Origin validation (every mode) ───────────────────────────────────────────


async def test_foreign_origin_is_refused_even_without_oauth():
    inner = Recorder()
    app = McpHttpGuard(inner, load_settings("127.0.0.1"))
    async with _client(app) as c:
        bad = await c.post("/mcp", json=_call("list_models"), headers={"Origin": "https://evil.io"})
        ok = await c.post(
            "/mcp", json=_call("list_models"), headers={"Origin": "http://localhost:6274"}
        )
        no_origin = await c.post("/mcp", json=_call("list_models"))
    assert bad.status_code == 403 and bad.json()["error"] == "invalid_origin"
    assert ok.status_code == 200 and no_origin.status_code == 200
    assert len(inner.calls) == 2


async def test_allowed_origin_list_is_honoured(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_ALLOWED_ORIGINS", "https://console.example.org/")
    app = McpHttpGuard(Recorder(), load_settings("0.0.0.0"))
    async with _client(app) as c:
        ok = await c.get("/mcp", headers={"Origin": "https://console.example.org"})
        loop = await c.get("/mcp", headers={"Origin": "http://127.0.0.1:3000"})
    assert ok.status_code == 200
    assert loop.status_code == 403  # loopback origins only when the server itself is loopback


# ── OAuth resource server ─────────────────────────────────────────────────────


async def test_protected_resource_metadata_is_public(tmp_path, monkeypatch, idp):
    app = McpHttpGuard(Recorder(), _oauth(tmp_path, monkeypatch, idp))
    async with _client(app) as c:
        root = await c.get("/.well-known/oauth-protected-resource")
        suffixed = await c.get("/.well-known/oauth-protected-resource/mcp")
        post = await c.post("/.well-known/oauth-protected-resource")
    assert root.status_code == 200 and suffixed.status_code == 200
    doc = root.json()
    assert doc["resource"] == RESOURCE
    assert doc["authorization_servers"] == [idp.issuer]
    assert SCOPE_READ in doc["scopes_supported"]
    assert doc["bearer_methods_supported"] == ["header"]
    assert post.status_code == 405


async def test_missing_token_gets_a_401_challenge_naming_the_metadata(tmp_path, monkeypatch, idp):
    inner = Recorder()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    async with _client(app) as c:
        r = await c.post("/mcp", json=_call("list_models"))
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        'resource_metadata="https://mcp.example.org/.well-known/oauth-protected-resource/mcp"'
        in (challenge)
    )
    assert inner.calls == []


async def test_token_for_another_audience_is_refused(tmp_path, monkeypatch, idp):
    inner = Recorder()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    token = idp.mint(scope=SCOPE_ADMIN)  # aud="examlops": the control plane's audience
    async with _client(app) as c:
        r = await c.post("/mcp", json=_call("list_models"), headers=_bearer(token))
    assert r.status_code == 401 and r.json()["error"] == "invalid_token"
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert inner.calls == []
    assert any(e["action"] == "mcp_http_denied" for e in _audit_actions())


async def test_expired_and_forged_tokens_are_refused(tmp_path, monkeypatch, idp):
    import time

    app = McpHttpGuard(Recorder(), _oauth(tmp_path, monkeypatch, idp))
    expired = idp.mint(
        groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_READ, exp=int(time.time()) - 3600
    )
    forged = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_READ)[:-4] + "AAAA"
    async with _client(app) as c:
        for token in (expired, forged, "not-a-jwt"):
            r = await c.post("/mcp", json=_call("list_models"), headers=_bearer(token))
            assert r.status_code == 401, token


async def test_read_scope_reaches_reads_but_not_writes(tmp_path, monkeypatch, idp):
    inner = Recorder()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    token = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_READ)
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with _client(app) as c:
        a = await c.post("/mcp", json=listing, headers=_bearer(token))
        b = await c.post("/mcp", json=_call("list_models"), headers=_bearer(token))
        w = await c.post(
            "/mcp",
            json=_call("set_traffic_split", {"model": "JPCP", "production": 90, "canary": 10}),
            headers=_bearer(token),
        )
    assert a.status_code == 200 and b.status_code == 200
    # The body the app received is exactly what the client sent (buffered and replayed).
    assert json.loads(inner.calls[1]["body"]) == _call("list_models")
    assert w.status_code == 403 and w.json()["error"] == "insufficient_scope"
    assert (
        f'scope="{SCOPE_WRITE} {SCOPE_ADMIN} mcp:tool:set_traffic_split"'
        in (w.headers["www-authenticate"])
    )
    assert len(inner.calls) == 2
    denied = [e for e in _audit_actions() if e["action"] == "mcp_http_denied"]
    assert denied and denied[0]["actor"] == "c:alice" and denied[0]["target"] == "set_traffic_split"


async def test_write_scope_allows_tier_a_and_audits_it_but_not_tier_b(tmp_path, monkeypatch, idp):
    inner = Recorder()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    token = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_WRITE)
    rule = {"model": "JPCP", "metric": "rmse", "operator": "<", "threshold": 5.0}
    async with _client(app) as c:
        a = await c.post(
            "/mcp",
            json=_call("set_traffic_split", {"model": "JPCP", "production": 90, "canary": 10}),
            headers=_bearer(token),
        )
        b = await c.post("/mcp", json=_call("set_promotion_rule", rule), headers=_bearer(token))
    assert a.status_code == 200
    assert b.status_code == 403
    authorized = [e for e in _audit_actions() if e["action"] == "mcp_http_tool_authorized"]
    assert [e["target"] for e in authorized] == ["set_traffic_split"]
    assert authorized[0]["actor"] == "c:alice"


async def test_a_per_tool_scope_grants_exactly_that_tool(tmp_path, monkeypatch, idp):
    app = McpHttpGuard(Recorder(), _oauth(tmp_path, monkeypatch, idp))
    token = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope="mcp:tool:set_promotion_rule")
    rule = {"model": "JPCP", "metric": "rmse", "operator": "<", "threshold": 5.0}
    async with _client(app) as c:
        ok = await c.post("/mcp", json=_call("set_promotion_rule", rule), headers=_bearer(token))
        other = await c.post("/mcp", json=_call("grant_access"), headers=_bearer(token))
    assert ok.status_code == 200 and other.status_code == 403


async def test_apply_plan_needs_the_planned_tools_scope(tmp_path, monkeypatch, idp):
    from examlops.mcp import tools

    app = McpHttpGuard(Recorder(), _oauth(tmp_path, monkeypatch, idp))
    plan = tools.plan_change(
        "set_promotion_rule", {"model": "JPCP", "metric": "rmse", "operator": "<", "threshold": 5}
    )["plan"]
    write = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_WRITE)
    admin = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_ADMIN)
    call = _call("apply_plan", {"plan_hash": plan["plan_hash"]})
    async with _client(app) as c:
        denied = await c.post("/mcp", json=call, headers=_bearer(write))
        allowed = await c.post("/mcp", json=call, headers=_bearer(admin))
    assert denied.status_code == 403 and allowed.status_code == 200


async def test_one_forbidden_call_in_a_batch_refuses_the_batch(tmp_path, monkeypatch, idp):
    inner = Recorder()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    token = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_READ)
    batch = [_call("list_models", id_=1), _call("grant_access", id_=2)]
    async with _client(app) as c:
        r = await c.post("/mcp", json=batch, headers=_bearer(token))
    assert r.status_code == 403 and inner.calls == []


async def test_a_token_without_any_mcp_scope_is_refused(tmp_path, monkeypatch, idp):
    app = McpHttpGuard(Recorder(), _oauth(tmp_path, monkeypatch, idp))
    token = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope="openid profile")
    async with _client(app) as c:
        r = await c.get("/mcp", headers=_bearer(token))
    assert r.status_code == 403 and r.json()["error"] == "insufficient_scope"


async def test_an_oversized_body_is_refused_before_parsing(tmp_path, monkeypatch, idp):
    monkeypatch.setenv("EXAMLOPS_MCP_MAX_BODY_BYTES", "256")
    inner = Recorder()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    token = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_ADMIN)
    async with _client(app) as c:
        r = await c.post(
            "/mcp", json=_call("list_models", {"pad": "x" * 1024}), headers=_bearer(token)
        )
    assert r.status_code == 413 and inner.calls == []


async def test_lifespan_passes_through_and_websocket_is_closed_under_oauth(
    tmp_path, monkeypatch, idp
):
    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    sent: list[dict[str, Any]] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {}

    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    await app({"type": "lifespan"}, receive, send)
    await app({"type": "websocket", "headers": []}, receive, send)
    assert seen == ["lifespan"]
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_a_verifier_crash_is_a_401_not_a_500(tmp_path, monkeypatch, idp):
    def boom(token, resource):
        raise RuntimeError("jwks endpoint on fire")

    app = McpHttpGuard(Recorder(), _oauth(tmp_path, monkeypatch, idp), verifier=boom)
    async with _client(app) as c:
        r = await c.post("/mcp", json=_call("list_models"), headers=_bearer("x.y.z"))
    assert r.status_code == 401


# ── server wiring ─────────────────────────────────────────────────────────────


class _FakeFastMCP:
    def __init__(self, name):
        self.name = name

    def tool(self, **kw):
        return lambda fn: fn

    def resource(self, *a, **k):
        return lambda fn: fn

    def prompt(self, **kw):
        return lambda fn: fn

    def http_app(self):
        return Recorder()


def test_remote_bind_without_oauth_is_refused(monkeypatch):
    from examlops.mcp import server

    monkeypatch.setattr(server, "_import_fastmcp", lambda: _FakeFastMCP)
    monkeypatch.setattr(server, "_run_http", lambda *a, **k: pytest.fail("must not serve"))
    with pytest.raises(server.UnsafeMCPBind):
        server.serve(transport="http", host="0.0.0.0", port=1)


def test_remote_bind_with_oauth_serves_the_guarded_app(tmp_path, monkeypatch, idp):
    from examlops.mcp import server

    _oauth(tmp_path, monkeypatch, idp)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(server, "_import_fastmcp", lambda: _FakeFastMCP)
    monkeypatch.setattr(
        server, "_run_http", lambda app, host, port: captured.update(app=app, host=host)
    )
    server.serve(transport="http", host="0.0.0.0", port=8765)
    assert isinstance(captured["app"], McpHttpGuard)
    assert captured["app"].settings.oauth and captured["host"] == "0.0.0.0"


def test_loopback_without_oauth_is_still_origin_guarded(monkeypatch):
    from examlops.mcp import server

    captured: dict[str, Any] = {}
    monkeypatch.setattr(server, "_import_fastmcp", lambda: _FakeFastMCP)
    monkeypatch.setattr(server, "_run_http", lambda app, host, port: captured.update(app=app))
    server.serve(transport="http", host="127.0.0.1", port=8765)
    assert isinstance(captured["app"], McpHttpGuard) and not captured["app"].settings.oauth


def test_broken_oauth_config_stops_the_server(monkeypatch):
    from examlops.mcp import server

    monkeypatch.setenv("EXAMLOPS_MCP_AUTH", "oauth")
    monkeypatch.setattr(server, "_import_fastmcp", lambda: _FakeFastMCP)
    monkeypatch.setattr(server, "_run_http", lambda *a, **k: pytest.fail("must not serve"))
    with pytest.raises(McpAuthConfigError):
        server.serve(transport="http", host="127.0.0.1", port=1)


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_exa_mcp_scopes_json():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    result = CliRunner().invoke(app, ["--json", "mcp", "scopes"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["tools"]["set_promotion_rule"]["scopes"] == [
        SCOPE_ADMIN,
        "mcp:tool:set_promotion_rule",
    ]
    assert data["tools"]["list_models"]["tier"] == "read"


def test_exa_mcp_serve_reports_a_broken_oauth_config(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.mcp import server

    monkeypatch.setenv("EXAMLOPS_MCP_AUTH", "oauth")
    monkeypatch.setattr(server, "_import_fastmcp", lambda: _FakeFastMCP)
    result = CliRunner().invoke(app, ["mcp", "serve", "--transport", "http"])
    assert result.exit_code != 0
    assert "EXAMLOPS_MCP_RESOURCE" in result.output


# ── review hardening: user role, fail-closed resolution, parsing, sessions ───


async def test_a_scope_never_exceeds_the_users_platform_role(tmp_path, monkeypatch, idp):
    """Scopes say what the client was granted; the role says what the *user* may do (ADR 0120)."""
    inner = Recorder()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    viewer = idp.mint(groups=["examlops-viewers"], aud=RESOURCE, scope=SCOPE_ADMIN)
    operator = idp.mint(groups=["examlops-operators"], aud=RESOURCE, scope=SCOPE_ADMIN)
    rule = {"model": "JPCP", "metric": "rmse", "operator": "<", "threshold": 5.0}
    split = {"model": "JPCP", "production": 90, "canary": 10}
    async with _client(app) as c:
        v_read = await c.post("/mcp", json=_call("list_models"), headers=_bearer(viewer))
        v_write = await c.post(
            "/mcp", json=_call("set_traffic_split", split), headers=_bearer(viewer)
        )
        o_write = await c.post(
            "/mcp", json=_call("set_traffic_split", split), headers=_bearer(operator)
        )
        o_admin = await c.post(
            "/mcp", json=_call("set_promotion_rule", rule), headers=_bearer(operator)
        )
    assert v_read.status_code == 200
    assert v_write.status_code == 403 and v_write.json()["error"] == "access_denied"
    assert o_write.status_code == 200
    assert o_admin.status_code == 403 and o_admin.json()["error"] == "access_denied"
    assert len(inner.calls) == 2
    assert any(e["action"] == "authz_denied" for e in _audit_actions())


async def test_an_authorizer_failure_refuses(tmp_path, monkeypatch, idp):
    def boom(principal, action, resource):
        raise RuntimeError("PDP on fire")

    inner = Recorder()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp), authorizer=boom)
    token = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_ADMIN)
    async with _client(app) as c:
        r = await c.post("/mcp", json=_call("list_models"), headers=_bearer(token))
    assert r.status_code == 403 and inner.calls == []


async def test_apply_of_an_unresolvable_plan_needs_the_admin_scope(tmp_path, monkeypatch, idp):
    """An unreadable plan could be tier B/C by the time the tool reads it: fail to the top."""
    app = McpHttpGuard(Recorder(), _oauth(tmp_path, monkeypatch, idp))
    write = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_WRITE)
    call = _call("apply_plan", {"plan_hash": "0" * 64})
    async with _client(app) as c:
        r = await c.post("/mcp", json=call, headers=_bearer(write))
    assert r.status_code == 403 and r.json()["error"] == "insufficient_scope"
    assert http_auth.required_scopes_for_call("apply_plan", {}) == (
        SCOPE_ADMIN,
        "mcp:tool:apply_plan",
    )


async def test_a_single_tool_scope_does_not_read_resources_or_prompts(tmp_path, monkeypatch, idp):
    app = McpHttpGuard(Recorder(), _oauth(tmp_path, monkeypatch, idp))
    token = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope="mcp:tool:list_models")
    read = {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "x"}}
    prompt = {"jsonrpc": "2.0", "id": 2, "method": "prompts/get", "params": {"name": "x"}}
    async with _client(app) as c:
        a = await c.post("/mcp", json=read, headers=_bearer(token))
        b = await c.post("/mcp", json=prompt, headers=_bearer(token))
        ok = await c.post("/mcp", json=_call("list_models"), headers=_bearer(token))
    assert a.status_code == 403 and b.status_code == 403 and ok.status_code == 200


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b"[1, 2]", b'"tools/call"', b"\xff\xfe\x00"],
)
async def test_an_unreadable_body_is_refused_not_forwarded(tmp_path, monkeypatch, idp, body):
    """A body the guard cannot read is never forwarded: no parser differential past the scopes."""
    inner = Recorder()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    token = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_READ)
    headers = {**_bearer(token), "content-type": "application/json"}
    async with _client(app) as c:
        r = await c.post("/mcp", content=body, headers=headers)
    assert r.status_code == 400 and inner.calls == []


class _SessionApp(Recorder):
    """Issues an MCP session id on every response, like FastMCP after initialize."""

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            await receive()
        self.calls.append({"path": scope.get("path"), "body": b""})
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"mcp-session-id", b"sess-alice")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})


async def test_a_session_is_bound_to_the_principal_that_opened_it(tmp_path, monkeypatch, idp):
    inner = _SessionApp()
    app = McpHttpGuard(inner, _oauth(tmp_path, monkeypatch, idp))
    alice = idp.mint(groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_READ)
    bob = idp.mint(
        groups=ADMIN_GROUPS, aud=RESOURCE, scope=SCOPE_READ, sub="u-999", preferred_username="bob"
    )
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    sess = {"mcp-session-id": "sess-alice"}
    async with _client(app) as c:
        opened = await c.post("/mcp", json=init, headers=_bearer(alice))
        again = await c.post("/mcp", json=_call("list_models"), headers={**_bearer(alice), **sess})
        stolen = await c.get("/mcp", headers={**_bearer(bob), **sess})
        killed = await c.delete("/mcp", headers={**_bearer(bob), **sess})
    assert opened.headers["mcp-session-id"] == "sess-alice"
    assert again.status_code == 200
    assert stolen.status_code == 403 and killed.status_code == 403
    assert len(inner.calls) == 2


def test_the_session_binding_table_is_bounded(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_MCP_MAX_BOUND_SESSIONS", "3")
    guard = McpHttpGuard(Recorder(), load_settings("127.0.0.1"))
    for i in range(10):
        guard._bind_session(f"s{i}", "p")
    assert list(guard._sessions) == ["s7", "s8", "s9"]


async def test_anonymous_refusals_are_audited_at_a_bounded_rate(tmp_path, monkeypatch, idp):
    monkeypatch.setenv("EXAMLOPS_MCP_ANON_DENY_AUDIT_PER_MIN", "2")
    app = McpHttpGuard(Recorder(), _oauth(tmp_path, monkeypatch, idp))
    async with _client(app) as c:
        for _ in range(5):
            r = await c.post("/mcp", json=_call("list_models"), headers=_bearer("x.y.z"))
            assert r.status_code == 401
    denied = [e for e in _audit_actions() if e["action"] == "mcp_http_denied"]
    assert len(denied) == 2


async def test_a_websocket_from_a_foreign_origin_is_refused_without_oauth():
    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    sent: list[dict[str, Any]] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {}

    app = McpHttpGuard(inner, load_settings("127.0.0.1"))
    evil = {"type": "websocket", "headers": [(b"origin", b"https://evil.io")]}
    local = {"type": "websocket", "headers": [(b"origin", b"http://localhost:1")]}
    await app(evil, receive, send)
    await app(local, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert seen == ["websocket"]
