"""Engine contract + per-model ``engine:`` config + the single ``vllm serve`` argv renderer.

Split out of ``examlops.engines`` so the server engine (``vllm_server``) and the media
guard (``media``) can import the types without a circular import back into the package
``__init__``. Everything here is re-exported from ``examlops.engines`` — importing from
either place gets the same objects.

Track V / ADR 0107: the contract gained an optional **chat-native** surface
(``chat``/``chat_stream``) and a ``multimodal`` block, both additive with defaults that
preserve the pre-Track-V behaviour of every existing engine, YAML file and test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeGuard, runtime_checkable

# ── Engine interface ──────────────────────────────────────────────────────────


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "stop"
    # Speculative-decoding stats (GWT-5); populated only when spec-decode is on.
    accepted_tokens: int = 0
    proposed_tokens: int = 0
    # Track V (R-V2): server-mode latency + modality telemetry. ``ttft_s`` is measured at
    # the first non-empty streamed delta; on a non-streaming call it stays 0.0.
    ttft_s: float = 0.0
    total_s: float = 0.0
    image_count: int = 0
    # ADR 0035 clause 2. ``None`` = the server did not report reasoning usage (never 0, which
    # would read as "reasoned for free"); ``reasoning_text`` is the raw trace and is content.
    reasoning_tokens: int | None = None
    reasoning_text: str | None = None


@runtime_checkable
class InferenceEngine(Protocol):
    name: str

    def generate(self, prompt: str, **kw: Any) -> Completion: ...
    def stream(self, prompt: str, **kw: Any): ...
    def health(self) -> bool: ...


@runtime_checkable
class ChatEngine(Protocol):
    """R-V3 — engines that accept role-tagged messages (and therefore media parts).

    An engine satisfying this Protocol receives OpenAI-style ``messages`` verbatim, so
    multimodal content parts survive. Engines that only satisfy :class:`InferenceEngine`
    get the flattening default in :func:`chat_via_generate`.
    """

    name: str

    def chat(self, messages: list[dict[str, Any]], **kw: Any) -> Completion: ...
    def chat_stream(self, messages: list[dict[str, Any]], **kw: Any): ...


def supports_chat(engine: Any) -> TypeGuard[ChatEngine]:
    """True if ``engine`` accepts messages natively (R-V4 decides pass-through vs flatten).

    Declared as a ``TypeGuard`` rather than plain ``bool`` because the guard is the whole
    point: the gateway calls ``engine.chat(...)`` inside the true branch, and a ``bool``
    return leaves that call reading as an attribute the engine contract does not have.
    """
    return callable(getattr(engine, "chat", None))


# ── Engine config (per-model YAML `engine:` block) ────────────────────────────

_VALID_ENGINES = ("vllm", "vllm-server", "vllm-inproc", "sglang", "echo")
_VALID_DTYPES = ("auto", "float16", "bfloat16", "float32", "int8", "int4", "fp8")
_VALID_MODES = ("server", "inproc")
_VALID_MODALITIES = ("text", "vision", "audio", "video")
_VALID_KV_CACHE_DTYPES = ("auto", "fp8", "fp8_e4m3", "fp8_e5m2")
_MM_KINDS = ("image", "video", "audio")


@dataclass
class MultimodalConfig:
    """Track V (R-V5) — the modality + media-safety block of a per-model ``engine:``.

    The limits are enforced twice on purpose: in-process by :mod:`examlops.engines.media`
    *before* dispatch, and by the vLLM server itself via the flags rendered from this same
    object (:func:`to_vllm_args`). ``allowed_media_domains`` is an SSRF control — an empty
    list means "no remote URLs accepted at all", not "anything goes".
    """

    modality: str = "text"
    limit_mm_per_prompt: dict[str, int] = field(default_factory=dict)
    allowed_media_domains: list[str] = field(default_factory=list)
    allowed_local_media_path: str | None = None
    max_image_bytes: int = 20 * 1024 * 1024  # 20 MiB — generous for a page scan, bounded
    mm_processor_cache_gb: float | None = None
    enable_mm_embeds: bool = False

    @property
    def is_multimodal(self) -> bool:
        return self.modality != "text"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> MultimodalConfig:
        d = d or {}
        limits = {k: int(v) for k, v in (d.get("limit_mm_per_prompt") or {}).items()}
        return cls(
            modality=str(d.get("modality", "text")).lower(),
            limit_mm_per_prompt=limits,
            allowed_media_domains=[str(x).lower() for x in (d.get("allowed_media_domains") or [])],
            allowed_local_media_path=d.get("allowed_local_media_path"),
            max_image_bytes=int(d.get("max_image_bytes", 20 * 1024 * 1024)),
            mm_processor_cache_gb=d.get("mm_processor_cache_gb"),
            enable_mm_embeds=bool(d.get("enable_mm_embeds", False)),
        )


@dataclass
class EngineConfig:
    engine: str = "vllm"
    dtype: str = "auto"
    quantization: str | None = None  # awq | gptq | fp8 | None
    max_model_len: int | None = None
    tensor_parallel_size: int = 1
    prefix_cache: bool = True
    speculative_decoding: dict[str, Any] = field(default_factory=dict)
    # ── Track V additions (all optional; defaults reproduce pre-V behaviour) ──
    mode: str = "server"  # server | inproc — only meaningful for the vllm family
    base_url: str | None = None  # OpenAI-compatible endpoint of a running `vllm serve`
    api_key_secret_ref: str | None = None  # D7 secret name; the value is never stored here
    hf_model_id: str | None = None  # weights to serve (HF id or local path)
    served_model_name: str | None = None  # the name clients use in `model:`
    trust_remote_code: bool = False
    pipeline_parallel_size: int = 1
    data_parallel_size: int = 1
    gpu_memory_utilization: float | None = None
    max_num_seqs: int | None = None
    max_num_batched_tokens: int | None = None
    kv_cache_dtype: str = "auto"
    enable_chunked_prefill: bool | None = None
    swap_space_gb: int | None = None
    multimodal: MultimodalConfig = field(default_factory=MultimodalConfig)
    # ADR 0035 clause 2: the request field this server accepts to cap thinking tokens (for
    # example ``thinking_token_budget`` where the deployed vLLM supports it). Unset = the gateway
    # sends no cap and enforces after the fact from the reported usage; the platform never guesses
    # a provider parameter name.
    reasoning_cap_param: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EngineConfig:
        return cls(
            engine=str(d.get("engine", "vllm")).lower(),
            dtype=str(d.get("dtype", "auto")).lower(),
            quantization=d.get("quantization"),
            max_model_len=d.get("max_model_len"),
            tensor_parallel_size=int(d.get("tensor_parallel_size", 1)),
            prefix_cache=bool(d.get("prefix_cache", True)),
            speculative_decoding=dict(d.get("speculative_decoding", {}) or {}),
            mode=str(d.get("mode", "server")).lower(),
            base_url=d.get("base_url"),
            api_key_secret_ref=d.get("api_key_secret_ref"),
            hf_model_id=d.get("hf_model_id"),
            served_model_name=d.get("served_model_name"),
            trust_remote_code=bool(d.get("trust_remote_code", False)),
            pipeline_parallel_size=int(d.get("pipeline_parallel_size", 1)),
            data_parallel_size=int(d.get("data_parallel_size", 1)),
            gpu_memory_utilization=d.get("gpu_memory_utilization"),
            max_num_seqs=d.get("max_num_seqs"),
            max_num_batched_tokens=d.get("max_num_batched_tokens"),
            kv_cache_dtype=str(d.get("kv_cache_dtype", "auto")).lower(),
            enable_chunked_prefill=d.get("enable_chunked_prefill"),
            swap_space_gb=d.get("swap_space_gb"),
            multimodal=MultimodalConfig.from_dict(d.get("multimodal")),
            reasoning_cap_param=(
                str(d["reasoning_cap_param"]) if d.get("reasoning_cap_param") else None
            ),
        )


def validate_engine_block(block: dict[str, Any]) -> list[str]:
    """Validate a per-model ``engine`` block; return a list of error strings (empty = ok).

    Used by the registry-integrity CI guard (GWT-2) — a bad block fails CI.
    """
    errors: list[str] = []
    if not isinstance(block, dict):
        return ["engine block must be a mapping"]
    engine = str(block.get("engine", "vllm")).lower()
    if engine not in _VALID_ENGINES:
        errors.append(f"engine '{engine}' not in {_VALID_ENGINES}")
    dtype = str(block.get("dtype", "auto")).lower()
    if dtype not in _VALID_DTYPES:
        errors.append(f"dtype '{dtype}' not in {_VALID_DTYPES}")
    tp = block.get("tensor_parallel_size", 1)
    if not isinstance(tp, int) or tp < 1:
        errors.append("tensor_parallel_size must be an int >= 1")
    mml = block.get("max_model_len")
    if mml is not None and (not isinstance(mml, int) or mml < 1):
        errors.append("max_model_len must be a positive int")
    spec = block.get("speculative_decoding")
    if spec is not None and not isinstance(spec, dict):
        errors.append("speculative_decoding must be a mapping")
    if isinstance(spec, dict) and spec.get("enabled") and not spec.get("draft_model"):
        errors.append("speculative_decoding.enabled requires a draft_model")

    # ── Track V fields ────────────────────────────────────────────────────────
    mode = str(block.get("mode", "server")).lower()
    if mode not in _VALID_MODES:
        errors.append(f"mode '{mode}' not in {_VALID_MODES}")
    for key in ("pipeline_parallel_size", "data_parallel_size"):
        val = block.get(key, 1)
        if not isinstance(val, int) or val < 1:
            errors.append(f"{key} must be an int >= 1")
    for key in ("max_num_seqs", "max_num_batched_tokens", "swap_space_gb"):
        val = block.get(key)
        if val is not None and (not isinstance(val, int) or val < 1):
            errors.append(f"{key} must be a positive int")
    gmu = block.get("gpu_memory_utilization")
    if gmu is not None:
        if not isinstance(gmu, int | float) or not (0.0 < float(gmu) <= 1.0):
            errors.append("gpu_memory_utilization must be a float in (0.0, 1.0]")
    kv = str(block.get("kv_cache_dtype", "auto")).lower()
    if kv not in _VALID_KV_CACHE_DTYPES:
        errors.append(f"kv_cache_dtype '{kv}' not in {_VALID_KV_CACHE_DTYPES}")
    base_url = block.get("base_url")
    if base_url is not None and not str(base_url).startswith(("http://", "https://")):
        errors.append("base_url must be an http(s) URL")

    cap = block.get("reasoning_cap_param")
    if cap is not None and not (isinstance(cap, str) and cap.isidentifier()):
        errors.append("reasoning_cap_param must be a request-field name (an identifier)")

    errors.extend(_validate_multimodal(block.get("multimodal")))
    return errors


def _validate_multimodal(mm: Any) -> list[str]:
    if mm is None:
        return []
    if not isinstance(mm, dict):
        return ["multimodal block must be a mapping"]
    errors: list[str] = []
    modality = str(mm.get("modality", "text")).lower()
    if modality not in _VALID_MODALITIES:
        errors.append(f"multimodal.modality '{modality}' not in {_VALID_MODALITIES}")
    limits = mm.get("limit_mm_per_prompt")
    if limits is not None:
        if not isinstance(limits, dict):
            errors.append("multimodal.limit_mm_per_prompt must be a mapping")
        else:
            for kind, val in limits.items():
                if kind not in _MM_KINDS:
                    errors.append(f"limit_mm_per_prompt key '{kind}' not in {_MM_KINDS}")
                if not isinstance(val, int) or val < 0:
                    errors.append(f"limit_mm_per_prompt.{kind} must be an int >= 0")
    domains = mm.get("allowed_media_domains")
    if domains is not None and not isinstance(domains, list):
        errors.append("multimodal.allowed_media_domains must be a list")
    mib = mm.get("max_image_bytes")
    if mib is not None and (not isinstance(mib, int) or mib < 1):
        errors.append("multimodal.max_image_bytes must be a positive int")
    cache_gb = mm.get("mm_processor_cache_gb")
    if cache_gb is not None and (not isinstance(cache_gb, int | float) or float(cache_gb) < 0):
        errors.append("multimodal.mm_processor_cache_gb must be a non-negative number")
    # A vision/video/audio model with no per-prompt limit is an unbounded-input DoS surface.
    if modality != "text" and not (limits or {}):
        errors.append(
            f"multimodal.modality '{modality}' requires limit_mm_per_prompt "
            "(an unbounded media count is a DoS surface)"
        )
    return errors


# ── Sampling params (R-A2) ────────────────────────────────────────────────────

# Generic sampling knobs the gateway/CLI may pass through **kw; mapped 1:1 onto
# vLLM's ``SamplingParams``. Kept as a covered pure-python helper so the passthrough
# logic is testable without a GPU (the vLLM call itself stays GPU-only / pragma).
_SAMPLING_KEYS = ("temperature", "max_tokens", "top_p", "stop", "seed")


def reasoning_tokens_from_usage(usage: Any) -> int | None:
    """Reasoning tokens from an OpenAI-compatible ``usage`` block, or ``None`` when not reported.

    ``usage.completion_tokens_details.reasoning_tokens`` is the field OpenAI-compatible servers
    use. ``None`` means *the backend did not say* - it is never coerced to 0, because a budget
    that passes on an absent count is a budget that passes everything.
    """
    if not isinstance(usage, dict):
        return None
    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        return None
    value = details.get("reasoning_tokens")
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return int(value)


def _sampling_kwargs(kw: dict[str, Any]) -> dict[str, Any]:
    """Extract the sampling params from ``kw`` (drops ``None`` and unknown keys)."""
    return {k: kw[k] for k in _SAMPLING_KEYS if kw.get(k) is not None}


def schema_response_format(schema: dict[str, Any], *, name: str = "response") -> dict[str, Any]:
    """The OpenAI-compatible ``response_format`` that **constrains** a server to ``schema``.

    ADR 0035 clause 1's decoding half: the server compiles the schema into a grammar and can only
    emit text that fits it, instead of being asked in prose and checked afterwards. One function,
    because every server path sends the same body — ``vllm serve``, and any other OpenAI-compatible
    endpoint. ``strict`` is what makes the constraint binding rather than a hint.
    """
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema, "strict": True},
    }


# ── `vllm serve` argv (R-V6) ──────────────────────────────────────────────────


def to_vllm_args(
    config: EngineConfig, *, host: str | None = None, port: int | None = None
) -> list[str]:
    """Render the ``vllm serve`` argv for ``config`` — **the only place this is built**.

    Every substrate consumes this one function: the Compose service command, the
    Slurm/Flux job template, the KServe manifest ``args``, and ``exa serve llm args``.
    That is what makes seam parity (R-CON2/R-V6) real — an operator's ``engine:`` block
    means exactly the same thing on HPC as on Kubernetes, because the flags are generated
    once rather than transcribed three times.

    The model itself is *not* included (the leading positional argument differs per
    substrate: a local path on HPC, an HF id in Compose, a ``storageUri`` on KServe), and
    secrets are never rendered — ``api_key_secret_ref`` is resolved to an environment
    variable by the launcher, never placed on a command line where ``ps`` can read it.
    """
    args: list[str] = []
    if config.served_model_name:
        args += ["--served-model-name", str(config.served_model_name)]
    if config.dtype and config.dtype != "auto":
        args += ["--dtype", str(config.dtype)]
    if config.quantization:
        args += ["--quantization", str(config.quantization)]
    if config.max_model_len:
        args += ["--max-model-len", str(config.max_model_len)]
    if config.tensor_parallel_size > 1:
        args += ["--tensor-parallel-size", str(config.tensor_parallel_size)]
    if config.pipeline_parallel_size > 1:
        args += ["--pipeline-parallel-size", str(config.pipeline_parallel_size)]
    if config.data_parallel_size > 1:
        args += ["--data-parallel-size", str(config.data_parallel_size)]
    if config.gpu_memory_utilization is not None:
        args += ["--gpu-memory-utilization", str(config.gpu_memory_utilization)]
    if config.max_num_seqs is not None:
        args += ["--max-num-seqs", str(config.max_num_seqs)]
    if config.max_num_batched_tokens is not None:
        args += ["--max-num-batched-tokens", str(config.max_num_batched_tokens)]
    if config.kv_cache_dtype and config.kv_cache_dtype != "auto":
        args += ["--kv-cache-dtype", str(config.kv_cache_dtype)]
    if config.swap_space_gb is not None:
        args += ["--swap-space", str(config.swap_space_gb)]
    if config.enable_chunked_prefill is True:
        args.append("--enable-chunked-prefill")
    elif config.enable_chunked_prefill is False:
        args.append("--no-enable-chunked-prefill")
    # Prefix caching is on by default in the config; only emit the negative form so the
    # rendered argv stays minimal and matches vLLM's own default when unset.
    if not config.prefix_cache:
        args.append("--no-enable-prefix-caching")
    if config.trust_remote_code:
        args.append("--trust-remote-code")

    spec = config.speculative_decoding or {}
    if spec.get("enabled") and spec.get("draft_model"):
        payload: dict[str, Any] = {"model": spec["draft_model"]}
        if spec.get("num_speculative_tokens"):
            payload["num_speculative_tokens"] = int(spec["num_speculative_tokens"])
        args += ["--speculative-config", json.dumps(payload, sort_keys=True)]

    args += _multimodal_args(config.multimodal)

    if host:
        args += ["--host", str(host)]
    if port:
        args += ["--port", str(port)]
    return args


def _multimodal_args(mm: MultimodalConfig) -> list[str]:
    """Render the media flags — the server-side half of the two-ended media guard (R-V5)."""
    args: list[str] = []
    for kind in _MM_KINDS:  # deterministic order, so the argv is comparable/diffable
        limit = mm.limit_mm_per_prompt.get(kind)
        if limit is not None:
            args += [f"--limit-mm-per-prompt.{kind}", str(limit)]
    if mm.allowed_media_domains:
        args += ["--allowed-media-domains", *mm.allowed_media_domains]
    if mm.allowed_local_media_path:
        args += ["--allowed-local-media-path", str(mm.allowed_local_media_path)]
    if mm.mm_processor_cache_gb is not None:
        args += ["--mm-processor-cache-gb", str(mm.mm_processor_cache_gb)]
    if mm.enable_mm_embeds:
        args.append("--enable-mm-embeds")
    return args
