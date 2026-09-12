# tests/unit/test_gateway_cache_isolation.py
"""ADR 0018 clauses 2 and 3 — the semantic cache keys on what was actually asked (BL-072).

Found reading the cache against its ADR. Clause 2 says the key "includes model + normalized params
+ tenant namespace" and clause 3 says a request bypasses the cache above a temperature threshold.
`SemanticCache` implements both — and the **gateway binding**, the only place the ADR puts the cache
("at the gateway (B2) layer so all LLM callers benefit transparently"), defeated both: it passed a
fixed `{"temperature": 0.0}`, and never called `is_cacheable`.

So one namespace served every request: a caller pinning a `seed` for reproducibility, capping
`max_tokens`, or asking for a JSON schema could be handed an entry stored under different ones, and
a caller asking for variety at temperature 1.2 got the same cached answer every time.

The gateway now passes each request's own params to the hooks, which is what these hold.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import gateway as gw  # noqa: E402
from examlops import semantic_cache as sc  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    init_db()


@pytest.fixture
def calls():
    return []


@pytest.fixture
def client(calls):
    """A gateway with a real semantic cache bound to it, and a counting backend."""

    def backend(model, messages, **kw):
        calls.append(kw)
        return gw.Completion(text=f'{{"a": "answer-{len(calls)}"}}', model=model, backend="b")

    router = gw.Router()
    router.add_route("m", [("b", backend)])
    lookup, store = sc.bind_to_gateway(sc.SemanticCache(), tenant="acme")
    return gw.GatewayClient(router, cache_lookup=lookup, cache_store=store)


def _ask(client, text="what is the capital of France?", **kw):
    return client.chat("m", [{"role": "user", "content": text}], **kw)


# ── the same question, asked the same way, is cached ─────────────────────────


def test_an_identical_request_is_served_from_cache(client, calls):
    first = _ask(client)
    second = _ask(client)

    assert len(calls) == 1, "the second request never reached the backend"
    assert second.cached is True and second.text == first.text


# ── asked differently, it is a different question ────────────────────────────


@pytest.mark.parametrize(
    "first,second",
    [
        ({"temperature": 0.0}, {"temperature": 0.3}),
        ({"max_tokens": 16}, {"max_tokens": 2000}),
        ({"seed": 1}, {"seed": 2}),
        ({"top_p": 0.1}, {"top_p": 0.9}),
        ({}, {"seed": 7}),
        ({}, {"response_schema": SCHEMA}),
    ],
    ids=["temperature", "max_tokens", "seed", "top_p", "no-seed-vs-seed", "schema"],
)
def test_different_params_do_not_share_an_entry(client, calls, first, second):
    _ask(client, **first)
    reply = _ask(client, **second)

    assert len(calls) == 2, "the second request asked for something else and must reach the model"
    assert reply.cached is False


def test_the_same_schema_still_hits(client, calls):
    _ask(client, response_schema=SCHEMA)
    reply = _ask(client, response_schema=SCHEMA)

    assert len(calls) == 1
    assert reply.cached is True and reply.parsed == {"a": "answer-1"}


def test_a_schema_key_does_not_depend_on_key_order():
    """Two spellings of one schema are one namespace, not two."""
    a = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    b = {"required": ["x"], "properties": {"x": {"type": "string"}}, "type": "object"}

    assert sc.namespace("m", {"response_schema": a}, "t") == sc.namespace(
        "m", {"response_schema": b}, "t"
    )


# ── clause 3: a request that wants variety is not answered from cache ────────


def test_a_hot_request_bypasses_the_cache(client, calls):
    """Above the bypass temperature the caller is asking for variety; a cache would remove it."""
    _ask(client, temperature=1.2)
    _ask(client, temperature=1.2)

    assert len(calls) == 2, "neither answered from cache"


def test_a_hot_request_is_not_stored_either(client, calls):
    _ask(client, temperature=1.2)
    reply = _ask(client, temperature=0.0)

    assert len(calls) == 2 and reply.cached is False


def test_no_cache_skips_it_in_both_directions(client, calls):
    _ask(client)  # stored
    reply = _ask(client, no_cache=True)
    _ask(client, no_cache=True)

    assert reply.cached is False
    assert len(calls) == 3, "not answered from the cache, and not stored by it"


def test_no_cache_never_reaches_a_backend(client, calls):
    _ask(client, no_cache=True, temperature=0.0)

    assert calls == [{"temperature": 0.0}]


# ── the hook contract ────────────────────────────────────────────────────────


def test_the_hooks_receive_the_request_params():
    seen: list[dict] = []

    def lookup(model, messages, params=None):
        seen.append(params)
        return None

    def store(model, messages, completion, params=None):
        seen.append(params)

    router = gw.Router()
    router.add_route("m", [("b", lambda model, messages, **kw: "hi")])
    client = gw.GatewayClient(router, cache_lookup=lookup, cache_store=store)

    client.chat("m", [{"role": "user", "content": "q"}], temperature=0.25, seed=3)

    assert seen == [{"temperature": 0.25, "seed": 3}] * 2


def test_an_older_two_argument_hook_still_works_and_says_why_it_is_unsafe():
    """It keeps working — but it can only key on the prompt, which is the bug this fixes."""
    router = gw.Router()
    router.add_route("m", [("b", lambda model, messages, **kw: "hi")])

    def old_lookup(model, messages):
        return "cached"

    gw._hook_takes_params.cache_clear()
    client = gw.GatewayClient(router, cache_lookup=old_lookup)

    with pytest.warns(UserWarning, match="takes no `params`"):
        reply = client.chat("m", [{"role": "user", "content": "q"}])

    assert reply.text == "cached"


def test_the_warning_is_said_once_per_hook():
    router = gw.Router()
    router.add_route("m", [("b", lambda model, messages, **kw: "hi")])

    def old_lookup(model, messages):
        return None

    gw._hook_takes_params.cache_clear()
    client = gw.GatewayClient(router, cache_lookup=old_lookup)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(3):
            client.chat("m", [{"role": "user", "content": "q"}])

    assert len([w for w in caught if "takes no `params`" in str(w.message)]) == 1


def test_a_hook_that_takes_kwargs_is_given_the_params():
    seen: list[dict] = []
    router = gw.Router()
    router.add_route("m", [("b", lambda model, messages, **kw: "hi")])

    def flexible(model, messages, **kw):
        seen.append(kw)
        return None

    gw._hook_takes_params.cache_clear()
    gw.GatewayClient(router, cache_lookup=flexible).chat(
        "m", [{"role": "user", "content": "q"}], temperature=0.1
    )

    assert seen == [{"params": {"temperature": 0.1}}]


# ── tenants stay apart, as they already did ──────────────────────────────────


def test_two_tenants_do_not_share_an_entry(calls):
    cache = sc.SemanticCache()
    router = gw.Router()
    router.add_route("m", [("b", lambda model, messages, **kw: calls.append(kw) or "hi")])

    for tenant in ("acme", "globex"):
        lookup, store = sc.bind_to_gateway(cache, tenant=tenant)
        gw.GatewayClient(router, cache_lookup=lookup, cache_store=store, tenant=tenant).chat(
            "m", [{"role": "user", "content": "q"}]
        )

    assert len(calls) == 2
