"""ADR 0016 decision 1 — ``SGLangServerEngine``: a client of a running SGLang server.

SGLang is integrated the same way vLLM is (ADR 0107): **as a server, not a library**. The
``sglang.launch_server`` process owns the GPU, does continuous batching and RadixAttention prefix
caching across every concurrent client, and speaks the OpenAI-compatible HTTP API
(``/v1/chat/completions``, ``/v1/models``) plus ``/health`` and — with ``--enable-metrics`` — a
Prometheus ``/metrics`` endpoint whose series are prefixed ``sglang:``.

Because the wire protocol is the one :class:`~examlops.engines.vllm_server.VLLMServerEngine`
already speaks, this engine *is* that client with three differences, each deliberate:

* its ``name`` is ``sglang`` — telemetry, audit and the gateway report which runtime answered;
* its metrics scrape keeps ``sglang:*`` series rather than ``vllm:*``;
* its endpoint resolves from ``engine.base_url`` or ``EXAMLOPS_SGLANG_BASE_URL`` — never from the
  vLLM variable, so one dev box can point at a vLLM server and an SGLang server at once without a
  model silently being served by the wrong runtime.

The **launch** half is :func:`to_sglang_args`, the single renderer of the
``python -m sglang.launch_server`` argv from a per-model ``engine:`` block (the SGLang twin of
:func:`~examlops.engines.config.to_vllm_args`). Pure stdlib; nothing here needs a GPU or the
``sglang`` package, so the whole client is exercised in CI against a stub HTTP server.
"""

from __future__ import annotations

import os
from typing import Any

from examlops.engines.config import EngineConfig
from examlops.engines.vllm_server import VLLMServerEngine

__all__ = [
    "SGLANG_BASE_URL_ENV",
    "SGLANG_SPEC_ALGORITHMS",
    "SGLangServerEngine",
    "parse_sglang_metrics",
    "render_launch_command",
    "resolve_sglang_base_url",
    "sglang_unsupported",
    "to_sglang_args",
]

#: The process-wide default endpoint for ``engine: sglang`` models. Separate from
#: ``EXAMLOPS_VLLM_BASE_URL`` on purpose — see the module docstring.
SGLANG_BASE_URL_ENV = "EXAMLOPS_SGLANG_BASE_URL"

#: ``--speculative-algorithm`` values this renderer emits. ``EAGLE`` is SGLang's default and the
#: mode a ``draft_model`` block maps to when it names no method.
SGLANG_SPEC_ALGORITHMS = ("EAGLE", "EAGLE3", "NEXTN", "STANDALONE", "NGRAM")


def resolve_sglang_base_url(config: EngineConfig) -> str | None:
    """The per-model ``engine.base_url`` beats ``EXAMLOPS_SGLANG_BASE_URL``; neither ⇒ ``None``."""
    return config.base_url or os.getenv(SGLANG_BASE_URL_ENV) or None


class SGLangServerEngine(VLLMServerEngine):
    """OpenAI-compatible client of a running ``sglang.launch_server`` process."""

    name = "sglang"

    #: SGLang compiles ``response_format: json_schema`` into a grammar (xgrammar by default),
    #: so, like vLLM, it constrains the decoder rather than only being checked afterwards.
    constrains_schema = True

    def __init__(
        self,
        base_url: str,
        model: str,
        config: EngineConfig | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(base_url, model, config or EngineConfig(engine="sglang"), **kw)

    def _token(self) -> str:
        """As the vLLM client, but the env fallback is ``EXAMLOPS_SGLANG_API_KEY``."""
        if self._api_key is not None:
            return self._api_key
        if self.config.api_key_secret_ref:
            try:
                from examlops import secrets as _secrets

                self._api_key = _secrets.get_secret(self.config.api_key_secret_ref)
                return self._api_key
            except Exception:
                pass
        self._api_key = os.getenv("EXAMLOPS_SGLANG_API_KEY", "")
        return self._api_key

    def metrics(self) -> dict[str, float]:
        """Scrape the server's ``sglang:*`` series (needs ``--enable-metrics`` on the server)."""
        try:
            with self._request("/metrics") as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception:
            return {}
        return parse_sglang_metrics(text)


def parse_sglang_metrics(text: str) -> dict[str, float]:
    """Sum each ``sglang:*`` sample per metric name, skipping histogram buckets."""
    out: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith("sglang:"):
            continue
        name, _, rest = line.partition("{")
        if rest:
            _, _, value_part = rest.partition("}")
        else:
            name, _, value_part = line.partition(" ")
        name = name.strip()
        if name.endswith("_bucket"):
            continue
        try:
            value = float(value_part.strip().split()[0])
        except (ValueError, IndexError):
            continue
        out[name] = out.get(name, 0.0) + value
    return out


def to_sglang_args(
    config: EngineConfig,
    *,
    model_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    enable_metrics: bool = True,
) -> list[str]:
    """Render the ``python -m sglang.launch_server`` argv for ``config``.

    The SGLang twin of :func:`~examlops.engines.config.to_vllm_args`, with the same two rules:
    **secrets are never rendered** (``api_key_secret_ref`` becomes an environment variable in the
    launcher, never a flag ``ps`` can read) and the output is deterministic, so two renders of one
    block diff clean. ``--enable-metrics`` is on by default because the platform's monitoring and
    the FinOps spec-decode surface read ``/metrics``; a server without it is unobservable.

    Fields SGLang has no equivalent for are refused rather than dropped, via
    :func:`sglang_unsupported` — a block that asks for something the server will not do must fail
    at render time, not run without it.
    """
    unsupported = sglang_unsupported(config)
    if unsupported:
        raise ValueError("engine block cannot be rendered for SGLang: " + "; ".join(unsupported))
    args: list[str] = []
    if model_path or config.hf_model_id:
        args += ["--model-path", str(model_path or config.hf_model_id)]
    if config.served_model_name:
        args += ["--served-model-name", str(config.served_model_name)]
    if config.dtype and config.dtype not in ("auto", "fp8", "int8", "int4"):
        args += ["--dtype", str(config.dtype)]
    if config.quantization:
        args += ["--quantization", str(config.quantization)]
    elif config.dtype == "fp8":
        # In vLLM's vocabulary dtype=fp8 means fp8 weights; SGLang expresses that as a
        # quantization method, not a dtype.
        args += ["--quantization", "fp8"]
    if config.max_model_len:
        args += ["--context-length", str(config.max_model_len)]
    if config.tensor_parallel_size > 1:
        args += ["--tp-size", str(config.tensor_parallel_size)]
    if config.pipeline_parallel_size > 1:
        args += ["--pp-size", str(config.pipeline_parallel_size)]
    if config.data_parallel_size > 1:
        args += ["--dp-size", str(config.data_parallel_size)]
    if config.gpu_memory_utilization is not None:
        args += ["--mem-fraction-static", str(config.gpu_memory_utilization)]
    if config.max_num_seqs is not None:
        args += ["--max-running-requests", str(config.max_num_seqs)]
    if config.max_num_batched_tokens is not None:
        args += ["--max-prefill-tokens", str(config.max_num_batched_tokens)]
    if config.kv_cache_dtype and config.kv_cache_dtype != "auto":
        kv = "fp8_e4m3" if config.kv_cache_dtype == "fp8" else config.kv_cache_dtype
        args += ["--kv-cache-dtype", str(kv)]
    if config.enable_chunked_prefill is False:
        args += ["--chunked-prefill-size", "-1"]  # SGLang's documented "disable" value
    # RadixAttention prefix caching is on by default in SGLang; emit only the negative form.
    if not config.prefix_cache:
        args.append("--disable-radix-cache")
    if config.trust_remote_code:
        args.append("--trust-remote-code")

    spec = config.speculative_decoding or {}
    if spec.get("enabled"):
        algo = str(spec.get("method") or "EAGLE").upper()
        args += ["--speculative-algorithm", algo]
        if spec.get("draft_model"):
            args += ["--speculative-draft-model-path", str(spec["draft_model"])]
        gamma = int(spec.get("num_speculative_tokens") or 0)
        if gamma > 0 and algo == "NGRAM":
            # NGRAM derives its step count from the draft-token budget itself.
            args += ["--speculative-num-draft-tokens", str(gamma)]
        elif gamma > 0:
            # The EAGLE family (EAGLE/EAGLE3/NEXTN/STANDALONE) auto-chooses steps, top-k and
            # draft tokens *together*: setting draft tokens without steps trips an assertion at
            # server start (sglang arg_groups/speculative_hook.py, _handle_eagle_family). vLLM's
            # ``num_speculative_tokens`` is the draft depth γ, i.e. SGLang's steps on a linear
            # (top-k 1) chain, whose verify width SGLang itself fixes at steps + 1.
            args += [
                "--speculative-num-steps",
                str(gamma),
                "--speculative-eagle-topk",
                "1",
                "--speculative-num-draft-tokens",
                str(gamma + 1),
            ]

    if enable_metrics:
        args.append("--enable-metrics")
    if host:
        args += ["--host", str(host)]
    if port:
        args += ["--port", str(port)]
    return args


def sglang_unsupported(config: EngineConfig) -> list[str]:
    """Engine-block fields that SGLang cannot honour, as human-readable reasons (empty = ok)."""
    reasons: list[str] = []
    if config.swap_space_gb is not None:
        reasons.append("swap_space_gb has no SGLang equivalent (CPU swap is a vLLM feature)")
    if config.multimodal.is_multimodal and (
        config.multimodal.limit_mm_per_prompt or config.multimodal.allowed_media_domains
    ):
        # vLLM enforces these server-side via flags; SGLang has no per-prompt media limit flag,
        # so only the in-process half of the two-ended media guard would run. Refuse rather
        # than silently halve an SSRF/DoS control.
        reasons.append(
            "multimodal media limits are enforced server-side only by vLLM; serve "
            "multimodal models with engine: vllm"
        )
    spec = config.speculative_decoding or {}
    method = spec.get("method")
    if spec.get("enabled") and method and str(method).upper() not in SGLANG_SPEC_ALGORITHMS:
        reasons.append(f"speculative_decoding.method {method!r} not in {SGLANG_SPEC_ALGORITHMS}")
    return reasons


def render_launch_command(config: EngineConfig, **kw: Any) -> str:
    """The full launch command as one shell-ready string (for docs, dry runs and the CLI)."""
    import shlex

    argv = ["python", "-m", "sglang.launch_server", *to_sglang_args(config, **kw)]
    return " ".join(shlex.quote(a) for a in argv)
