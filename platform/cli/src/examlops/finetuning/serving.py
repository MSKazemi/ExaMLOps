"""Multi-LoRA serving engines behind :class:`~examlops.finetuning.MultiLoRARouter` (ADR 0044 cl. 3).

The router owns the *policy* — one base, a bounded LRU hot set, base-mismatch refusal, the
promotion gate. An :class:`AdapterEngine` owns the *mechanism*: making an adapter resident,
evicting it, and running a request through it. Three engines:

``registry``
    No inference. Resolves a request to a registry row and echoes it — the dry-run the router
    always had, kept for operators checking routing without a model host.

``torch``
    **Real inference through a trained adapter, on CPU.** The reference base stack is built
    once; each resident adapter is a verified set of tensors (bundle digest checked against the
    registry before load, ``torch.load(weights_only=True)``) swapped into that one base per
    request under a lock — the multi-LoRA pattern at reference scale. An adapter whose recorded
    frozen-base digest differs from the loaded base is refused, which is base-mismatch refusal
    at the level of the weights, not just the name. The task is the synthetic one the reference
    script trains (``"data": "synthetic"``): the answer is a class and its probabilities.

``vllm``
    The E2 engine. A running ``vllm serve --enable-lora`` (with
    ``VLLM_ALLOW_RUNTIME_LORA_UPDATING=True``) is told to load an adapter with
    ``POST /v1/load_lora_adapter`` and to drop an evicted one with
    ``POST /v1/unload_lora_adapter``; a request is routed to the adapter by naming it as the
    ``model`` of an OpenAI-compatible call, through :class:`~examlops.engines.VLLMServerEngine`.
    The adapter's registered ``adapter_uri`` must be a path the *serving host* can read (a PEFT
    adapter directory). Needs a GPU host; hermetically tested against a stub server.

``render_lora_args`` gives the ``vllm serve`` flags a base needs to accept adapters, so the
Compose / Slurm / KServe launchers can append them to :func:`examlops.engines.to_vllm_args`.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

ENGINE_NAMES = ("registry", "torch", "vllm")


class AdapterServingError(RuntimeError):
    """An adapter could not be made resident, or a request could not be served through it."""


class AdapterEngine(Protocol):
    name: str
    #: True when ``generate`` runs a model; the router gates such engines on promotion.
    serves_inference: bool

    def load(self, row: dict[str, Any]) -> None: ...
    def unload(self, adapter_id: str) -> None: ...
    def generate(self, adapter_id: str, prompt: str, **kw: Any) -> dict[str, Any]: ...


class RegistryEngine:
    """Routing without a model: the request is resolved and echoed (the dry-run path)."""

    name = "registry"
    serves_inference = False

    def load(self, row: dict[str, Any]) -> None:
        return None

    def unload(self, adapter_id: str) -> None:
        return None

    def generate(self, adapter_id: str, prompt: str, **kw: Any) -> dict[str, Any]:
        return {"completion": f"[{adapter_id}] {prompt}", "served": False}


# ── torch (reference stack, real inference) ───────────────────────────────────


def encode_prompt(prompt: str) -> list[int]:
    """Deterministic prompt → token ids for the reference stack (vocab/sequence length fixed)."""
    from examlops.finetuning import lora

    words = prompt.split() or [""]
    ids = [
        int.from_bytes(hashlib.sha256(w.encode()).digest()[:4], "big") % lora.VOCAB for w in words
    ]
    return [ids[i % len(ids)] for i in range(lora.SEQ_LEN)]


class TorchAdapterEngine:
    """One reference base in memory; resident adapters are verified tensors swapped in per call."""

    name = "torch"
    serves_inference = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bases: dict[tuple[Any, ...], Any] = {}
        self._base_sha: str | None = None
        self._resident: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}

    def _base_for(self, config: dict[str, Any]) -> Any:
        from examlops.finetuning import lora, train_lora

        key = (config["backend"], config["method"], int(config["rank"]), float(config["alpha"]))
        model = self._bases.get(key)
        if model is None:
            backend = lora.get_backend(config["backend"])
            model = backend.build(
                seed=int(config["seed"]),
                rank=int(config["rank"]),
                alpha=float(config["alpha"]),
                method=config["method"],
            )
            model.eval()
            actual = train_lora._base_sha256(model)
            if actual != config.get("base_weights_sha256"):
                raise AdapterServingError(
                    "the rebuilt base does not match the digest the adapter was trained on "
                    f"({actual[:12]}… vs {str(config.get('base_weights_sha256'))[:12]}…)"
                )
            self._bases[key] = model
        return model

    def load(self, row: dict[str, Any]) -> None:
        from examlops.finetuning import BaseMismatchError, artifacts

        aid = row["adapter_id"]
        if row.get("method") == "full":
            raise AdapterServingError(
                f"{aid} is a full fine-tune — a model version, not an adapter; serve it as a model"
            )
        uri = row.get("adapter_uri")
        if not uri:
            raise AdapterServingError(f"{aid} has no adapter bundle registered (adapter_uri)")
        directory = Path(uri)
        if not (directory / artifacts.CONFIG_FILE).exists():
            directory = artifacts.bundle_dir(uri)  # a run directory was registered
        try:
            state, config = artifacts.load_bundle(
                directory, expected_sha256=row.get("adapter_sha256")
            )
        except artifacts.BundleError as exc:
            raise AdapterServingError(f"{aid}: {exc}") from exc
        # The tensor digest does not cover adapter_config.json, so hold the config to what the
        # (signed) registry row says before it is allowed to choose the base the tensors run on.
        mismatched = [
            field
            for field, registered in (
                ("adapter_sha256", row.get("adapter_sha256")),
                ("method", row.get("method")),
                ("rank", row.get("rank")),
            )
            if registered is not None and config.get(field) != registered
        ]
        if mismatched:
            raise AdapterServingError(
                f"{aid}: the bundle's adapter_config.json disagrees with the registry on "
                f"{', '.join(mismatched)} — refusing to build a base from it"
            )
        base_sha = config.get("base_weights_sha256")
        with self._lock:
            if self._base_sha is not None and base_sha != self._base_sha:
                raise BaseMismatchError(
                    f"adapter {aid} was trained on base weights {str(base_sha)[:12]}…, this "
                    f"engine serves {self._base_sha[:12]}…"
                )
            try:
                model = self._base_for(config)
            except Exception as exc:
                raise AdapterServingError(f"{aid}: cannot build its base: {exc}") from exc
            names = {n for n, _ in model.named_parameters() if "lora_" in n}
            if set(state) != names:
                raise AdapterServingError(f"{aid}: bundle tensors do not fit the base's adapters")
            self._base_sha = base_sha
            key = (config["backend"], config["method"], int(config["rank"]), float(config["alpha"]))
            self._resident[aid] = (key, state)

    def unload(self, adapter_id: str) -> None:
        with self._lock:
            self._resident.pop(adapter_id, None)

    def generate(self, adapter_id: str, prompt: str, **kw: Any) -> dict[str, Any]:
        import torch

        with self._lock:
            entry = self._resident.get(adapter_id)
            if entry is None:
                raise AdapterServingError(f"{adapter_id} is not resident")
            key, state = entry
            model = self._bases[key]
            params = dict(model.named_parameters())
            with torch.no_grad():
                for name, tensor in state.items():
                    params[name].copy_(tensor)
                x = torch.tensor([encode_prompt(prompt)])
                probs = torch.softmax(model(x), dim=1)[0]
            label = int(probs.argmax())
        return {
            "completion": f"class {label}",
            "label": label,
            "probabilities": [round(float(p), 6) for p in probs],
            "served": True,
            "data": "synthetic",
        }


# ── vLLM (E2 engine, dynamic LoRA) ────────────────────────────────────────────


def render_lora_args(*, max_loras: int = 4, max_lora_rank: int = 16) -> list[str]:
    """The ``vllm serve`` flags that let a base accept adapters at runtime.

    ``max_loras`` is vLLM's per-batch adapter cap — set it to the router's hot-set size so the
    engine's memory bound and the router's LRU agree. Runtime loading additionally needs
    ``VLLM_ALLOW_RUNTIME_LORA_UPDATING=True`` in the server's environment.
    """
    if max_loras < 1 or max_lora_rank < 1:
        raise ValueError("max_loras and max_lora_rank must be >= 1")
    return ["--enable-lora", "--max-loras", str(max_loras), "--max-lora-rank", str(max_lora_rank)]


class VLLMAdapterEngine:
    """Drive a running vLLM server's runtime LoRA API and route requests by adapter name."""

    name = "vllm"
    serves_inference = True

    def __init__(self, base_url: str, *, api_key: str | None = None, timeout: float = 60.0):
        from examlops.engines import EngineConfig, VLLMServerEngine

        self.base_url = base_url.rstrip("/")
        self._client = VLLMServerEngine(
            self.base_url, "", EngineConfig(engine="vllm-server"), api_key=api_key, timeout=timeout
        )
        self._api_key = api_key
        self._timeout = timeout

    def _post(self, path: str, body: dict[str, Any]) -> None:
        from examlops.engines import EngineUnreachable

        try:
            with self._client._request(path, body) as resp:
                resp.read()
        except EngineUnreachable as exc:
            raise AdapterServingError(str(exc)) from exc

    def load(self, row: dict[str, Any]) -> None:
        aid = row["adapter_id"]
        uri = row.get("adapter_uri")
        if not uri:
            raise AdapterServingError(
                f"{aid} has no adapter_uri: register the PEFT adapter directory the serving host "
                "can read (exa finetune … --adapter-uri PATH)"
            )
        self._post("/v1/load_lora_adapter", {"lora_name": aid, "lora_path": str(uri)})

    def unload(self, adapter_id: str) -> None:
        try:
            self._post("/v1/unload_lora_adapter", {"lora_name": adapter_id})
        except AdapterServingError as exc:  # an eviction must not fail the request that caused it
            logger.warning("vLLM did not unload adapter %s: %s", adapter_id, exc)

    def generate(self, adapter_id: str, prompt: str, **kw: Any) -> dict[str, Any]:
        from examlops.engines import EngineConfig, EngineUnreachable, VLLMServerEngine

        engine = VLLMServerEngine(
            self.base_url,
            adapter_id,  # vLLM routes to a LoRA by serving it under its lora_name
            EngineConfig(engine="vllm-server"),
            api_key=self._api_key,
            timeout=self._timeout,
        )
        try:
            comp = engine.generate(prompt, **kw)
        except EngineUnreachable as exc:
            raise AdapterServingError(str(exc)) from exc
        return {
            "completion": comp.text,
            "prompt_tokens": comp.prompt_tokens,
            "completion_tokens": comp.completion_tokens,
            "served": True,
        }


def build_adapter_engine(
    name: str, *, base_url: str | None = None, api_key: str | None = None
) -> AdapterEngine:
    if name == "registry":
        return RegistryEngine()
    if name == "torch":
        return TorchAdapterEngine()
    if name == "vllm":
        import os

        url = base_url or os.getenv("EXAMLOPS_VLLM_BASE_URL")
        if not url:
            raise AdapterServingError(
                "the vllm adapter engine needs --base-url or EXAMLOPS_VLLM_BASE_URL"
            )
        return VLLMAdapterEngine(url, api_key=api_key)
    raise ValueError(f"adapter engine must be one of {ENGINE_NAMES}, got {name!r}")


__all__ = [
    "ENGINE_NAMES",
    "AdapterEngine",
    "AdapterServingError",
    "RegistryEngine",
    "TorchAdapterEngine",
    "VLLMAdapterEngine",
    "build_adapter_engine",
    "encode_prompt",
    "render_lora_args",
]
