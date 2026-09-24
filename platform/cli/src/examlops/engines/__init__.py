"""E2 — Optimized inference engines (ADR 0016, refined by ADR 0107).

A thin ``InferenceEngine`` layer so serving backends (Ray/Compose, KServe E1) and the
gateway (B2) are decoupled from *which* runtime executes generation.

**The production engine is** :class:`~examlops.engines.vllm_server.VLLMServerEngine` — a
client of a running ``vllm serve`` process (ADR 0107). The older in-process engine, which
drives vLLM's offline-batch ``LLM`` API, is retained as ``vllm-inproc`` for corpus scoring;
it cannot batch across concurrent clients and exposes no ``/metrics``, so it is not the
serving path. **SGLang** (RadixAttention, structured output) has no real integration yet — a
roadmap item (ADR 0016/0143), not a runnable engine — so selecting it is refused clearly at
construction time (BL-108) rather than constructing an object that only fails once something
tries to generate with it. The **fallback** is a pure-python :class:`EchoEngine` so the
contract is exercisable on CPU with no deps (GWT-1/GWT-A8).

Also here: the per-model ``engine`` block schema + validation (GWT-2, in :mod:`.config`), a
``quantize`` transformation that registers a new signed + BOM'd version (GWT-3, via D3), and
speculative-decoding telemetry hooks (GWT-5, via C1).

Layout: :mod:`.config` (contract, ``EngineConfig``, ``to_vllm_args``), :mod:`.media` (R-V5
media validation), :mod:`.vllm_server` (the server client). All public names are re-exported
here, so ``from examlops.engines import X`` keeps working for every pre-existing X.
"""

from __future__ import annotations

import importlib.util
import os
import warnings
from typing import Any

from examlops.engines.config import (  # noqa: F401 - re-exported for back-compat
    _MM_KINDS as _MM_KINDS,
)
from examlops.engines.config import (
    _SAMPLING_KEYS as _SAMPLING_KEYS,
)
from examlops.engines.config import (
    _VALID_DTYPES as _VALID_DTYPES,
)
from examlops.engines.config import (
    _VALID_ENGINES as _VALID_ENGINES,
)
from examlops.engines.config import (
    _VALID_MODALITIES as _VALID_MODALITIES,
)
from examlops.engines.config import (
    _VALID_MODES as _VALID_MODES,
)
from examlops.engines.config import (
    ChatEngine,
    Completion,
    EngineConfig,
    InferenceEngine,
    MultimodalConfig,
    _sampling_kwargs,
    supports_chat,
    to_vllm_args,
    validate_engine_block,
)
from examlops.engines.instrumented import (
    InstrumentedChatEngine,
    InstrumentedEngine,
    instrument,
)
from examlops.engines.media import (
    MediaRejected,
    MediaStats,
    flatten_messages,
    normalize_content,
)
from examlops.engines.vllm_server import (
    EngineUnreachable,
    VLLMServerEngine,
    chat_via_generate,
    parse_prometheus_text,
)

__all__ = [
    "ChatEngine",
    "Completion",
    "EchoEngine",
    "EngineConfig",
    "EngineUnreachable",
    "InferenceEngine",
    "InstrumentedChatEngine",
    "InstrumentedEngine",
    "MediaRejected",
    "MediaStats",
    "MultimodalConfig",
    "VLLMEngine",
    "VLLMServerEngine",
    "build_engine",
    "chat_via_generate",
    "flatten_messages",
    "instrument",
    "normalize_content",
    "parse_prometheus_text",
    "quantize_model",
    "record_spec_decode_telemetry",
    "supports_chat",
    "to_vllm_args",
    "validate_engine_block",
]

# ── Engines ───────────────────────────────────────────────────────────────────


class EchoEngine:
    """CPU, dependency-free fallback engine — deterministic, for tests/dev (GWT-1).

    Intentionally has **no** ``chat`` surface: the gateway routes messages through
    :func:`chat_via_generate`, which flattens and warns about dropped media (R-V4). Giving
    this engine a silent ``chat`` would make media loss invisible.
    """

    name = "echo"

    def __init__(self, config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig(engine="echo")

    def generate(self, prompt: str, *, max_tokens: int = 16, **kw: Any) -> Completion:
        toks = prompt.split()
        out = " ".join(toks[:max_tokens])
        spec = self.config.speculative_decoding
        accepted = proposed = 0
        if spec.get("enabled"):
            proposed = len(out.split())
            accepted = proposed  # echo accepts all draft tokens (100% acceptance)
        return Completion(
            text=out,
            prompt_tokens=len(toks),
            completion_tokens=len(out.split()),
            accepted_tokens=accepted,
            proposed_tokens=proposed,
        )

    def stream(self, prompt: str, *, max_tokens: int = 16, **kw: Any):
        for tok in prompt.split()[:max_tokens]:
            yield tok + " "

    def health(self) -> bool:
        return True


class VLLMEngine:
    """In-process vLLM engine (``vllm-inproc``) — vLLM's **offline batch** API.

    Correct for scoring a fixed corpus in one process. **Not** the serving path: it cannot
    batch across concurrent clients, exposes no ``/metrics``, and reloads the weights per
    process (ADR 0107). Use :class:`VLLMServerEngine` to serve. Lazily imports vllm; degrades
    if unavailable.
    """

    name = "vllm-inproc"
    #: vLLM constrains the decoder to a JSON schema (ADR 0035 clause 1) via
    #: ``SamplingParams(guided_decoding=…)``; see :meth:`_sampling_params`.
    constrains_schema = True

    def __init__(self, model_path: str, config: EngineConfig | None = None) -> None:
        self.model_path = model_path
        self.config = config or EngineConfig(engine="vllm-inproc")
        self._llm: Any = None

    def _ensure(self) -> None:
        if self._llm is not None:
            return
        try:
            from vllm import LLM  # type: ignore
        except Exception as exc:  # pragma: no cover - GPU dep not installed in CI
            raise RuntimeError(
                "vLLM not available; install examlops[serving-vllm] on a GPU host "
                "or set the model's engine to 'echo' for CPU dev"
            ) from exc
        self._llm = LLM(
            model=self.model_path,
            dtype=self.config.dtype,
            quantization=self.config.quantization,
            max_model_len=self.config.max_model_len,
            tensor_parallel_size=self.config.tensor_parallel_size,
            enable_prefix_caching=self.config.prefix_cache,
        )

    @staticmethod
    def _sampling_params(kw: dict[str, Any]) -> Any:
        """vLLM ``SamplingParams`` for this call, with the schema constraint when one was asked
        for (ADR 0035 clause 1). Separated from :meth:`generate` so the decision is testable
        without a GPU: only the vLLM imports live here, and a test can supply them.
        """
        from vllm import SamplingParams  # type: ignore

        params = _sampling_kwargs(kw)  # R-A2: sampling passthrough
        schema = kw.get("response_schema")
        if schema is not None:
            from vllm.sampling_params import GuidedDecodingParams  # type: ignore

            params["guided_decoding"] = GuidedDecodingParams(json=schema)
        return SamplingParams(**params)

    def generate(self, prompt: str, **kw: Any) -> Completion:  # pragma: no cover - GPU
        self._ensure()
        sp = self._sampling_params(kw)
        result = self._llm.generate([prompt], sp)[0]
        out = result.outputs[0]
        return Completion(
            text=out.text,
            prompt_tokens=len(getattr(result, "prompt_token_ids", []) or []),
            completion_tokens=len(getattr(out, "token_ids", []) or []),
            finish_reason=getattr(out, "finish_reason", None) or "stop",
        )

    def stream(self, prompt: str, **kw: Any):  # pragma: no cover - GPU
        # Chunk-level only. Token-true streaming is a property of the *server* path
        # (VLLMServerEngine parses SSE frames); an in-process engine would need
        # AsyncLLMEngine and an event loop the CLI does not have — see ADR 0107.
        comp = self.generate(prompt, **kw)
        for tok in comp.text.split():
            yield tok + " "

    def health(self) -> bool:  # R-A3: real readiness (model loaded)
        return self._llm is not None


_ENGINES: dict[str, Any] = {
    "echo": EchoEngine,
    "vllm": VLLMServerEngine,  # `vllm` resolves to the server path by default (ADR 0107)
    "vllm-server": VLLMServerEngine,
    "vllm-inproc": VLLMEngine,
}

# Optional heavy dependency backing each **in-process** engine. The server engine needs
# none — it speaks HTTP to a process that owns the GPU — so it is deliberately absent here.
# `sglang` is intercepted in `_construct_engine` before this is ever consulted (BL-108): there
# is no real integration to gate on dependency availability, so it is refused unconditionally
# rather than through the "dependency missing" path every other engine here uses.
_ENGINE_DEP: dict[str, str] = {"vllm-inproc": "vllm"}


def _dep_available(engine: str) -> bool:
    """True if the engine's runtime dependency is importable (no import side effects)."""
    mod = _ENGINE_DEP.get(engine)
    if mod is None:
        return True
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def _gpu_available() -> bool:
    """True if a CUDA GPU is reachable (torch present + a visible device).

    Used to decide whether real quantization compute can run (R-A4). Absent torch or
    CUDA ⇒ CPU host ⇒ provenance-only. Never raises (a broken torch install ⇒ False).
    """
    try:
        if importlib.util.find_spec("torch") is None:
            return False
        import torch  # type: ignore

        return bool(torch.cuda.is_available())
    except Exception:  # pragma: no cover - defensive (broken torch install)
        return False


def resolve_base_url(config: EngineConfig) -> str | None:
    """The endpoint a server-mode engine should talk to, or ``None`` if there is none.

    Precedence: the per-model ``engine.base_url`` beats the process-wide
    ``EXAMLOPS_VLLM_BASE_URL``, so a single env var can point a whole dev box at one server
    while an individual model still pins its own.
    """
    return config.base_url or os.getenv("EXAMLOPS_VLLM_BASE_URL") or None


def build_engine(
    config: EngineConfig, model_path: str | None = None, *, allow_fallback: bool = True
) -> InferenceEngine:
    """Instantiate the engine named by ``config``, wrapped in GenAI telemetry.

    Resolution is :func:`_construct_engine`; the returned engine is then wrapped by
    :func:`examlops.engines.instrumented.instrument` so every ``generate``/``stream``/``chat``
    emits an OTel GenAI span (ADR 0006 clause 2). The wrapper delegates everything it does not
    instrument and is a no-op unless ``OTEL_SDK_DISABLED`` is falsy, so behaviour, warnings and
    engine identity (``name``, ``base_url``, ``config``) are unchanged.
    """
    engine = _construct_engine(config, model_path, allow_fallback=allow_fallback)
    return instrument(engine, model_path or config.hf_model_id or config.engine)


def _construct_engine(
    config: EngineConfig, model_path: str | None = None, *, allow_fallback: bool = True
) -> InferenceEngine:
    """Resolve and instantiate the engine named by ``config`` (no telemetry wrapper).

    Resolution for the vLLM family (ADR 0107):

    1. ``vllm`` / ``vllm-server`` with a reachable endpoint (``engine.base_url`` or
       ``EXAMLOPS_VLLM_BASE_URL``) ⇒ :class:`VLLMServerEngine`. Needs **no** local ``vllm``
       install — the GPU lives in the server process.
    2. Otherwise ⇒ the in-process ``vllm-inproc`` engine, which does need the dep.

    R-A8: when an engine's runtime dependency is missing, or a server engine has no endpoint
    to talk to, degrade to :class:`EchoEngine` with a ``RuntimeWarning`` so the full
    gateway→engine path stays exercisable with no GPU. Pass ``allow_fallback=False`` to
    require the real engine (raises rather than silently downgrading).
    """
    engine = config.engine
    if engine == "echo":
        return EchoEngine(config)

    if engine in ("vllm", "vllm-server", "vllm-inproc"):
        return _build_vllm(engine, config, model_path, allow_fallback)

    if engine == "sglang":
        # BL-108: no real SGLang integration exists (roadmap item, ADR 0016/0143) — refused here,
        # at construction, rather than constructing an object that only fails on the first
        # generate()/stream() call. `allow_fallback` still governs whether that refusal degrades
        # to EchoEngine (the common path, e.g. through the gateway) or raises (an explicit ask
        # for the real engine has nothing real to give).
        message = (
            "engine 'sglang' has no real integration yet — it is a named roadmap item "
            "(ADR 0016/0143), not a runnable engine. Use 'vllm' (the built, tested engine), "
            "or pass allow_fallback=True to run against EchoEngine instead."
        )
        if allow_fallback:
            warnings.warn(message, RuntimeWarning, stacklevel=3)
            return _echo_fallback(config)
        raise NotImplementedError(message)

    if allow_fallback and not _dep_available(engine):
        warnings.warn(
            f"engine '{engine}' unavailable (runtime dependency not installed); "
            "falling back to EchoEngine (CPU/CI).",
            RuntimeWarning,
            stacklevel=3,
        )
        return _echo_fallback(config)
    cls = _ENGINES.get(engine, VLLMServerEngine)
    return cls(model_path or "", config)  # type: ignore[call-arg]


def _build_vllm(
    engine: str, config: EngineConfig, model_path: str | None, allow_fallback: bool
) -> InferenceEngine:
    wants_server = engine != "vllm-inproc" and config.mode != "inproc"
    base_url = resolve_base_url(config) if wants_server else None

    if wants_server and base_url:
        return VLLMServerEngine(base_url, model_path or config.hf_model_id or "", config)

    if wants_server and not base_url:
        # Explicit server intent with nowhere to send the request. Do not silently start
        # loading weights in-process — that would turn a config mistake into a multi-minute
        # GPU allocation. Fall back loudly, or raise under allow_fallback=False.
        if not allow_fallback:
            raise RuntimeError(
                f"engine '{engine}' is server-mode but no endpoint is configured; set "
                "engine.base_url, EXAMLOPS_VLLM_BASE_URL, or start one with "
                "`exa serve llm start`"
            )
        warnings.warn(
            f"engine '{engine}' is server-mode but no endpoint is configured "
            "(engine.base_url / EXAMLOPS_VLLM_BASE_URL unset); falling back to EchoEngine. "
            "Start one with `exa serve llm start`.",
            RuntimeWarning,
            stacklevel=4,
        )
        return _echo_fallback(config)

    if allow_fallback and not _dep_available("vllm-inproc"):
        warnings.warn(
            "engine 'vllm-inproc' unavailable (runtime dependency not installed); "
            "falling back to EchoEngine (CPU/CI). Install examlops[serving-vllm] "
            "on a GPU host to use it.",
            RuntimeWarning,
            stacklevel=4,
        )
        return _echo_fallback(config)
    return VLLMEngine(model_path or "", config)


def _echo_fallback(config: EngineConfig) -> EchoEngine:
    # Carry spec-decode config so telemetry stays exercisable on the fallback.
    return EchoEngine(EngineConfig(engine="echo", speculative_decoding=config.speculative_decoding))


# ── Quantization (GWT-3) — registers a new signed + BOM'd version ─────────────

_VALID_QUANT_METHODS = ("awq", "gptq", "fp8", "int8")


def quantize_model(
    model: str,
    version: str,
    method: str = "awq",
    *,
    artifact_paths: list | None = None,
    dataset: str | None = None,
    dataset_revision: str | None = None,
    actor: str | None = None,
) -> str:
    """Register a quantization as a new signed + BOM'd version (R5/GWT-3).

    **No quantization compute runs here, on any host.** This records the *intended*
    transformation as provenance — signing it and generating its AI-BOM so the D3 supply-chain
    path is exercisable — and returns the new version string. The weights are unchanged.

    The name says "quantize" because this is where a real AWQ/GPTQ/FP8 quantizer belongs (the A2
    GPU increment); it is not there yet, and a ``RuntimeWarning`` says so on every call. Until it
    is, ADR 0117's portability gate reports ``inert`` for these versions rather than ``passed``:
    an identical result is only evidence of parity when a transformation actually occurred.
    """
    if method not in _VALID_QUANT_METHODS:
        raise ValueError(f"quantization method '{method}' not in {_VALID_QUANT_METHODS}")
    new_version = f"{version}-{method}"

    # R-A4 originally warned only on a CPU host, on the premise that a GPU host ran the real
    # quantizer. It does not: **no quantizer is invoked on either path**, so a GPU host was
    # handed a signed, BOM'd "quantized" version with unchanged weights and no warning at all.
    # The warning is therefore unconditional, and names the GPU case separately rather than
    # treating it as the working one.
    #
    # ADR 0117: what the portability gate needs is whether a quantizer *ran*, which is not the
    # same question as whether a GPU exists. Deriving this from `_gpu_available()` would assert
    # that weights changed on a GPU host where they did not, and the parity gate would then
    # compare a model against itself, find perfect agreement and report `passed` — the exact
    # vacuous-pass trap the gate exists to close, one level up. A future real quantizer sets
    # this True at the point it actually transforms the weights.
    weights_transformed = False
    gpu = _gpu_available()
    warnings.warn(
        f"quantize_model: recording provenance-only for {model} v{new_version} — no "
        f"{method.upper()} quantization compute runs in this function on any host, so the "
        "signed + BOM'd version records the intended transformation but the weights are "
        "unchanged"
        + (
            ". A CUDA GPU is present, but the real quantizer (A2 increment) is not wired in yet"
            if gpu
            else ". Real quantization compute also requires a GPU host (A2 increment)"
        ),
        RuntimeWarning,
        stacklevel=2,
    )

    # Sign + BOM the (quantized) artifact bundle so it enters serving via the D3 gate.
    try:
        from examlops import supplychain

        if artifact_paths:
            supplychain.sign_model(model, new_version, artifact_paths, actor=actor)
        supplychain.generate_ai_bom(
            model,
            new_version,
            dataset=dataset,
            dataset_revision=dataset_revision,
            framework=f"quantized:{method}",
        )
    except Exception:
        # D3 unavailable (e.g. no signing key) — quantization metadata still recorded below.
        pass

    _audit_quantize(model, version, new_version, method, actor, weights_transformed)
    return new_version


def record_spec_decode_telemetry(
    model: str, completion: Completion, *, tenant: str | None = None
) -> dict[str, float]:
    """Emit speculative-decoding acceptance-rate + speedup to C1 telemetry + FinOps (GWT-5)."""
    proposed = max(completion.proposed_tokens, 0)
    accepted = max(completion.accepted_tokens, 0)
    acceptance = (accepted / proposed) if proposed else 0.0
    # A common first-order model: speedup ≈ 1 + acceptance_rate * draft_lookahead(≈1).
    speedup = 1.0 + acceptance
    try:
        from examlops.telemetry import genai

        with genai.genai_span(
            "model", system="examlops", model=model, tenant=tenant or "default"
        ) as span:
            span.set_attribute("examlops.specdecode.acceptance_rate", acceptance)
            span.set_attribute("examlops.specdecode.speedup", speedup)
    except Exception:
        pass
    return {"acceptance_rate": acceptance, "speedup": speedup}


def _audit_quantize(
    model: str,
    base: str,
    new_version: str,
    method: str,
    actor: str | None,
    weights_transformed: bool = False,
) -> None:
    """Record the quantisation, **including whether any weights actually changed**.

    ``weights_transformed`` exists for ADR 0117's portability gate. Without it the gate cannot
    tell a real requantisation from the provenance-only path, and a comparator would then find
    perfect parity between an artefact and itself and green-light the promotion having measured
    nothing. An identical result is only evidence of parity when a transformation occurred.
    """
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event(
            "exa-engines",
            actor,
            "model_quantized",
            f"{model}@{new_version}",
            {
                "base_version": base,
                "method": method,
                "weights_transformed": weights_transformed,
                "provenance_only": not weights_transformed,
            },
        )
    except Exception:
        pass
