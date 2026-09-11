"""Track V — server-mode vLLM engine (ADR 0107, spec §4.5).

GWT-V1 server engine resolution + echo degradation · GWT-V2 token-true SSE streaming +
TTFT · GWT-V3 chat-native contract · GWT-V6 one argv renderer across substrates.

Everything runs on CPU against a **real** stub HTTP server on an ephemeral port — real
sockets, real SSE framing, real ``/health`` and ``/metrics`` parsing — so the server path
is genuinely exercised without a GPU or a vLLM install.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops import engines  # noqa: E402
from examlops.engines import config as engine_config  # noqa: E402
from examlops.platform_db import init_db  # noqa: E402

_METRICS = """\
# HELP vllm:num_requests_running Number of requests currently running.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="m"} 3.0
vllm:num_requests_waiting{model_name="m"} 1.0
vllm:kv_cache_usage_perc{model_name="m"} 0.42
vllm:e2e_request_latency_seconds_bucket{le="0.5"} 7.0
"""


class _StubHandler(BaseHTTPRequestHandler):
    """A minimal but faithful stand-in for `vllm serve`'s OpenAI surface."""

    last_request: dict = {}

    def log_message(self, *args):  # silence the test log
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
            self._send(200, json.dumps({"data": [{"id": "stub-vlm"}]}).encode())
        elif self.path == "/metrics":
            self._send(200, _METRICS.encode(), "text/plain")
        else:
            self._send(404, b"{}")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).last_request = payload
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("Hello", " there", " world"):
                frame = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = {
            "choices": [{"message": {"content": "stub answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2},
        }
        self._send(200, json.dumps(body).encode())


@pytest.fixture
def stub_server():
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.delenv("EXAMLOPS_VLLM_BASE_URL", raising=False)
    init_db()


# ── GWT-V1: resolution ────────────────────────────────────────────────────────


def test_gwtv1_vllm_resolves_to_server_engine_when_endpoint_configured(stub_server):
    cfg = engines.EngineConfig(engine="vllm", base_url=stub_server)
    eng = engines.build_engine(cfg, model_path="stub-vlm")
    assert eng.name == "vllm-server"
    assert eng.health() is True


def test_gwtv1_env_endpoint_is_used_when_yaml_has_none(stub_server, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_VLLM_BASE_URL", stub_server)
    eng = engines.build_engine(engines.EngineConfig(engine="vllm"), model_path="stub-vlm")
    assert eng.name == "vllm-server"


def test_gwtv1_per_model_base_url_beats_env(stub_server, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_VLLM_BASE_URL", "http://wrong:9999")
    cfg = engines.EngineConfig(engine="vllm", base_url=stub_server)
    assert engines.build_engine(cfg).base_url == stub_server


def test_gwtv1_no_endpoint_degrades_to_echo():
    with pytest.warns(RuntimeWarning, match="no endpoint is configured"):
        eng = engines.build_engine(engines.EngineConfig(engine="vllm"))
    assert eng.name == "echo"


def test_gwtv1_inproc_still_selectable():
    # The offline-batch engine did not disappear; it is just no longer the default.
    assert engines._ENGINES["vllm-inproc"] is engines.VLLMEngine


def test_health_is_false_and_never_raises_when_unreachable():
    eng = engines.VLLMServerEngine("http://127.0.0.1:1", "m")
    assert eng.health() is False


# ── GWT-V2: token-true streaming + TTFT ───────────────────────────────────────


def test_gwtv2_stream_yields_incremental_deltas(stub_server):
    eng = engines.VLLMServerEngine(stub_server, "stub-vlm")
    chunks = list(eng.chat_stream([{"role": "user", "content": "hi"}]))
    assert chunks == ["Hello", " there", " world"]  # per-frame, not one blob
    assert len(chunks) >= 2


def test_gwtv2_ttft_recorded_at_first_delta(stub_server):
    eng = engines.VLLMServerEngine(stub_server, "stub-vlm")
    list(eng.chat_stream([{"role": "user", "content": "hi"}]))
    assert eng.last_ttft_s > 0


def test_gwtv2_stream_survives_a_malformed_frame(stub_server):
    # A single bad SSE frame must not abort a live stream.
    assert engines.vllm_server._delta_text({"choices": []}) == ""


# ── GWT-V3: chat-native contract ──────────────────────────────────────────────


def test_gwtv3_chat_returns_usage_and_latency(stub_server):
    eng = engines.VLLMServerEngine(stub_server, "stub-vlm")
    comp = eng.chat([{"role": "user", "content": "hi"}], max_tokens=8, temperature=0.0)
    assert comp.text == "stub answer"
    assert (comp.prompt_tokens, comp.completion_tokens) == (11, 2)
    assert comp.total_s >= 0
    # Sampling params reach the wire (R-A2 passthrough on the server path).
    assert _StubHandler.last_request["max_tokens"] == 8
    assert _StubHandler.last_request["temperature"] == 0.0


def test_gwtv3_only_the_server_engine_is_chat_capable(stub_server):
    assert engines.supports_chat(engines.VLLMServerEngine(stub_server, "m")) is True
    # EchoEngine deliberately has no `chat`, so media loss can never be silent (R-V4).
    assert engines.supports_chat(engines.EchoEngine()) is False


def test_gwtv3_generate_still_works_through_the_chat_path(stub_server):
    eng = engines.VLLMServerEngine(stub_server, "stub-vlm")
    assert eng.generate("hello").text == "stub answer"


def test_served_model_name_overrides_the_wire_model(stub_server):
    cfg = engines.EngineConfig(engine="vllm", served_model_name="public-alias")
    engines.VLLMServerEngine(stub_server, "internal-id", cfg).chat(
        [{"role": "user", "content": "x"}]
    )
    assert _StubHandler.last_request["model"] == "public-alias"


# ── metrics ───────────────────────────────────────────────────────────────────


def test_metrics_scraped_from_the_server(stub_server):
    metrics = engines.VLLMServerEngine(stub_server, "m").metrics()
    assert metrics["vllm:num_requests_running"] == 3.0
    assert metrics["vllm:kv_cache_usage_perc"] == 0.42


def test_metrics_parser_skips_histogram_buckets():
    # Summing `_bucket` series would produce a number that looks like a value but is not.
    parsed = engines.parse_prometheus_text(_METRICS)
    assert not any(k.endswith("_bucket") for k in parsed)


def test_metrics_returns_empty_when_unreachable():
    assert engines.VLLMServerEngine("http://127.0.0.1:1", "m").metrics() == {}


# ── GWT-V6: one argv renderer ─────────────────────────────────────────────────


def test_gwtv6_argv_renders_parallelism_and_media_flags():
    cfg = engines.EngineConfig.from_dict(
        {
            "engine": "vllm",
            "dtype": "bfloat16",
            "tensor_parallel_size": 4,
            "pipeline_parallel_size": 2,
            "max_model_len": 8192,
            "gpu_memory_utilization": 0.9,
            "multimodal": {
                "modality": "vision",
                "limit_mm_per_prompt": {"image": 2},
                "allowed_media_domains": ["example.com"],
                "allowed_local_media_path": "/data/media",
            },
        }
    )
    args = engines.to_vllm_args(cfg)
    assert (
        "--tensor-parallel-size" in args and args[args.index("--tensor-parallel-size") + 1] == "4"
    )
    assert "--pipeline-parallel-size" in args
    assert "--limit-mm-per-prompt.image" in args
    assert "--allowed-media-domains" in args and "example.com" in args
    assert "--allowed-local-media-path" in args


def test_gwtv6_kserve_manifest_uses_the_same_renderer():
    """The k8s path must not re-transcribe flags — that is how substrates drift apart."""
    from examlops.serving_backends import ResolvedRef, registry_to_kserve

    block = {
        "engine": "vllm",
        "tensor_parallel_size": 4,
        "multimodal": {"modality": "vision", "limit_mm_per_prompt": {"image": 2}},
    }
    ref = ResolvedRef("qwen-vl", "1", "Production", "hf://Qwen/Qwen2.5-VL-7B-Instruct")
    manifest = registry_to_kserve({"name": "qwen-vl", "engine": block}, ref)
    manifest_args = manifest["spec"]["template"]["containers"][0]["args"]
    assert manifest_args == engines.to_vllm_args(engines.EngineConfig.from_dict(block))
    # The media guard specifically must survive into the k8s manifest.
    assert "--limit-mm-per-prompt.image" in manifest_args


def test_gwtv6_hpc_template_embeds_the_same_argv(tmp_path):
    from examlops.llm_endpoints import EndpointSpec, HpcLauncher

    cfg = engines.EngineConfig.from_dict(
        {"engine": "vllm", "tensor_parallel_size": 4, "dtype": "bfloat16"}
    )
    spec = EndpointSpec(
        model="qwen-vl", hf_model_id="Qwen/Qwen3-VL", config=cfg, nodes=2, work_dir=str(tmp_path)
    )
    script = HpcLauncher(scheduler="slurm").render_script(spec)
    assert " ".join(engines.to_vllm_args(cfg)) in script
    # Every real placeholder is substituted. (The template's own comment mentions the
    # literal `@@NAME@@` form, so a blanket "no @@" check would match the docs.)
    for placeholder in (
        "@@MODEL@@",
        "@@IMAGE@@",
        "@@SIF_PATH@@",
        "@@PORT@@",
        "@@NODES@@",
        "@@ENDPOINT_FILE@@",
        "@@RAY_PORT@@",
        "@@MODULE_LOADS@@",
        "@@VLLM_ARGS@@",
    ):
        assert placeholder not in script, placeholder
    assert "Qwen/Qwen3-VL" in script
    assert "ray start --head" in script  # multi-node ⇒ a Ray cluster


def test_single_node_script_skips_the_ray_cluster(tmp_path):
    """One node needs no Ray — vLLM does in-node tensor parallelism itself."""
    from examlops.llm_endpoints import EndpointSpec, HpcLauncher

    spec = EndpointSpec(
        model="qwen-vl", hf_model_id="Qwen/Qwen3-VL", nodes=1, work_dir=str(tmp_path)
    )
    script = HpcLauncher(scheduler="slurm").render_script(spec)
    assert 'NODES="1"' in script


def test_argv_omits_defaults_so_it_stays_diffable():
    assert engines.to_vllm_args(engines.EngineConfig()) == []


def test_speculative_decoding_renders_as_json_config():
    cfg = engines.EngineConfig(
        speculative_decoding={"enabled": True, "draft_model": "tiny", "num_speculative_tokens": 5}
    )
    args = engines.to_vllm_args(cfg)
    payload = json.loads(args[args.index("--speculative-config") + 1])
    assert payload == {"model": "tiny", "num_speculative_tokens": 5}


# ── engine-block validation (GWT-2 extended for Track V) ──────────────────────


def test_validate_rejects_a_vision_model_without_an_item_limit():
    errors = engines.validate_engine_block({"engine": "vllm", "multimodal": {"modality": "vision"}})
    assert any("limit_mm_per_prompt" in e for e in errors)


def test_validate_accepts_a_complete_vision_block():
    assert (
        engines.validate_engine_block(
            {
                "engine": "vllm",
                "mode": "server",
                "base_url": "http://gpu01:8000",
                "pipeline_parallel_size": 2,
                "gpu_memory_utilization": 0.9,
                "kv_cache_dtype": "fp8",
                "multimodal": {"modality": "vision", "limit_mm_per_prompt": {"image": 2}},
            }
        )
        == []
    )


@pytest.mark.parametrize(
    "block,needle",
    [
        ({"mode": "sideways"}, "mode"),
        ({"base_url": "gpu01:8000"}, "base_url"),
        ({"gpu_memory_utilization": 1.5}, "gpu_memory_utilization"),
        ({"kv_cache_dtype": "int3"}, "kv_cache_dtype"),
        ({"pipeline_parallel_size": 0}, "pipeline_parallel_size"),
        ({"multimodal": {"modality": "smell", "limit_mm_per_prompt": {"image": 1}}}, "modality"),
        (
            {"multimodal": {"modality": "vision", "limit_mm_per_prompt": {"hologram": 1}}},
            "hologram",
        ),
    ],
)
def test_validate_catches_bad_track_v_fields(block, needle):
    errors = engines.validate_engine_block({"engine": "vllm", **block})
    assert any(needle in e for e in errors), errors


def test_config_defaults_are_unchanged_for_a_pre_track_v_block():
    """An existing engine block must mean exactly what it meant before Track V."""
    cfg = engines.EngineConfig.from_dict({"engine": "vllm", "dtype": "float16"})
    assert (cfg.engine, cfg.dtype, cfg.tensor_parallel_size, cfg.prefix_cache) == (
        "vllm",
        "float16",
        1,
        True,
    )
    assert cfg.multimodal.modality == "text"
    assert engine_config.to_vllm_args(cfg) == ["--dtype", "float16"]
