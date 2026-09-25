"""`exa gateway validate|status|providers` — operator CLI over the real `llm-gateway` service.

Before this, the service had no CLI at all: an operator had to `curl` raw endpoints to know
whether it was serving, and there was no offline way to catch a bad `gateway.yaml` before it
reached production (ADR 0155 d2 promises `exa gateway validate` as the CI gate; it did not exist).

`validate` is pure and offline (no network); `status`/`providers` talk to a real service over
HTTP, driven here against an in-process ASGI app via `httpx.ASGITransport` — the same fake-service
pattern `test_llm_gateway_service.py` uses, so a passing test means the CLI parses what the service
actually returns.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli import _output
from examlops.cli.commands import gateway_cmd
from examlops.gateway.providers import OllamaProvider
from examlops.gateway.providers.base import run_sync
from examlops.gateway.service.app import create_app

runner = CliRunner()


def invoke_json(app, args):
    """Run a command with `_output.json_mode` on, the way the real `-o json`/`--json` global flag
    does (set by the root `exa` app's callback, which this sub-typer is not invoked through here)."""
    _output.json_mode = True
    try:
        return runner.invoke(app, args)
    finally:
        _output.json_mode = False


GOOD_CFG = {
    "version": 1,
    "providers": {
        "n1": {"type": "ollama", "base_url": "http://127.0.0.1:11434", "locality": "local"}
    },
    "models": {
        "chat": {"deployments": [{"provider": "n1", "model": "qwen3:8b"}], "required": True}
    },
    "aliases": {"default": "chat"},
}
ADMIN = "cli-test-admin-token-0123456789"


class Upstream:
    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "qwen3:8b", "capabilities": ["completion"]}]}
            )
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            if body.get("stream"):
                lines = [
                    json.dumps(
                        {"model": body["model"], "message": {"role": "assistant", "content": "hel"}}
                    ),
                    json.dumps({"model": body["model"], "message": {"content": "lo"}}),
                    json.dumps(
                        {
                            "model": body["model"],
                            "message": {"content": ""},
                            "done": True,
                            "done_reason": "stop",
                            "prompt_eval_count": 3,
                            "eval_count": 2,
                        }
                    ),
                ]
                return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "message": {"role": "assistant", "content": "hello"},
                    "done": True,
                    "done_reason": "stop",
                },
            )
        return httpx.Response(404, json={"error": "not found"})


def make_app(**kw):
    def factory(name, cfg):
        return OllamaProvider(
            name, cfg.base_url, locality=cfg.locality, transport=httpx.MockTransport(Upstream())
        )

    kw.setdefault("config", GOOD_CFG)
    kw.setdefault("auth", "off")
    kw.setdefault("admin_token", ADMIN)
    return create_app(provider_factory=factory, **kw)


class FakeClient:
    """A synchronous stand-in for ``httpx.Client`` bound to an in-process ASGI app.

    ``httpx.ASGITransport`` implements only the async transport interface, so a real sync
    ``httpx.Client`` cannot use it directly; this bridges the same way
    ``examlops.gateway.providers.base.run_sync`` drives an async provider call from sync code, so
    the test double behaves like a real socket without opening one.
    """

    def __init__(self, app):
        self._app = app

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    async def _do(self, method, path, headers, **kw):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://gw"
        ) as c:
            return await c.request(method, path, headers=headers, **kw)

    def get(self, path, headers=None, params=None):
        return run_sync(self._do("GET", path, headers, params=params))

    def post(self, path, headers=None, json=None):
        return run_sync(self._do("POST", path, headers, json=json))

    def stream(self, method, path, *, json=None, headers=None):
        return _FakeStream(self._app, method, path, headers, json)


class _CollectedResponse:
    """A pre-collected stand-in for a streamed ``httpx.Response``. There is no live generator to
    hand back across the sync/async bridge the way a real socket's ``httpx.Client.stream()`` has
    (``run_sync`` drives one coroutine to completion, it does not yield mid-flight) — the whole SSE
    body is read eagerly inside the async bridge below, then replayed through the same
    ``.status_code``/``.read()``/``.json()``/``.iter_lines()`` surface ``_chat_stream`` uses
    against a real one, so the command code under test is identical either way."""

    def __init__(self, status_code, text):
        self.status_code = status_code
        self._text = text

    def read(self):
        return self._text.encode()

    def json(self):
        return json.loads(self._text)

    def iter_lines(self):
        return iter(self._text.splitlines())


class _FakeStream:
    def __init__(self, app, method, path, headers, body):
        self._app, self._method, self._path, self._headers, self._body = (
            app,
            method,
            path,
            headers,
            body,
        )

    def __enter__(self):
        return run_sync(self._collect())

    def __exit__(self, *exc):
        return False

    async def _collect(self):
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._app), base_url="http://gw"
            ) as c,
            c.stream(self._method, self._path, headers=self._headers, json=self._body) as resp,
        ):
            await resp.aread()
            return _CollectedResponse(resp.status_code, resp.text)


@pytest.fixture
def wired(monkeypatch):
    """Point `gateway_cmd`'s HTTP client at an in-process fake service instead of the network."""
    app = make_app()
    monkeypatch.setattr(gateway_cmd, "_sync_client", lambda **kw: FakeClient(app))
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", ADMIN)
    return app


# ── validate (pure, offline) ───────────────────────────────────────────────────


def test_validate_a_good_file_exits_zero_and_says_so(tmp_path):
    p = tmp_path / "gateway.yaml"
    p.write_text(yaml.safe_dump(GOOD_CFG))
    res = runner.invoke(gateway_cmd.app, ["validate", str(p)])
    assert res.exit_code == 0
    assert "valid" in res.output.lower()


def test_validate_reports_every_error_with_its_path_and_exits_nonzero(tmp_path):
    bad = json.loads(json.dumps(GOOD_CFG))
    bad["models"]["chat"]["deployments"][0]["provider"] = "nope"
    bad["providers"]["n1"]["max_concurrency"] = 0
    p = tmp_path / "gateway.yaml"
    p.write_text(yaml.safe_dump(bad))
    res = runner.invoke(gateway_cmd.app, ["validate", str(p)])
    assert res.exit_code == 1
    assert "models.chat.deployments[0].provider" in res.output
    assert "providers.n1.max_concurrency" in res.output


def test_validate_a_missing_file_is_a_clean_error_not_a_traceback(tmp_path):
    res = runner.invoke(gateway_cmd.app, ["validate", str(tmp_path / "nope.yaml")])
    assert res.exit_code == 1
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "traceback" not in res.output.lower()


def test_validate_json_output_is_machine_readable(tmp_path):
    p = tmp_path / "gateway.yaml"
    p.write_text(yaml.safe_dump(GOOD_CFG))
    res = invoke_json(gateway_cmd.app, ["validate", str(p)])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data == {"valid": True, "errors": [], "source": str(p)}

    bad = json.loads(json.dumps(GOOD_CFG))
    bad["aliases"]["ghost"] = "does-not-exist"
    p2 = tmp_path / "bad.yaml"
    p2.write_text(yaml.safe_dump(bad))
    res2 = invoke_json(gateway_cmd.app, ["validate", str(p2)])
    assert res2.exit_code == 1
    data2 = json.loads(res2.output)
    assert data2["valid"] is False and any("aliases.ghost" in e for e in data2["errors"])


def test_validate_defaults_to_the_discovered_config_path(monkeypatch, tmp_path):
    p = tmp_path / "gateway.yaml"
    p.write_text(yaml.safe_dump(GOOD_CFG))
    monkeypatch.setenv("EXAMLOPS_GATEWAY_CONFIG", str(p))
    res = runner.invoke(gateway_cmd.app, ["validate"])
    assert res.exit_code == 0
    # --json, not the human-readable text: Rich word-wraps a long path across lines depending on
    # the detected console width (varies under pytest-xdist's longer `popen-gwN` tmp paths), so an
    # exact substring match on the wrapped prose is inherently flaky; JSON is never wrapped.
    data = json.loads(invoke_json(gateway_cmd.app, ["validate"]).output)
    assert data["source"] == str(p)


def test_validate_with_no_config_anywhere_is_valid_by_generated_default(monkeypatch):
    """No file configured ⇒ the service falls back to a generated config (ADR 0155 d4); the CLI
    must say that rather than treating "nothing to validate" as an error."""
    monkeypatch.delenv("EXAMLOPS_GATEWAY_CONFIG", raising=False)
    monkeypatch.setattr(gateway_cmd, "default_config_path", lambda: None)
    res = runner.invoke(gateway_cmd.app, ["validate"])
    assert res.exit_code == 0
    assert "generated" in res.output.lower()


# ── status (talks to the live service) ─────────────────────────────────────────


def test_status_reports_ready_and_the_route_count(wired):
    res = runner.invoke(gateway_cmd.app, ["status"])
    assert res.exit_code == 0
    assert "ready" in res.output.lower()
    assert "1" in res.output  # one route (chat)


def test_status_refuses_a_forbidden_configured_url_cleanly(monkeypatch):
    """`_gateway_url()` resolves an env var exactly like `gateway_service_backend` does — the same
    ADR 0154 egress check applies here too, so an operator's (or a script's) misconfigured
    `EXAMLOPS_LLM_GATEWAY_URL` cannot make the CLI silently talk to an Azure endpoint.

    Asserts on "denied", never bare "azure": the forbidden URL itself *contains* "azure", so a
    plain connection failure that merely echoes the URL back in its message would satisfy a
    looser check without the validation ever having run — the client is force-broken below so a
    pass here can only mean the refusal happened before any network attempt.
    """

    def must_not_be_called(**kw):
        raise AssertionError("a request was attempted despite the forbidden URL")

    monkeypatch.setattr(gateway_cmd, "_sync_client", must_not_be_called)
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "https://acme.openai.azure.com/v1")
    res = runner.invoke(gateway_cmd.app, ["status"])
    assert res.exit_code == 1
    assert "denied" in res.output.lower() and "azure endpoints are forbidden" in res.output.lower()
    assert "traceback" not in res.output.lower()


def test_status_exits_nonzero_when_the_service_is_unreachable(monkeypatch):
    def refuse(**kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(gateway_cmd, "_sync_client", refuse)
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://127.0.0.1:1")
    res = runner.invoke(gateway_cmd.app, ["status"])
    assert res.exit_code == 1
    assert "unreachable" in res.output.lower() or "cannot" in res.output.lower()


def test_status_exits_nonzero_when_the_service_answers_but_is_not_ready(monkeypatch):
    """A distinct failure mode from being unreachable: the service is up and answering, but no
    deployment is healthy yet (or the required route is down) — `ready: false` in a real 200/503."""
    dead_cfg = {
        **GOOD_CFG,
        "providers": {
            "n1": {"type": "ollama", "base_url": "http://127.0.0.1:1", "locality": "local"}
        },
    }

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def factory(name, cfg):
        return OllamaProvider(
            name, cfg.base_url, locality=cfg.locality, transport=httpx.MockTransport(down)
        )

    app = create_app(provider_factory=factory, config=dead_cfg, auth="off", admin_token=ADMIN)
    monkeypatch.setattr(gateway_cmd, "_sync_client", lambda **kw: FakeClient(app))
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")
    res = runner.invoke(gateway_cmd.app, ["status"])
    assert res.exit_code == 1
    assert "ready: false" in res.output.lower()


def test_status_json_mirrors_the_services_own_ready_document(wired):
    res = invoke_json(gateway_cmd.app, ["status"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["ready"] is True
    assert "chat" in data["routes"]


def test_status_requires_no_admin_token(wired, monkeypatch):
    """/health and /ready are unauthenticated on the service; the CLI must not demand a token
    just to check whether the gateway is up."""
    monkeypatch.delenv("LLM_GATEWAY_ADMIN_TOKEN", raising=False)
    res = runner.invoke(gateway_cmd.app, ["status"])
    assert res.exit_code == 0


# ── providers (admin) ───────────────────────────────────────────────────────────


def test_providers_shows_health_from_the_live_service(wired):
    res = runner.invoke(gateway_cmd.app, ["providers"])
    assert res.exit_code == 0
    assert "n1" in res.output
    assert "ollama" in res.output.lower()


def test_providers_without_an_admin_token_never_issues_the_request(wired, monkeypatch):
    """The early client-side guard is a fast, specific message — distinct from the generic 401 a
    round trip would also produce — and it must fire before the request is sent, not merely
    before the client object is constructed (constructing an ``httpx.Client`` touches no socket;
    only ``.get()``/``.request()`` does)."""
    monkeypatch.delenv("LLM_GATEWAY_ADMIN_TOKEN", raising=False)

    class Tripwire(FakeClient):
        def get(self, *a, **kw):  # pragma: no cover - must never run
            raise AssertionError("a request was sent despite the missing admin token")

    monkeypatch.setattr(gateway_cmd, "_sync_client", lambda **kw: Tripwire(wired))
    res = runner.invoke(gateway_cmd.app, ["providers"])
    assert res.exit_code == 1
    assert "is not set" in res.output.lower()


def test_providers_json_carries_no_secrets(wired):
    """The admin token that authenticates the call must never appear in its own output."""
    res = invoke_json(gateway_cmd.app, ["providers"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert "n1" in data
    assert ADMIN not in res.output


def test_a_rejected_admin_token_is_a_clean_401_not_a_stack_trace(wired, monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", "wrong-token-but-long-enough-16")
    res = runner.invoke(gateway_cmd.app, ["providers"])
    assert res.exit_code == 1
    assert "traceback" not in res.output.lower()
    # The dedicated 401 branch, not the generic "HTTP 4xx: <body>" fallback further down: a
    # rejected token is common enough (rotated, wrong env) to earn its own actionable hint.
    assert "rejected the admin token" in res.output.lower()


# ── models (unauthenticated read, filtered by key) ─────────────────────────────


def test_models_lists_servable_routes_and_aliases(wired):
    res = runner.invoke(gateway_cmd.app, ["models"])
    assert res.exit_code == 0
    assert "chat" in res.output and "default" in res.output  # route + alias, both servable
    assert "n1" in res.output  # the owning provider


def test_models_json_mirrors_the_services_response(wired):
    res = invoke_json(gateway_cmd.app, ["models"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    ids = {m["id"] for m in data["data"]}
    assert {"chat", "default"} <= ids


def test_models_never_prints_the_key_it_was_given(wired):
    """The `--key` value is a virtual key — a secret-shaped value (`_SECRET_PARAMS` in
    `surface.py` names this exact param for that reason) and must never be echoed back."""
    res = runner.invoke(gateway_cmd.app, ["models", "--key", "vk-super-secret-value"])
    assert res.exit_code == 0
    assert "vk-super-secret-value" not in res.output


def test_models_reports_a_forbidden_configured_url_without_any_network_call(monkeypatch):
    def must_not_be_called(**kw):
        raise AssertionError("a request was attempted despite the forbidden URL")

    monkeypatch.setattr(gateway_cmd, "_sync_client", must_not_be_called)
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "https://acme.openai.azure.com/v1")
    res = runner.invoke(gateway_cmd.app, ["models"])
    assert res.exit_code == 1
    assert "denied" in res.output.lower() and "azure endpoints are forbidden" in res.output.lower()


def test_models_unreachable_service_is_a_clean_error(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://127.0.0.1:1")
    res = runner.invoke(gateway_cmd.app, ["models"])
    assert res.exit_code == 1
    assert "unreachable" in res.output.lower()
    assert "traceback" not in res.output.lower()


# ── routes (admin, the full configured topology) ────────────────────────────────


def test_routes_shows_deployments_fallbacks_and_reverse_aliases(wired):
    res = runner.invoke(gateway_cmd.app, ["routes"])
    assert res.exit_code == 0
    assert "chat" in res.output
    assert "n1/qwen3:8b" in res.output  # provider/model, not just the route name
    assert "default" in res.output  # the reverse-resolved alias for "chat"


def test_routes_json_mirrors_the_services_admin_config(wired):
    res = invoke_json(gateway_cmd.app, ["routes"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert "chat" in data["routes"]
    assert data["aliases"]["default"] == "chat"
    assert data["source"] == "file"


def test_routes_without_an_admin_token_never_issues_the_request(wired, monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY_ADMIN_TOKEN", raising=False)

    class Tripwire(FakeClient):
        def get(self, *a, **kw):  # pragma: no cover - must never run
            raise AssertionError("a request was sent despite the missing admin token")

    monkeypatch.setattr(gateway_cmd, "_sync_client", lambda **kw: Tripwire(wired))
    res = runner.invoke(gateway_cmd.app, ["routes"])
    assert res.exit_code == 1
    assert "is not set" in res.output.lower()


def test_routes_reports_a_forbidden_configured_url_without_any_network_call(monkeypatch):
    def must_not_be_called(**kw):
        raise AssertionError("a request was attempted despite the forbidden URL")

    monkeypatch.setattr(gateway_cmd, "_sync_client", must_not_be_called)
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "https://acme.openai.azure.com/v1")
    res = runner.invoke(gateway_cmd.app, ["routes"])
    assert res.exit_code == 1
    assert "denied" in res.output.lower() and "azure endpoints are forbidden" in res.output.lower()


def test_routes_surfaces_a_prior_rejected_reload_as_a_warning(monkeypatch, tmp_path):
    """A route table with no obvious problem can still be hiding a rejected reload attempt — the
    same `last_reload_error` `/admin/config` already carries, surfaced here so an operator checking
    `routes` after a failed `reload` sees why the table didn't change, not just that it didn't."""
    import yaml

    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(GOOD_CFG))
    app = make_app(config=None, config_path=path)
    monkeypatch.setattr(gateway_cmd, "_sync_client", lambda **kw: FakeClient(app))
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", ADMIN)
    assert runner.invoke(gateway_cmd.app, ["status"]).exit_code == 0  # warm it up first

    bad = json.loads(json.dumps(GOOD_CFG))
    bad["models"]["chat"]["deployments"][0]["provider"] = "ghost"
    path.write_text(yaml.safe_dump(bad))
    assert runner.invoke(gateway_cmd.app, ["reload"]).exit_code == 1

    res = runner.invoke(gateway_cmd.app, ["routes"])
    assert res.exit_code == 0  # the route table itself is fine — the warning is additive
    assert "last reload was rejected" in res.output.lower() and "ghost" in res.output


# ── reload (admin, mutating) ────────────────────────────────────────────────────


def test_reload_reports_the_route_count_and_audits_the_action(wired):
    from examlops.data.audit import export_audit_events

    res = runner.invoke(gateway_cmd.app, ["reload"])
    assert res.exit_code == 0
    assert "reloaded" in res.output.lower()
    events = [e for e in export_audit_events() if e["action"] == "gateway_reloaded"]
    assert events, (
        "reload must leave an audit trail, the same as the MCP gateway_service_reload tool"
    )
    assert events[-1]["source"] == "exa-gateway"


def test_reload_without_an_admin_token_never_issues_the_request(wired, monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY_ADMIN_TOKEN", raising=False)

    class Tripwire(FakeClient):
        def post(self, *a, **kw):  # pragma: no cover - must never run
            raise AssertionError("a request was sent despite the missing admin token")

    monkeypatch.setattr(gateway_cmd, "_sync_client", lambda **kw: Tripwire(wired))
    res = runner.invoke(gateway_cmd.app, ["reload"])
    assert res.exit_code == 1
    assert "is not set" in res.output.lower()


def test_reload_rejects_a_forbidden_configured_url_without_any_network_call(monkeypatch):
    def must_not_be_called(**kw):
        raise AssertionError("a request was attempted despite the forbidden URL")

    monkeypatch.setattr(gateway_cmd, "_sync_client", must_not_be_called)
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "https://acme.openai.azure.com/v1")
    res = runner.invoke(gateway_cmd.app, ["reload"])
    assert res.exit_code == 1
    assert "denied" in res.output.lower() and "azure endpoints are forbidden" in res.output.lower()


def test_reload_of_an_invalid_config_keeps_the_last_good_one_serving(monkeypatch, tmp_path):
    """ADR 0155 d3: a rejected reload must never brick the gateway, and the CLI must say exactly
    why — mirroring the service's own `test_reload_keeps_the_last_good_config_...` at the HTTP
    layer, but exercised here through the CLI's own error-formatting path."""
    import yaml

    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(GOOD_CFG))
    app = make_app(config=None, config_path=path)
    monkeypatch.setattr(gateway_cmd, "_sync_client", lambda **kw: FakeClient(app))
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", ADMIN)

    # Warm the service on the good config first — "the previous one keeps serving" is a claim
    # about an already-running gateway; a reload attempt as the process's very first-ever request
    # is the separate cold-start case `test_admin_reload_as_the_very_first_request_...` covers.
    warm = runner.invoke(gateway_cmd.app, ["status"])
    assert warm.exit_code == 0

    bad = json.loads(json.dumps(GOOD_CFG))
    bad["models"]["chat"]["deployments"][0]["provider"] = "ghost"
    path.write_text(yaml.safe_dump(bad))

    res = runner.invoke(gateway_cmd.app, ["reload"])
    assert res.exit_code == 1
    assert "rejected" in res.output.lower() and "ghost" in res.output
    assert "traceback" not in res.output.lower()

    still_up = runner.invoke(gateway_cmd.app, ["status"])
    assert still_up.exit_code == 0 and "ready: true" in still_up.output.lower()


def test_reload_of_an_invalid_config_json_mode_reports_the_errors(monkeypatch, tmp_path):
    import yaml

    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(GOOD_CFG))
    app = make_app(config=None, config_path=path)
    monkeypatch.setattr(gateway_cmd, "_sync_client", lambda **kw: FakeClient(app))
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_TOKEN", ADMIN)
    bad = json.loads(json.dumps(GOOD_CFG))
    bad["models"]["chat"]["deployments"][0]["provider"] = "ghost"
    path.write_text(yaml.safe_dump(bad))

    res = invoke_json(gateway_cmd.app, ["reload"])
    assert res.exit_code == 1
    data = json.loads(res.output)
    assert data["reloaded"] is False and any("ghost" in e for e in data["errors"])


# ── chat --stream (real SSE against the deployed service) ──────────────────────


def test_chat_stream_prints_tokens_as_they_arrive(wired):
    """The fake upstream splits "hello" across two native-Ollama stream lines ("hel" + "lo") —
    the service reassembles them into OpenAI-shaped SSE deltas, and the CLI must reassemble those
    back into one printed word, proving the whole round trip, not just one hop of it."""
    res = runner.invoke(gateway_cmd.app, ["chat", "chat", "--message", "hi", "--stream"])
    assert res.exit_code == 0
    assert res.output == "hello\n"  # exactly the streamed text plus one trailing newline


def test_chat_stream_never_prints_the_key_it_was_given(wired):
    res = runner.invoke(
        gateway_cmd.app,
        ["chat", "chat", "--message", "hi", "--stream", "--key", "vk-super-secret-value"],
    )
    assert res.exit_code == 0
    assert "vk-super-secret-value" not in res.output


def test_chat_stream_of_an_unknown_model_is_a_clean_error_not_a_traceback(wired):
    res = runner.invoke(gateway_cmd.app, ["chat", "does-not-exist", "--message", "hi", "--stream"])
    assert res.exit_code == 1
    assert "traceback" not in res.output.lower()
    assert "model_not_found" in res.output.lower() or "not found" in res.output.lower()


def test_chat_stream_reports_a_forbidden_configured_url_without_any_network_call(monkeypatch):
    def must_not_be_called(**kw):
        raise AssertionError("a request was attempted despite the forbidden URL")

    monkeypatch.setattr(gateway_cmd, "_sync_client", must_not_be_called)
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "https://acme.openai.azure.com/v1")
    res = runner.invoke(gateway_cmd.app, ["chat", "chat", "--message", "hi", "--stream"])
    assert res.exit_code == 1
    assert "denied" in res.output.lower() and "azure endpoints are forbidden" in res.output.lower()


def test_chat_stream_unreachable_service_is_a_clean_error(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://127.0.0.1:1")
    res = runner.invoke(gateway_cmd.app, ["chat", "chat", "--message", "hi", "--stream"])
    assert res.exit_code == 1
    assert "unreachable" in res.output.lower()
    assert "traceback" not in res.output.lower()


def test_chat_stream_a_mid_stream_error_is_surfaced_with_the_partial_content_kept(monkeypatch):
    """The counterpart to `..._of_an_unknown_model_...` above: that case fails *before* the SSE
    stream ever starts (a pre-connection 4xx). This one fails *inside* an already-200,-already-
    streaming response — the only path that reaches `_chat_stream`'s own `if "error" in data:`
    branch, which nothing else in this file exercises. ADR 0153 d9: once the first byte is out, the
    stream is never re-routed — the already-printed partial content must stay on screen, not be
    discarded, when the terminal error event arrives."""

    class FlakyUpstream(Upstream):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/chat":
                body = json.loads(request.content)
                if body.get("stream") and body["model"] == "flaky":
                    lines = [
                        json.dumps(
                            {
                                "model": "flaky",
                                "message": {"role": "assistant", "content": "partial"},
                            }
                        ),
                        json.dumps({"error": "upstream died mid-stream"}),
                    ]
                    return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
            return super().__call__(request)

    cfg = json.loads(json.dumps(GOOD_CFG))
    cfg["models"]["flaky"] = {"deployments": [{"provider": "n1", "model": "flaky"}]}

    def factory(name, pcfg):
        return OllamaProvider(
            name,
            pcfg.base_url,
            locality=pcfg.locality,
            transport=httpx.MockTransport(FlakyUpstream()),
        )

    app = create_app(provider_factory=factory, config=cfg, auth="off", admin_token=ADMIN)
    monkeypatch.setattr(gateway_cmd, "_sync_client", lambda **kw: FakeClient(app))
    monkeypatch.setenv("EXAMLOPS_LLM_GATEWAY_URL", "http://gw")

    res = runner.invoke(gateway_cmd.app, ["chat", "flaky", "--message", "hi", "--stream"])
    assert res.exit_code == 1
    assert "partial" in res.output  # the content that streamed before the error is not discarded
    assert "died mid-stream" in res.output.lower() or "stream_interrupted" in res.output.lower()


def test_chat_without_stream_still_uses_the_in_process_client_not_the_service(monkeypatch):
    """`--stream` must be strictly additive — omitting it keeps the exact prior behaviour (the
    in-process `GatewayClient`, echoing when no route matches), never silently switching every
    `chat` call onto the network path just because the service commands now exist alongside it."""
    monkeypatch.delenv("EXAMLOPS_LLM_GATEWAY_URL", raising=False)

    def must_not_be_called(**kw):
        raise AssertionError("chat without --stream must never touch _sync_client")

    monkeypatch.setattr(gateway_cmd, "_sync_client", must_not_be_called)
    res = runner.invoke(gateway_cmd.app, ["chat", "default", "--message", "hi"])
    assert res.exit_code == 0
    assert "echo" in res.output.lower()
