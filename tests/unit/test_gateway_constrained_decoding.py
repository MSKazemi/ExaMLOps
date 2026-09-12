# tests/unit/test_gateway_constrained_decoding.py
"""ADR 0035 clause 1 — the schema constrains the decoder, not just the verdict (BL-071).

The recorded finding: "clause 1's **constrained decoding** is still absent — no guided decoding, no
grammar or regex constraint, no provider structured-output API is called — so the platform validates
and repairs a response it did not constrain". Repair reaches a valid object by rewriting a wrong
answer; a constraint means the model could not have produced one.

Now a backend that can constrain (an OpenAI-compatible server, in-process vLLM) is *given* the
schema — `response_format: {"type": "json_schema", …, "strict": true}` on the wire — and a backend
that cannot is asked as before. Validation and repair still run either way: a server may ignore the
field, so the constraint is never taken on trust, and which mode ran is recorded so the failure rate
can be read per mode.

These run a real HTTP server that records the request body the gateway sent.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import gateway as gw  # noqa: E402
from examlops.data.events import structured_output_stats  # noqa: E402
from examlops.engines.config import EngineConfig, schema_response_format  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}, "score": {"type": "number"}},
    "required": ["verdict", "score"],
}


class _Server:
    """What the fake OpenAI-compatible server answers, and what it was asked."""

    def __init__(self):
        self.answer = '{"verdict": "ok", "score": 0.9}'
        self.requests: list[dict] = []


@pytest.fixture
def server(monkeypatch):
    state = _Server()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(body)
            payload = {
                "choices": [{"message": {"content": state.answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5},
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    init_db()
    yield state
    httpd.shutdown()


def _client(server):
    backend = gw.engine_backend(
        "test-model", {"engine": "vllm-server", "mode": "server", "base_url": server.base_url}
    )
    router = gw.Router()
    router.add_route("m", [("server", backend)])
    return gw.GatewayClient(router), backend


def _ask(server, **kw):
    client, _ = _client(server)
    return client.chat("m", [{"role": "user", "content": "grade this"}], **kw)


# ── the constraint goes to the server ────────────────────────────────────────


def test_the_schema_is_sent_as_the_constraint(server):
    comp = _ask(server, response_schema=SCHEMA)

    (request,) = server.requests
    assert request["response_format"] == schema_response_format(SCHEMA)
    assert request["response_format"]["json_schema"]["strict"] is True
    assert comp.parsed == {"verdict": "ok", "score": 0.9}


def test_a_request_without_a_schema_carries_no_constraint(server):
    _ask(server)

    assert "response_format" not in server.requests[0]


def test_sampling_still_goes_with_it(server):
    _ask(server, response_schema=SCHEMA, temperature=0.0, max_tokens=64)

    request = server.requests[0]
    assert (request["temperature"], request["max_tokens"]) == (0.0, 64)
    assert "response_schema" not in request, "the schema travels as response_format, once"


# ── a backend that cannot constrain is unchanged ─────────────────────────────


def test_a_backend_that_cannot_constrain_is_not_handed_the_schema():
    """It is asked exactly as before, and the answer is validated afterwards."""
    seen: list[dict] = []

    def plain(model, messages, **kw):
        seen.append(kw)
        return gw.Completion(text='{"verdict": "ok", "score": 1}', model=model, backend="plain")

    router = gw.Router()
    router.add_route("m", [("plain", plain)])
    init_db()

    comp = gw.GatewayClient(router).chat(
        "m", [{"role": "user", "content": "x"}], response_schema=SCHEMA
    )

    assert seen == [{}], "no schema reaches a backend that cannot use it"
    assert comp.parsed == {"verdict": "ok", "score": 1}


def test_the_echo_engine_does_not_claim_to_constrain():
    backend = gw.engine_backend("m", {"engine": "echo"})

    assert backend.constrains_schema is False


def test_the_server_engine_claims_it_through_the_telemetry_wrapper(server):
    """`build_engine` wraps engines for spans; the wrapper must not hide the capability."""
    _, backend = _client(server)

    assert type(backend.engine).__name__ != "VLLMServerEngine", "wrapped, as in production"
    assert backend.constrains_schema is True


# ── a constraint is never taken on trust ─────────────────────────────────────


def test_an_answer_that_does_not_fit_is_still_refused(server):
    """A server may ignore `response_format`; the platform's guarantee does not rest on it."""
    from examlops.structured import StructuredOutputError

    server.answer = '{"verdict": "ok"}'  # `score` missing, and unrepairable

    with pytest.raises(StructuredOutputError):
        _ask(server, response_schema=SCHEMA, max_repairs=0)


def test_prose_around_the_json_is_still_tolerated(server):
    server.answer = 'Sure!\n```json\n{"verdict": "ok", "score": 0.5}\n```'

    assert _ask(server, response_schema=SCHEMA).parsed == {"verdict": "ok", "score": 0.5}


# ── which mode ran is recorded ───────────────────────────────────────────────


def test_a_constrained_decode_is_recorded_as_one(server):
    _ask(server, response_schema=SCHEMA)

    stats = structured_output_stats()
    assert (stats["valid"], stats["constrained"]) == (1, 1)


def test_an_unconstrained_one_is_not(server):
    """So a constrained backend that still needs repairs is visible as exactly that."""

    def plain(model, messages, **kw):
        return gw.Completion(text='{"verdict": "ok", "score": 1}', model=model, backend="plain")

    router = gw.Router()
    router.add_route("m", [("plain", plain)])
    init_db()

    gw.GatewayClient(router).chat("m", [{"role": "user", "content": "x"}], response_schema=SCHEMA)

    stats = structured_output_stats()
    assert (stats["valid"], stats["constrained"]) == (1, 0)


def test_a_datastore_from_before_gains_the_column(tmp_path, monkeypatch):
    from examlops.platform_db import get_db
    from examlops.platform_db import init_db as init

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "old.db"))
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE structured_output_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "model TEXT, tenant TEXT NOT NULL DEFAULT 'default', outcome TEXT NOT NULL, "
            "ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO structured_output_events (outcome) VALUES ('valid')")
    init(force=True)

    assert structured_output_stats() == {"valid": 1, "constrained": 0}


# ── the in-process engine asks vLLM for the same thing ───────────────────────


def test_the_in_process_engine_builds_guided_sampling_params(monkeypatch):
    """vLLM's offline API takes the constraint as `SamplingParams(guided_decoding=…)`. The GPU
    path cannot run here, so the vLLM modules are supplied and the decision is checked."""
    import types

    from examlops.engines import VLLMEngine

    built = {}

    class GuidedDecodingParams:
        def __init__(self, **kw):
            built["guided"] = kw

    def SamplingParams(**kw):  # noqa: N802 - vLLM's own name
        built["sampling"] = kw
        return kw

    vllm = types.ModuleType("vllm")
    vllm.SamplingParams = SamplingParams
    sampling_params = types.ModuleType("vllm.sampling_params")
    sampling_params.GuidedDecodingParams = GuidedDecodingParams
    monkeypatch.setitem(sys.modules, "vllm", vllm)
    monkeypatch.setitem(sys.modules, "vllm.sampling_params", sampling_params)

    VLLMEngine("m", EngineConfig(engine="vllm-inproc"))._sampling_params(
        {"response_schema": SCHEMA, "temperature": 0.0, "unknown": 1}
    )

    assert built["guided"] == {"json": SCHEMA}
    assert built["sampling"]["temperature"] == 0.0
    assert "unknown" not in built["sampling"] and "response_schema" not in built["sampling"]


def test_without_a_schema_the_in_process_engine_asks_for_no_constraint(monkeypatch):
    import types

    from examlops.engines import VLLMEngine

    built = {}
    vllm = types.ModuleType("vllm")
    vllm.SamplingParams = lambda **kw: built.setdefault("sampling", kw)
    monkeypatch.setitem(sys.modules, "vllm", vllm)

    VLLMEngine("m", EngineConfig(engine="vllm-inproc"))._sampling_params({"temperature": 0.2})

    assert "guided_decoding" not in built["sampling"]
