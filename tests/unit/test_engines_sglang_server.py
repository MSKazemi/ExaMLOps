"""ADR 0016 decision 1 — SGLang as a served engine (``SGLangServerEngine`` + ``to_sglang_args``).

Runs against a **real** stub HTTP server on an ephemeral port that speaks the surface
``sglang.launch_server`` exposes — OpenAI-compatible chat/completions with SSE streaming,
``/health``, ``/v1/models`` and an ``sglang:``-prefixed ``/metrics`` — so the client path is
exercised end to end without a GPU or an ``sglang`` install.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import engines  # noqa: E402
from examlops.cli.main import app  # noqa: E402
from examlops.data.serving import upsert_llm_endpoint  # noqa: E402
from examlops.engines import sglang_server as sg  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402

runner = CliRunner()

_METRICS = """\
# HELP sglang:num_running_reqs The number of running requests.
# TYPE sglang:num_running_reqs gauge
sglang:num_running_reqs{model_name="stub"} 2.0
sglang:num_queue_reqs{model_name="stub"} 5.0
sglang:token_usage{model_name="stub"} 0.25
sglang:e2e_request_latency_seconds_bucket{le="0.5"} 9.0
vllm:num_requests_running{model_name="other"} 99.0
"""


class _SGLangStub(BaseHTTPRequestHandler):
    last_request: dict = {}
    last_auth: str | None = None

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, b"")
        elif self.path == "/v1/models":
            self._send(200, json.dumps({"data": [{"id": "stub-sglang"}]}).encode())
        elif self.path == "/metrics":
            self._send(200, _METRICS.encode(), "text/plain")
        else:
            self._send(404, b"{}")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).last_request = payload
        type(self).last_auth = self.headers.get("Authorization")
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("Radix", " attention"):
                frame = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        body = {
            "choices": [{"message": {"content": "from sglang"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        }
        self._send(200, json.dumps(body).encode())


@pytest.fixture
def stub():
    server = HTTPServer(("127.0.0.1", 0), _SGLangStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    for var in (
        "EXAMLOPS_SGLANG_BASE_URL",
        "EXAMLOPS_VLLM_BASE_URL",
        "EXAMLOPS_SGLANG_API_KEY",
        "EXAMLOPS_VLLM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    _SGLangStub.last_request = {}
    _SGLangStub.last_auth = None
    init_db()


# ── resolution ────────────────────────────────────────────────────────────────


def test_sglang_with_base_url_builds_the_real_server_client(stub):
    eng = engines.build_engine(engines.EngineConfig(engine="sglang", base_url=stub), "m")
    assert eng.name == "sglang"
    assert isinstance(eng.engine, engines.SGLangServerEngine)
    assert eng.health() is True
    assert eng.models() == ["stub-sglang"]


def test_sglang_resolves_from_its_own_env_var(stub, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SGLANG_BASE_URL", stub)
    eng = engines.build_engine(engines.EngineConfig(engine="sglang"), "m", allow_fallback=False)
    assert eng.name == "sglang"
    assert eng.base_url == stub


def test_the_vllm_env_var_never_routes_an_sglang_model(stub, monkeypatch):
    """One dev box may run both servers: a model must not be served by the wrong runtime."""
    monkeypatch.setenv("EXAMLOPS_VLLM_BASE_URL", stub)
    with pytest.raises(RuntimeError, match="no endpoint is configured"):
        engines.build_engine(engines.EngineConfig(engine="sglang"), "m", allow_fallback=False)


def test_per_model_base_url_beats_the_env_var(stub, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SGLANG_BASE_URL", "http://127.0.0.1:1")
    eng = engines.build_engine(engines.EngineConfig(engine="sglang", base_url=stub), "m")
    assert eng.base_url == stub


def test_sglang_is_listed_as_a_real_engine():
    assert engines._ENGINES["sglang"] is engines.SGLangServerEngine


# ── the wire ──────────────────────────────────────────────────────────────────


def test_generate_chat_and_stream_round_trip(stub):
    eng = engines.build_engine(
        engines.EngineConfig(engine="sglang", base_url=stub, served_model_name="qwen"), "m"
    )
    comp = eng.generate("hi", max_tokens=4, temperature=0.1)
    assert comp.text == "from sglang"
    assert comp.prompt_tokens == 7 and comp.completion_tokens == 2
    assert _SGLangStub.last_request["model"] == "qwen"
    assert _SGLangStub.last_request["max_tokens"] == 4
    assert engines.supports_chat(eng)  # the wrapper keeps the chat-native surface
    assert eng.chat([{"role": "user", "content": "x"}]).text == "from sglang"
    assert "".join(eng.stream("hi")) == "Radix attention"


def test_schema_constraint_is_sent_as_response_format(stub):
    eng = sg.SGLangServerEngine(stub, "m")
    eng.generate("hi", response_schema={"type": "object"})
    rf = _SGLangStub.last_request["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True


def test_api_key_comes_from_the_sglang_env_var(stub, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SGLANG_API_KEY", "sg-secret")
    monkeypatch.setenv("EXAMLOPS_VLLM_API_KEY", "vllm-secret")
    sg.SGLangServerEngine(stub, "m").generate("hi")
    assert _SGLangStub.last_auth == "Bearer sg-secret"


def test_metrics_keep_only_sglang_series_and_skip_buckets(stub):
    metrics = sg.SGLangServerEngine(stub, "m").metrics()
    assert metrics == {
        "sglang:num_running_reqs": 2.0,
        "sglang:num_queue_reqs": 5.0,
        "sglang:token_usage": 0.25,
    }


def test_unreachable_server_is_a_typed_error_and_health_false():
    eng = sg.SGLangServerEngine("http://127.0.0.1:1", "m", timeout=0.5)
    assert eng.health() is False
    with pytest.raises(engines.EngineUnreachable):
        eng.generate("hi")


# ── launch argv ───────────────────────────────────────────────────────────────


def test_to_sglang_args_maps_the_engine_block():
    cfg = engines.EngineConfig.from_dict(
        {
            "engine": "sglang",
            "hf_model_id": "Qwen/Qwen3-8B",
            "served_model_name": "qwen",
            "dtype": "bfloat16",
            "quantization": "awq",
            "max_model_len": 8192,
            "tensor_parallel_size": 4,
            "data_parallel_size": 2,
            "gpu_memory_utilization": 0.85,
            "max_num_seqs": 64,
            "kv_cache_dtype": "fp8",
            "prefix_cache": False,
            "trust_remote_code": True,
            "api_key_secret_ref": "sglang-key",
            "speculative_decoding": {
                "enabled": True,
                "draft_model": "tiny-draft",
                "num_speculative_tokens": 4,
            },
        }
    )
    args = sg.to_sglang_args(cfg, host="0.0.0.0", port=30000)
    pairs = dict(zip(args[::1], args[1::1], strict=False))
    assert pairs["--model-path"] == "Qwen/Qwen3-8B"
    assert pairs["--served-model-name"] == "qwen"
    assert pairs["--dtype"] == "bfloat16"
    assert pairs["--quantization"] == "awq"
    assert pairs["--context-length"] == "8192"
    assert pairs["--tp-size"] == "4"
    assert pairs["--dp-size"] == "2"
    assert pairs["--mem-fraction-static"] == "0.85"
    assert pairs["--max-running-requests"] == "64"
    assert pairs["--kv-cache-dtype"] == "fp8_e4m3"
    assert pairs["--speculative-algorithm"] == "EAGLE"
    assert pairs["--speculative-draft-model-path"] == "tiny-draft"
    # γ=4 is SGLang's step count on a top-k 1 chain; its verify width is steps + 1.
    assert pairs["--speculative-num-steps"] == "4"
    assert pairs["--speculative-eagle-topk"] == "1"
    assert pairs["--speculative-num-draft-tokens"] == "5"
    assert "--disable-radix-cache" in args and "--trust-remote-code" in args
    assert "--enable-metrics" in args
    assert pairs["--port"] == "30000"
    # The secret *name* is resolved by the launcher into env; it never reaches argv.
    assert not any("sglang-key" in a for a in args)
    assert sg.to_sglang_args(cfg) == sg.to_sglang_args(cfg)  # deterministic


def _sglang_accepts_spec_args(args: list[str]) -> tuple[bool, str]:
    """SGLang's own server-start rules for the EAGLE family (arg_groups/speculative_hook.py,
    ``_handle_eagle_family``): with ``--speculative-num-steps`` unset it *asserts* that top-k and
    draft tokens are unset too, and on a top-k 1 chain it requires draft = steps + 1."""
    pairs = dict(zip(args, args[1:], strict=False))
    algo = pairs.get("--speculative-algorithm")
    if algo is None or algo == "NGRAM":
        return True, "n/a"
    steps = pairs.get("--speculative-num-steps")
    topk = pairs.get("--speculative-eagle-topk")
    draft = pairs.get("--speculative-num-draft-tokens")
    if steps is None:
        return (topk is None and draft is None), "draft/topk set without steps"
    if topk == "1" and draft is not None and int(draft) != int(steps) + 1:
        return False, "draft != steps + 1 on a top-k 1 chain"
    return True, "ok"


@pytest.mark.parametrize("method", [None, "EAGLE", "EAGLE3", "NEXTN", "STANDALONE"])
@pytest.mark.parametrize("gamma", [None, 1, 3, 7])
def test_spec_decode_argv_starts_an_sglang_server(method, gamma):
    """The rendered argv must survive SGLang's own validation, not merely name real flags."""
    spec: dict = {"enabled": True, "draft_model": "d"}
    if method:
        spec["method"] = method
    if gamma:
        spec["num_speculative_tokens"] = gamma
    cfg = engines.EngineConfig.from_dict({"engine": "sglang", "speculative_decoding": spec})
    ok, why = _sglang_accepts_spec_args(sg.to_sglang_args(cfg))
    assert ok, why


def test_defaults_render_minimal_argv():
    assert sg.to_sglang_args(engines.EngineConfig(engine="sglang")) == ["--enable-metrics"]


def test_fp8_dtype_becomes_a_quantization_method():
    args = sg.to_sglang_args(engines.EngineConfig(engine="sglang", dtype="fp8"))
    assert args[:2] == ["--quantization", "fp8"] and "--dtype" not in args


@pytest.mark.parametrize(
    "block,match",
    [
        ({"swap_space_gb": 4}, "swap_space_gb"),
        (
            {"multimodal": {"modality": "vision", "limit_mm_per_prompt": {"image": 2}}},
            "multimodal",
        ),
        (
            {"speculative_decoding": {"enabled": True, "draft_model": "d", "method": "medusa"}},
            "method",
        ),
    ],
)
def test_fields_sglang_cannot_honour_are_refused_not_dropped(block, match):
    cfg = engines.EngineConfig.from_dict({"engine": "sglang", **block})
    with pytest.raises(ValueError, match=match):
        sg.to_sglang_args(cfg)


def test_launch_command_is_shell_quoted():
    cmd = sg.render_launch_command(
        engines.EngineConfig(engine="sglang"), model_path="/models/my model"
    )
    assert cmd.startswith("python -m sglang.launch_server --model-path '/models/my model'")


# ── CLI wiring ────────────────────────────────────────────────────────────────


def test_serve_llm_args_renders_the_sglang_launch_argv():
    upsert_llm_endpoint(
        "qwen-sg",
        hf_model_id="Qwen/Qwen3-8B",
        engine="sglang",
        engine_config={"engine": "sglang", "tensor_parallel_size": 2},
    )
    result = runner.invoke(app, ["--json", "serve", "llm", "args", "qwen-sg"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["engine"] == "sglang"
    assert data["args"][:2] == ["--model-path", "Qwen/Qwen3-8B"]
    assert "--tp-size" in data["args"] and "--tensor-parallel-size" not in data["args"]


def test_serve_llm_args_refuses_an_unrenderable_sglang_block():
    upsert_llm_endpoint(
        "bad-sg",
        hf_model_id="x",
        engine="sglang",
        engine_config={"engine": "sglang", "swap_space_gb": 4},
    )
    result = runner.invoke(app, ["serve", "llm", "args", "bad-sg"])
    assert result.exit_code == 1
    assert "swap_space_gb" in result.output


def test_serve_llm_status_scrapes_the_sglang_server(stub):
    upsert_llm_endpoint(
        "qwen-sg",
        hf_model_id="stub",
        engine="sglang",
        base_url=stub,
        state="READY",
        engine_config={"engine": "sglang"},
    )
    result = runner.invoke(app, ["--json", "serve", "llm", "status", "qwen-sg"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["metrics"]["sglang:num_running_reqs"] == 2.0
    assert "vllm:num_requests_running" not in data["metrics"]  # a vLLM series is not ours
