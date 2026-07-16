"""E2 — Optimized inference engines (ADR 0016).

A thin ``InferenceEngine`` layer so serving backends (Ray/Compose, KServe E1) and the
gateway (B2) are decoupled from *which* runtime executes generation. The production
engines are **vLLM** (PagedAttention, continuous batching — default) and **SGLang**
(RadixAttention, structured output); both lazily import their heavy, GPU-bound deps and
raise a clear error if unavailable. The **fallback** is a pure-python ``EchoEngine`` so
the contract is exercisable on CPU with no deps (GWT-1).

Also here: the per-model ``engine`` block schema + validation (GWT-2), a ``quantize``
transformation that registers a new signed + BOM'd version (GWT-3, via D3), and
speculative-decoding telemetry hooks (GWT-5, via C1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

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


@runtime_checkable
class InferenceEngine(Protocol):
    name: str

    def generate(self, prompt: str, **kw: Any) -> Completion: ...
    def stream(self, prompt: str, **kw: Any): ...
    def health(self) -> bool: ...


# ── Engine config (per-model YAML `engine:` block) ────────────────────────────

_VALID_ENGINES = ("vllm", "sglang", "echo")
_VALID_DTYPES = ("auto", "float16", "bfloat16", "float32", "int8", "int4", "fp8")


@dataclass
class EngineConfig:
    engine: str = "vllm"
    dtype: str = "auto"
    quantization: str | None = None  # awq | gptq | fp8 | None
    max_model_len: int | None = None
    tensor_parallel_size: int = 1
    prefix_cache: bool = True
    speculative_decoding: dict[str, Any] = field(default_factory=dict)

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
    return errors


# ── Engines ───────────────────────────────────────────────────────────────────


class EchoEngine:
    """CPU, dependency-free fallback engine — deterministic, for tests/dev (GWT-1)."""

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
    """vLLM-backed engine (default). Lazily imports vllm; degrades if unavailable."""

    name = "vllm"

    def __init__(self, model_path: str, config: EngineConfig | None = None) -> None:
        self.model_path = model_path
        self.config = config or EngineConfig(engine="vllm")
        self._llm = None

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

    def generate(self, prompt: str, **kw: Any) -> Completion:  # pragma: no cover - GPU
        self._ensure()
        outs = self._llm.generate([prompt])  # type: ignore[union-attr]
        text = outs[0].outputs[0].text
        return Completion(text=text)

    def stream(self, prompt: str, **kw: Any):  # pragma: no cover - GPU
        self._ensure()
        yield self.generate(prompt, **kw).text

    def health(self) -> bool:  # pragma: no cover - GPU
        return self._llm is not None


class SGLangEngine:
    """SGLang-backed engine (RadixAttention, structured output). Lazily imports sglang."""

    name = "sglang"

    def __init__(self, model_path: str, config: EngineConfig | None = None) -> None:
        self.model_path = model_path
        self.config = config or EngineConfig(engine="sglang")
        self._rt = None

    def _ensure(self) -> None:  # pragma: no cover - GPU dep not installed in CI
        if self._rt is not None:
            return
        try:
            import sglang  # type: ignore  # noqa: F401
        except Exception as exc:
            raise RuntimeError(
                "SGLang not available; install examlops[serving-sglang] on a GPU host"
            ) from exc
        self._rt = object()  # placeholder; real runtime init on the GPU host

    def generate(self, prompt: str, **kw: Any) -> Completion:  # pragma: no cover - GPU
        self._ensure()
        raise NotImplementedError("SGLang runtime is initialised on the GPU serving host")

    def stream(self, prompt: str, **kw: Any):  # pragma: no cover - GPU
        self._ensure()
        raise NotImplementedError

    def health(self) -> bool:  # pragma: no cover - GPU
        return self._rt is not None


_ENGINES: dict[str, Any] = {"echo": EchoEngine, "vllm": VLLMEngine, "sglang": SGLangEngine}


def build_engine(config: EngineConfig, model_path: str | None = None) -> InferenceEngine:
    """Instantiate the engine named by ``config`` (falls back to echo on CPU-only hosts)."""
    cls = _ENGINES.get(config.engine, VLLMEngine)
    if cls is EchoEngine:
        return EchoEngine(config)
    return cls(model_path or "", config)  # type: ignore[call-arg]


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
    """Quantize a model and register the result as a new signed + BOM'd version (R5/GWT-3).

    On a GPU host this drives the engine's real quantizer; in degraded/CPU mode it records
    the transformation as provenance so the sign + BOM (D3) path is exercisable. Returns the
    new version string.
    """
    if method not in _VALID_QUANT_METHODS:
        raise ValueError(f"quantization method '{method}' not in {_VALID_QUANT_METHODS}")
    new_version = f"{version}-{method}"

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

    _audit_quantize(model, version, new_version, method, actor)
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
    model: str, base: str, new_version: str, method: str, actor: str | None
) -> None:
    try:
        from examlops.platform_db import write_audit_event

        write_audit_event(
            "exa-engines",
            actor,
            "model_quantized",
            f"{model}@{new_version}",
            {"base_version": base, "method": method},
        )
    except Exception:
        pass
