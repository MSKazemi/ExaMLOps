"""The LoRA math, and the seam a real PEFT backend plugs into (ADR 0044 clause 1).

This is a genuine low-rank adaptation, written directly in torch: every adapted ``nn.Linear``
keeps its **frozen** base weight and gains two trainable factors ``A`` (``r x in``) and ``B``
(``out x r``); the forward pass adds ``(B @ A) x * alpha/r`` to the base output, and only ``A``
and ``B`` ever receive a gradient. ``B`` starts at zero, so an untrained adapter is an exact
no-op — the standard LoRA initialisation, and the property that lets a test say "the adapter
changed something" without ambiguity.

:data:`BACKENDS` is the seam. The default ``torch-lora`` backend is the rank-decomposed update
written directly (~40 lines, no optional dependency, runs in every CPU gate). The ``peft`` backend
(:class:`PeftBackend`) hands the same reference stack to Hugging Face PEFT
(``peft.get_peft_model`` + ``LoraConfig``); ``peft`` is the optional ``examlops[finetune]`` extra,
imported lazily, and without it asking for that backend fails loudly
(:class:`BackendNotAvailable`) instead of silently training something else. ``method="full"``
(built-in backend only) trains every weight of the stack — a full fine-tune at reference scale.

The task is **synthetic and declared as such**: tokens drawn uniformly from a small vocabulary,
labelled by a fixed random per-token coefficient vector (a teacher defined in *data* space, not in
the model's). Nothing here claims to be a language model. What it does claim — and what
:mod:`examlops.finetuning.train_lora` measures on a held-out split — is that the adapter weights
were fitted by gradient descent and that the score reported for them was computed, not typed in.
"""

from __future__ import annotations

import math
from typing import Any, Protocol

# Shape of the reference stack. Small enough that a full fine-tune run finishes in under a second
# on one CPU thread, large enough that the task is not trivially separable.
VOCAB = 32
DIM = 32
HIDDEN = 32
CLASSES = 2
SEQ_LEN = 12

#: Which split a batch is drawn from. The generator stream is keyed by the split, so an eval batch
#: can never coincide with a training batch — the held-out set is held out by construction.
TRAIN, EVAL = "train", "eval"
_SPLIT_SALT = {TRAIN: 1_000_003, EVAL: 7_919_003}


class BackendNotAvailable(RuntimeError):
    """A fine-tuning backend was requested that this installation does not have."""


class LoRABackend(Protocol):
    """What a fine-tuning backend must provide (the built-in one, and one day ``peft``)."""

    name: str

    def build(self, *, seed: int, rank: int, alpha: float, method: str) -> Any:
        """Return a trainable model whose only gradient-bearing parameters are the adapter's."""

    def adapter_state(self, model: Any) -> dict[str, Any]:
        """The adapter tensors alone — never the frozen base."""


def _teacher_coefficients(torch: Any, seed: int) -> Any:
    """One fixed coefficient per vocabulary token; a sequence's label is the sign of their sum."""
    g = torch.Generator().manual_seed(seed * 31 + 12_007)
    return torch.randn(VOCAB, generator=g)


def make_batch(torch: Any, seed: int, split: str, index: int, batch_size: int) -> tuple[Any, Any]:
    """A deterministic batch: ``(seed, split, index)`` alone decides it.

    There is therefore no data-loader state to checkpoint, and a resumed run replays exactly the
    batches an uninterrupted run would have seen — the same property the ADR 0032 reference script
    relies on.
    """
    if split not in _SPLIT_SALT:
        raise ValueError(f"split must be one of {tuple(_SPLIT_SALT)}, got {split!r}")
    g = torch.Generator().manual_seed(seed * 9_176_003 + _SPLIT_SALT[split] * (index + 1))
    x = torch.randint(0, VOCAB, (batch_size, SEQ_LEN), generator=g)
    coef = _teacher_coefficients(torch, seed)
    y = (coef[x].sum(dim=1) > 0).long()
    return x, y


def _module_class() -> Any:
    """Build the ``LoRALinear`` module class.

    It is built here rather than declared at module scope so that importing this module never
    imports torch — ``exa`` must start on a machine that has none.
    """
    import torch
    from torch import nn

    class _LoRALinear(nn.Module):
        def __init__(self, base: Any, rank: int, alpha: float, gen: Any) -> None:
            super().__init__()
            if rank < 1:
                raise ValueError("LoRA rank must be >= 1")
            self.base = base
            for p in self.base.parameters():
                p.requires_grad_(False)
            a = torch.randn(rank, base.in_features, generator=gen) / math.sqrt(base.in_features)
            self.lora_A = nn.Parameter(a)
            self.lora_B = nn.Parameter(torch.zeros(base.out_features, rank))
            self.scaling = alpha / rank

        def forward(self, x: Any) -> Any:
            delta = torch.nn.functional.linear(
                torch.nn.functional.linear(x, self.lora_A), self.lora_B
            )
            return self.base(x) + delta * self.scaling

        def delta_weight(self) -> Any:
            """``B @ A * alpha/r`` — the update this adapter adds to the base weight."""
            return (self.lora_B @ self.lora_A) * self.scaling

        def merged_weight(self) -> Any:
            return self.base.weight + self.delta_weight()

    return _LoRALinear


def _reference_stack(lora_cls: Any | None, *, rank: int, alpha: float, gen: Any) -> Any:
    """The frozen-embedding + two-layer MLP reference stack.

    With ``lora_cls`` the two linear layers are wrapped in the built-in LoRA module; without it
    they are plain ``nn.Linear`` — what a full fine-tune trains, and what the PEFT backend hands to
    ``peft.get_peft_model`` (which injects its own LoRA layers into ``fc1``/``fc2``).
    """
    import torch
    from torch import nn

    class _Model(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.emb = nn.Embedding(VOCAB, DIM)
            fc1, fc2 = nn.Linear(DIM, HIDDEN), nn.Linear(HIDDEN, CLASSES)
            self.fc1 = lora_cls(fc1, rank, alpha, gen) if lora_cls else fc1
            self.fc2 = lora_cls(fc2, rank, alpha, gen) if lora_cls else fc2

        def forward(self, x: Any) -> Any:
            pooled = self.emb(x).mean(dim=1)
            return self.fc2(torch.tanh(self.fc1(pooled)))

    return _Model()


def _quantise_frozen(model: Any) -> None:
    """Round every frozen parameter through bfloat16 (the built-in ``qlora`` base)."""
    import torch

    with torch.no_grad():
        for p in model.parameters():
            if not p.requires_grad:
                p.copy_(p.to(torch.bfloat16).to(p.dtype))


class TorchLoRABackend:
    """The built-in backend: a tiny embedding + MLP stack, adapted with real LoRA factors.

    ``method="full"`` is the ADR 0044 clause 4 path at reference scale: the same stack with **no**
    adapter, every parameter trainable, so the run produces a full set of weights (a normal model
    version) rather than a delta. It is single-process; sharding it with FSDP/DeepSpeed is the
    E6 increment and is not built here.
    """

    name = "torch-lora"

    def build(self, *, seed: int, rank: int, alpha: float = 16.0, method: str = "lora") -> Any:
        import torch

        if method not in ("lora", "qlora", "full"):
            raise BackendNotAvailable(
                f"the built-in backend trains lora/qlora adapters or a full fine-tune; "
                f"method {method!r} is not built here"
            )
        torch.manual_seed(seed)
        gen = torch.Generator().manual_seed(seed + 7)
        if method == "full":
            model = _reference_stack(None, rank=rank, alpha=alpha, gen=gen)
            for p in model.parameters():
                p.requires_grad_(True)
            return model
        model = _reference_stack(_module_class(), rank=rank, alpha=alpha, gen=gen)
        for p in model.emb.parameters():
            p.requires_grad_(False)
        if method == "qlora":
            # Honest about the difference: this is the same rank decomposition over a base kept in
            # reduced precision, not bitsandbytes 4-bit NF4. It is recorded as `qlora` only because
            # the base really is quantised; `exa finetune` says so in the run record.
            _quantise_frozen(model)
        return model

    def adapter_state(self, model: Any) -> dict[str, Any]:
        """The trained tensors: the LoRA factors, or — for a full fine-tune — every weight."""
        params = dict(model.named_parameters())
        if not any("lora_" in n for n in params):
            return {n: p.detach().cpu().clone() for n, p in params.items()}
        return {n: p.detach().cpu().clone() for n, p in params.items() if "lora_" in n}


#: The layers the PEFT backend adapts on the reference stack (``LoraConfig.target_modules``).
PEFT_TARGET_MODULES = ("fc1", "fc2")


def peft_available() -> bool:
    """True when ``peft`` is importable (the ``examlops[finetune]`` extra)."""
    import importlib.util

    try:
        return importlib.util.find_spec("peft") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


class PeftBackend:
    """Hugging Face PEFT behind the same seam (ADR 0044 clause 1).

    Builds the reference stack with plain ``nn.Linear`` layers and lets
    ``peft.get_peft_model(model, LoraConfig(...))`` inject PEFT's own LoRA layers into
    :data:`PEFT_TARGET_MODULES` and freeze everything else — the library does the adaptation, not
    this module. ``peft`` is an optional extra (``pip install 'examlops[finetune]'``) imported only
    here; without it the backend refuses with :class:`BackendNotAvailable` rather than silently
    training with the built-in backend. ``qlora`` is refused on this path: PEFT's QLoRA needs a
    bitsandbytes 4-bit base on a CUDA device, and pretending otherwise would mislabel the run.
    """

    name = "peft"

    def _peft(self) -> Any:
        try:
            import peft
        except ImportError as exc:
            raise BackendNotAvailable(
                "the PEFT backend needs the `peft` package, which is not installed: "
                "pip install 'examlops[finetune]' (or use --backend torch-lora)"
            ) from exc
        return peft

    def build(self, *, seed: int, rank: int, alpha: float = 16.0, method: str = "lora") -> Any:
        if method != "lora":
            raise BackendNotAvailable(
                f"the PEFT backend trains method 'lora' here; {method!r} needs a 4-bit "
                "bitsandbytes base on a CUDA device (qlora) or the full-FT path (full)"
            )
        if rank < 1:
            raise ValueError("LoRA rank must be >= 1")
        peft = self._peft()
        import torch

        torch.manual_seed(seed)
        base = _reference_stack(None, rank=rank, alpha=alpha, gen=None)
        config = peft.LoraConfig(
            r=rank,
            lora_alpha=alpha,
            lora_dropout=0.0,
            bias="none",
            target_modules=list(PEFT_TARGET_MODULES),
        )
        model = peft.get_peft_model(base, config)
        # The embedding is not a target, so PEFT froze it along with the base linears; assert the
        # invariant this backend promises instead of trusting it.
        for name, p in model.named_parameters():
            if p.requires_grad and "lora_" not in name:
                raise BackendNotAvailable(
                    f"peft left a non-adapter parameter trainable ({name}); refusing to train"
                )
        return model

    def adapter_state(self, model: Any) -> dict[str, Any]:
        return {
            name: p.detach().cpu().clone()
            for name, p in model.named_parameters()
            if "lora_" in name
        }


BACKENDS: dict[str, Any] = {"torch-lora": TorchLoRABackend(), "peft": PeftBackend()}
DEFAULT_BACKEND = "torch-lora"


def get_backend(name: str | None = None) -> Any:
    backend = BACKENDS.get(name or DEFAULT_BACKEND)
    if backend is None:
        raise BackendNotAvailable(
            f"unknown fine-tuning backend {name!r}; available: {', '.join(sorted(BACKENDS))}"
        )
    return backend


def trainable_parameters(model: Any) -> list[Any]:
    return [p for p in model.parameters() if p.requires_grad]


def adapter_sha256(state: dict[str, Any]) -> str:
    """A digest of the adapter tensors, in a fixed name order."""
    import hashlib

    h = hashlib.sha256()
    for name in sorted(state):
        h.update(name.encode())
        h.update(state[name].contiguous().numpy().tobytes())
    return h.hexdigest()


__all__ = [
    "BACKENDS",
    "CLASSES",
    "DEFAULT_BACKEND",
    "DIM",
    "EVAL",
    "HIDDEN",
    "SEQ_LEN",
    "TRAIN",
    "VOCAB",
    "BackendNotAvailable",
    "LoRABackend",
    "PEFT_TARGET_MODULES",
    "PeftBackend",
    "TorchLoRABackend",
    "adapter_sha256",
    "get_backend",
    "make_batch",
    "peft_available",
    "trainable_parameters",
]
