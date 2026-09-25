# tests/unit/test_finetune_adapter_serving.py
"""ADR 0044 clause 3: inference is served *through* an adapter, and only a governed one.

Before, ``MultiLoRARouter.route`` returned ``f"[{adapter}] {prompt}"`` — it selected a registry
row and nothing ran. These tests hold the new behaviour:

* the ``torch`` engine answers with the trained adapter's own output — two adapters on the same
  base give different answers to the same prompt, and each answer equals what the adapter's
  tensors compute when loaded by hand;
* an engine that serves inference refuses an unpromoted adapter (C3), a bundle whose digest is
  not the registered one (D3), a row whose signature no longer matches, and an adapter trained on
  different base weights;
* the ``vllm`` engine drives the E2 server's runtime LoRA API — load on admit, unload on LRU
  eviction, route by ``model=<adapter>`` — verified against a stub HTTP server.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("torch")

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.finetuning import (  # noqa: E402
    BaseMismatchError,
    MultiLoRARouter,
    SignatureMismatchError,
    UnpromotedAdapterError,
    lora,
    promote_adapter,
    register_measured_adapter,
    train_lora,
)
from examlops.finetuning.serving import (  # noqa: E402
    AdapterServingError,
    TorchAdapterEngine,
    VLLMAdapterEngine,
    build_adapter_engine,
    encode_prompt,
    render_lora_args,
)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "q" * 32)
    monkeypatch.delenv("EXAMLOPS_FINETUNE_FAULT", raising=False)
    from examlops import platform_db

    platform_db.init_db()


def _trained(tmp_path, capsys, aid, *, seed=4, lr="0.05", method="lora", promote=True, base="b"):
    rc = train_lora.main(
        ["--run-dir", str(tmp_path / aid), "--steps", "30", "--batch", "32", "--seed", str(seed),
         "--lr", lr, "--method", method]
    )  # fmt: skip
    assert rc == 0
    m = train_lora.parse_metrics(capsys.readouterr().out)
    register_measured_adapter(
        aid, base, method=method, rank=4, dataset_revision="rev", eval_score=m["eval_score"],
        eval_metric=m["eval_metric"], eval_n=m["eval_n"], train_run_id=aid,
        adapter_sha256=m["adapter_sha256"], adapter_uri=m["adapter_bundle"],
    )  # fmt: skip
    if promote:
        promote_adapter(aid)
    return m


def _by_hand(bundle: str, prompt: str) -> list[float]:
    import torch

    from examlops.finetuning import artifacts

    state, config = artifacts.load_bundle(
        bundle, expected_sha256=lora.adapter_sha256(torch.load(Path(bundle) / "adapter_model.pt"))
    )
    model = lora.get_backend(config["backend"]).build(
        seed=config["seed"], rank=config["rank"], alpha=config["alpha"], method=config["method"]
    )
    params = dict(model.named_parameters())
    with torch.no_grad():
        for k, v in state.items():
            params[k].copy_(v)
        model.eval()
        return torch.softmax(model(torch.tensor([encode_prompt(prompt)])), dim=1)[0].tolist()


def _actions(target):
    from examlops.platform_db import get_db

    with get_db() as conn:
        rows = conn.execute("SELECT action FROM audit_events WHERE target=?", (target,)).fetchall()
    return [r["action"] for r in rows]


# ── torch engine ──────────────────────────────────────────────────────────────


def test_requests_are_answered_by_the_adapter_they_name(tmp_path, capsys):
    a = _trained(tmp_path, capsys, "ad-a", lr="0.05")
    b = _trained(tmp_path, capsys, "ad-b", lr="0.2")
    router = MultiLoRARouter("b", engine=TorchAdapterEngine())
    prompt = "alpha beta gamma delta"

    ra = router.route("ad-a", prompt)
    rb = router.route("ad-b", prompt)
    ra2 = router.route("ad-a", prompt)  # served again after b was swapped in

    assert ra["served"] is True and ra["engine"] == "torch"
    assert ra["probabilities"] != rb["probabilities"], "each adapter really changes the output"
    assert ra["probabilities"] == ra2["probabilities"], "swapping adapters leaves no residue"
    assert ra["probabilities"] == pytest.approx(_by_hand(a["adapter_bundle"], prompt), abs=1e-5)
    assert rb["probabilities"] == pytest.approx(_by_hand(b["adapter_bundle"], prompt), abs=1e-5)
    assert sum(ra["probabilities"]) == pytest.approx(1.0, abs=1e-5)
    assert "adapter_loaded" in _actions("ad-a")


def test_an_unpromoted_adapter_is_not_served_unless_overridden(tmp_path, capsys):
    _trained(tmp_path, capsys, "raw", promote=False)

    with pytest.raises(UnpromotedAdapterError, match="not promoted"):
        MultiLoRARouter("b", engine=TorchAdapterEngine()).route("raw", "x y")
    assert "adapter_serve_refused" in _actions("raw")

    out = MultiLoRARouter("b", engine=TorchAdapterEngine(), allow_unpromoted=True).route("raw", "x")
    assert out["served"] is True
    assert "adapter_served_unpromoted" in _actions("raw")


def test_the_registry_engine_still_routes_without_the_gate(tmp_path, capsys):
    _trained(tmp_path, capsys, "dry", promote=False)
    out = MultiLoRARouter("b").route("dry", "hello")
    assert out["served"] is False and out["completion"] == "[dry] hello"


def test_a_swapped_bundle_is_refused(tmp_path, capsys):
    import torch

    m = _trained(tmp_path, capsys, "swap")
    weights = Path(m["adapter_bundle"]) / "adapter_model.pt"
    state = torch.load(weights, weights_only=True)
    state = {k: torch.zeros_like(v) for k, v in state.items()}
    torch.save(state, weights)

    with pytest.raises(AdapterServingError, match="refusing to load"):
        MultiLoRARouter("b", engine=TorchAdapterEngine()).route("swap", "x")


def test_an_adapter_on_different_base_weights_is_refused(tmp_path, capsys):
    _trained(tmp_path, capsys, "seed4", seed=4)
    _trained(tmp_path, capsys, "seed9", seed=9)  # same base *name*, different base *weights*
    router = MultiLoRARouter("b", engine=TorchAdapterEngine())
    router.route("seed4", "x")

    with pytest.raises(BaseMismatchError, match="base weights"):
        router.route("seed9", "x")


def test_a_row_altered_after_signing_is_refused(tmp_path, capsys):
    from examlops.platform_db import get_db

    _trained(tmp_path, capsys, "signed")
    with get_db() as conn:
        conn.execute("UPDATE lora_adapters SET dataset_revision='other' WHERE adapter_id='signed'")

    with pytest.raises(SignatureMismatchError, match="altered"):
        MultiLoRARouter("b", engine=TorchAdapterEngine()).route("signed", "x")


def test_a_full_fine_tune_is_not_served_as_an_adapter(tmp_path, capsys):
    _trained(tmp_path, capsys, "whole", method="full")
    with pytest.raises(AdapterServingError, match="model version"):
        MultiLoRARouter("b", engine=TorchAdapterEngine()).route("whole", "x")


def test_lru_eviction_unloads_from_the_engine(tmp_path, capsys):
    for aid in ("e1", "e2", "e3"):
        _trained(tmp_path, capsys, aid)
    engine = TorchAdapterEngine()
    router = MultiLoRARouter("b", hot_set_size=2, engine=engine)
    for aid in ("e1", "e2", "e3"):
        router.route(aid, "p")

    assert router.loaded == ["e2", "e3"] and router.evictions == 1
    with pytest.raises(AdapterServingError, match="not resident"):
        engine.generate("e1", "p")
    assert router.stats()["misses"] == 3


# ── vLLM engine (E2) against a stub server ────────────────────────────────────


class _Stub(BaseHTTPRequestHandler):
    calls: list = []
    fail_load = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append((self.path, body))
        if self.path == "/v1/load_lora_adapter" and type(self).fail_load:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"LoRA not enabled")
            return
        payload: dict = {}
        if self.path == "/v1/chat/completions":
            payload = {
                "choices": [{"message": {"content": f"via {body['model']}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stub():
    _Stub.calls = []
    _Stub.fail_load = False
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _paper_adapter(aid, *, uri="/models/adapters/x", promote=True):
    from examlops.finetuning import finetune

    finetune("llama", "lora", "rev", adapter_id=aid, adapter_uri=uri)
    if promote:
        promote_adapter(aid)


def test_vllm_loads_routes_and_unloads_through_the_runtime_lora_api(stub):
    for aid in ("v1", "v2"):
        _paper_adapter(aid, uri=f"/srv/{aid}")
    router = MultiLoRARouter("llama", hot_set_size=1, engine=VLLMAdapterEngine(stub))

    out = router.route("v1", "hello")
    router.route("v2", "hello")

    assert out["completion"] == "via v1" and out["served"] is True
    paths = [(p, b.get("lora_name")) for p, b in _Stub.calls]
    assert paths == [
        ("/v1/load_lora_adapter", "v1"),
        ("/v1/chat/completions", None),
        ("/v1/load_lora_adapter", "v2"),
        ("/v1/unload_lora_adapter", "v1"),  # LRU eviction reaches the engine
        ("/v1/chat/completions", None),
    ]
    assert _Stub.calls[0][1]["lora_path"] == "/srv/v1"
    assert _Stub.calls[1][1]["model"] == "v1", "the adapter is selected per request by model name"


def test_vllm_refusal_to_load_is_a_serving_error(stub):
    _Stub.fail_load = True
    _paper_adapter("bad")
    with pytest.raises(AdapterServingError, match="HTTP 400"):
        MultiLoRARouter("llama", engine=VLLMAdapterEngine(stub)).route("bad", "x")


def test_vllm_needs_a_path_the_server_can_read(stub):
    _paper_adapter("nouri", uri=None)
    with pytest.raises(AdapterServingError, match="adapter_uri"):
        MultiLoRARouter("llama", engine=VLLMAdapterEngine(stub)).route("nouri", "x")
    assert _Stub.calls == []


def test_engine_factory_and_lora_flags(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_VLLM_BASE_URL", raising=False)
    with pytest.raises(AdapterServingError, match="base-url"):
        build_adapter_engine("vllm")
    with pytest.raises(ValueError):
        build_adapter_engine("tgi")
    assert build_adapter_engine("torch").serves_inference is True
    assert render_lora_args(max_loras=8, max_lora_rank=32) == [
        "--enable-lora", "--max-loras", "8", "--max-lora-rank", "32",
    ]  # fmt: skip
    with pytest.raises(ValueError):
        render_lora_args(max_loras=0)


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_the_cli_routes_through_a_trained_adapter(tmp_path, capsys):
    from typer.testing import CliRunner

    from examlops.cli.main import app

    _trained(tmp_path, capsys, "cli-ad")
    result = CliRunner().invoke(
        app,
        ["--json", "serve", "adapter", "route", "b", "cli-ad", "--engine", "torch",
         "--prompt", "one two"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["served"] is True and payload["label"] in (0, 1)

    refused = CliRunner().invoke(
        app, ["serve", "adapter", "route", "b", "cli-ad", "--engine", "vllm"]
    )
    assert refused.exit_code == 1
