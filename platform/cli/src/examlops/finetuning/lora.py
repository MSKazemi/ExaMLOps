"""The LoRA math, and the seam a real PEFT backend plugs into (ADR 0044 clause 1).

This is a genuine low-rank adaptation, written directly in torch: every adapted ``nn.Linear``
keeps its **frozen** base weight and gains two trainable factors ``A`` (``r x in``) and ``B``
(``out x r``); the forward pass adds ``(B @ A) x * alpha/r`` to the base output, and only ``A``
and ``B`` ever receive a gradient. ``B`` starts at zero, so an untrained adapter is an exact
no-op — the standard LoRA initialisation, and the property that lets a test say "the adapter
changed something" without ambiguity.

Why not ``peft``/``trl``/``transformers``: the platform ships no multi-gigabyte model and runs
its gates on CPU, so the dependency would buy nothing the gates can exercise. The rank-decomposed
update is ~40 lines, and :data:`BACKENDS` is the seam: a ``peft`` backend implementing
:class:`LoRABackend` slots in behind the same interface, and until one exists asking for it fails
loudly (:class:`BackendNotAvailable`) instead of silently training something else.

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


class TorchLoRABackend:
    """The built-in backend: a tiny embedding + MLP stack, adapted with real LoRA factors."""

    name = "torch-lora"

    def build(self, *, seed: int, rank: int, alpha: float = 16.0, method: str = "lora") -> Any:
        import torch
        from torch import nn

        if method not in ("lora", "qlora"):
            raise BackendNotAvailable(
                f"the built-in backend trains LoRA adapters; method {method!r} is not built here"
            )
        torch.manual_seed(seed)
        gen = torch.Generator().manual_seed(seed + 7)
        lora_cls = _module_class()

        class _Model(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.emb = nn.Embedding(VOCAB, DIM)
                self.fc1 = lora_cls(nn.Linear(DIM, HIDDEN), rank, alpha, gen)
                self.fc2 = lora_cls(nn.Linear(HIDDEN, CLASSES), rank, alpha, gen)
                for p in self.emb.parameters():
                    p.requires_grad_(False)

            def forward(self, x: Any) -> Any:
                pooled = self.emb(x).mean(dim=1)
                return self.fc2(torch.tanh(self.fc1(pooled)))

        model = _Model()
        if method == "qlora":
            # Honest about the difference: this is the same rank decomposition over a base kept in
            # reduced precision, not bitsandbytes 4-bit NF4. It is recorded as `qlora` only because
            # the base really is quantised; `exa finetune` says so in the run record.
            with torch.no_grad():
                for p in model.parameters():
                    if not p.requires_grad:
                        p.copy_(p.to(torch.bfloat16).to(p.dtype))
        return model

    def adapter_state(self, model: Any) -> dict[str, Any]:
        return {
            name: p.detach().cpu().clone()
            for name, p in model.named_parameters()
            if "lora_" in name
        }


class PeftBackend:
    """Placeholder for a Hugging Face PEFT backend — not built, and it says so."""

    name = "peft"

    def build(self, **_: Any) -> Any:
        raise BackendNotAvailable(
            "the PEFT backend is not implemented: `peft`/`transformers` are in no manifest here "
            "and no real base checkpoint ships with the platform. Use --backend torch-lora."
        )

    def adapter_state(self, model: Any) -> dict[str, Any]:  # pragma: no cover - unreachable
        raise BackendNotAvailable("the PEFT backend is not implemented")


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
    "PeftBackend",
    "TorchLoRABackend",
    "adapter_sha256",
    "get_backend",
    "make_batch",
    "trainable_parameters",
]
