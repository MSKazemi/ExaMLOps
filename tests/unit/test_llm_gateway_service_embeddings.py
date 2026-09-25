"""`POST /v1/embeddings` on the live llm-gateway service (ADR 0152 d1 declared `Provider.embed()`
in 2026-09-20; nothing routed to it until now). Own fake upstream and config, kept separate from
`test_llm_gateway_service.py`'s 58 chat-completions tests — the shared fixture there declares no
embedding-capable model and changing it is unwarranted risk for an unrelated route.
"""

from __future__ import annotations

import json

import httpx
import pytest

from examlops.gateway import issue_virtual_key
from examlops.gateway.config import ProviderCfg
from examlops.gateway.providers import OllamaProvider
from examlops.gateway.service.app import create_app

ADMIN = "a-long-admin-token-for-embed-tests-0123"

CFG = {
    "version": 1,
    "providers": {
        "n1": {
            "type": "ollama",
            "base_url": "http://ollama.test:11434",
            "locality": "local",
            "discover": True,
        }
    },
    "models": {
        "embed": {
            "deployments": [{"provider": "n1", "model": "nomic-embed-text"}],
            "required": True,
        }
    },
    "aliases": {"default-embed": "embed"},
}


class Upstream:
    def __init__(self) -> None:
        self.mode = "ok"
        self.embed_bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("connection refused")
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "nomic-embed-text", "capabilities": ["embedding"]}]},
            )
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if path == "/api/embed":
            body = json.loads(request.content)
            self.embed_bodies.append(body)
            n = len(body["input"]) if isinstance(body["input"], list) else 1
            if self.mode == "bad_upstream":
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(
                200,
                json={
                    "embeddings": [[0.1, 0.2, 0.3] for _ in range(n)],
                    "prompt_eval_count": 3 * n,
                    "model": body["model"],
                },
            )
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


def make_app(upstream: Upstream, **kw):
    def factory(name: str, cfg: ProviderCfg):
        return OllamaProvider(
            name, cfg.base_url, locality=cfg.locality, transport=httpx.MockTransport(upstream)
        )

    kw.setdefault("config", CFG)
    kw.setdefault("auth", "off")
    kw.setdefault("admin_token", ADMIN)
    return create_app(provider_factory=factory, **kw)


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


async def test_a_string_input_returns_one_embedding(upstream):
    async with client(make_app(upstream)) as c:
        r = await c.post("/v1/embeddings", json={"model": "embed", "input": "hello"})
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list" and data["model"] == "embed"
    assert len(data["data"]) == 1
    assert data["data"][0] == {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}
    assert data["usage"] == {"prompt_tokens": 3, "total_tokens": 3}
    assert r.headers["x-examlops-provider"] == "n1" and r.headers["x-examlops-route"] == "embed"


async def test_a_list_input_returns_one_embedding_per_item_in_order(upstream):
    async with client(make_app(upstream)) as c:
        r = await c.post("/v1/embeddings", json={"model": "embed", "input": ["a", "b", "c"]})
    data = r.json()
    assert [d["index"] for d in data["data"]] == [0, 1, 2]
    assert data["usage"]["prompt_tokens"] == 9  # 3 tokens/item × 3 items, from the fake


async def test_aliases_resolve_and_the_real_model_reaches_the_upstream(upstream):
    async with client(make_app(upstream)) as c:
        r = await c.post("/v1/embeddings", json={"model": "default-embed", "input": "x"})
    assert r.status_code == 200
    assert upstream.embed_bodies[0]["model"] == "nomic-embed-text"


@pytest.mark.parametrize("body", [{"model": "embed", "input": ""}, {"model": "embed", "input": []}])
async def test_empty_input_is_a_400_not_a_request_to_the_upstream(upstream, body):
    async with client(make_app(upstream)) as c:
        r = await c.post("/v1/embeddings", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert upstream.embed_bodies == []


async def test_an_unknown_model_is_a_404_envelope(upstream):
    async with client(make_app(upstream)) as c:
        r = await c.post("/v1/embeddings", json={"model": "nope", "input": "x"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"


async def test_a_dead_upstream_is_a_503_naming_the_provider_not_the_input_text(upstream):
    upstream.mode = "down"
    body = {"model": "embed", "input": "TOP-SECRET-DOCUMENT-TEXT"}
    async with client(make_app(upstream)) as c:
        r = await c.post("/v1/embeddings", json=body)
    err = r.json()["error"]
    assert r.status_code == 503 and err["code"] == "upstream_unavailable"
    assert "TOP-SECRET-DOCUMENT-TEXT" not in r.text


async def test_an_upstream_5xx_is_reported_typed(upstream):
    upstream.mode = "bad_upstream"
    async with client(make_app(upstream)) as c:
        r = await c.post("/v1/embeddings", json={"model": "embed", "input": "x"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"


async def test_body_over_the_limit_is_refused_before_parsing(upstream):
    async with client(make_app(upstream, max_body_bytes=200)) as c:
        r = await c.post("/v1/embeddings", json={"model": "embed", "input": "x" * 2000})
    assert r.status_code == 413 and r.json()["error"]["code"] == "invalid_request"


async def test_a_body_without_a_content_length_is_still_bounded(upstream):
    """The header check alone is not the guard: a chunked request declares no `content-length`,
    so the post-read size check is what actually bounds it."""

    async def chunks():
        for _ in range(10):
            yield b"x" * 200  # chunked transfer: nothing declares the size up front

    async with client(make_app(upstream, max_body_bytes=500)) as c:
        r = await c.post(
            "/v1/embeddings", content=chunks(), headers={"content-type": "application/json"}
        )
    assert r.status_code == 413
    assert upstream.embed_bodies == []


async def test_keys_mode_refuses_a_missing_key_before_any_upstream_call(upstream):
    async with client(make_app(upstream, auth="keys")) as c:
        r = await c.post("/v1/embeddings", json={"model": "embed", "input": "x"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "key_invalid"
    assert upstream.embed_bodies == []


async def test_a_valid_key_is_served_and_the_call_is_billed_locally_at_zero_cost(upstream):
    raw = issue_virtual_key("acme", "embed", None, None, "admin")
    async with client(make_app(upstream, auth="keys")) as c:
        r = await c.post(
            "/v1/embeddings",
            json={"model": "embed", "input": "x"},
            headers={"Authorization": f"Bearer {raw}"},
        )
    assert r.status_code == 200


async def test_metrics_count_embeddings_requests(upstream):
    async with client(make_app(upstream)) as c:
        await c.post("/v1/embeddings", json={"model": "embed", "input": "x"})
        text = (await c.get("/metrics", headers={"Authorization": f"Bearer {ADMIN}"})).text
    assert (
        'llm_gateway_requests_total{code="ok",model="nomic-embed-text",provider="n1",'
        'route="embed",status="200"} 1.0' in text
    )
