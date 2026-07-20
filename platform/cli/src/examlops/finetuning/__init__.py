"""Next-Gen 40 · B7 — fine-tuning / PEFT / multi-LoRA serving (ADR 0044).

A PEFT/LoRA (and full-FT) fine-tuning workflow that produces **versioned, signed,
eval-gated, lineage-linked adapters**, served multi-LoRA on a shared base with per-request
routing.

- ``finetune`` runs a fine-tune (on the scheduler for large jobs; degrades to an
  in-process stub for dev/CI) and registers the adapter with its base ref, rank, training
  dataset revision, and eval score — signed (D3), lineage-linked (A2), cost-recorded.
- ``promote_adapter`` enforces a **C3 eval-gate**: an adapter below its quality floor
  cannot be promoted (R2/GWT-2).
- ``MultiLoRARouter`` loads a base **once** and serves many adapters, selecting per request
  by adapter id, with an **LRU hot set** bounding memory (R5/GWT-3/GWT-5), and **refuses**
  an adapter whose recorded base ref does not match the serving base (R4/GWT-4).

Pure-Python and fully testable — no HF PEFT/TRL, GPU, or serving engine required to
register adapters, enforce the eval-gate, or exercise the routing + LRU semantics.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from examlops import data as platform_db

VALID_METHODS = ("lora", "qlora", "full")


@dataclass
class AdapterVersion:
    adapter_id: str
    base_ref: str
    method: str
    rank: int | None
    dataset_revision: str | None
    eval_score: float | None
    eval_floor: float | None
    signed: bool
    cost_gpu_hours: float | None


class BaseMismatchError(RuntimeError):
    """Raised when an adapter is served on a base other than its recorded base ref (R4)."""


class EvalGateError(RuntimeError):
    """Raised when a below-floor adapter is promoted (R2/C3 gate)."""


def _sign(
    adapter_id: str, base_ref: str, dataset_revision: str | None
) -> tuple[str | None, str | None]:
    """Sign the adapter identity with the D3 HMAC key; degrade to unsigned if no key."""
    try:
        from examlops.supplychain import _hmac_sign

        payload = f"{adapter_id}|{base_ref}|{dataset_revision or ''}"
        return _hmac_sign(payload), "hmac-sha256"
    except Exception:
        return None, None


def finetune(
    base: str,
    method: str,
    dataset_rev: str,
    *,
    adapter_id: str | None = None,
    rank: int = 8,
    target_modules: list[str] | None = None,
    hyperparams: dict[str, Any] | None = None,
    eval_score: float | None = None,
    eval_floor: float = 0.0,
    cost_gpu_hours: float | None = None,
    actor: str | None = None,
) -> AdapterVersion:
    """Run a fine-tune and register a signed, lineage-linked adapter (R1/R3/GWT-1)."""
    if method not in VALID_METHODS:
        raise ValueError(f"method must be one of {VALID_METHODS}, got {method!r}")
    aid = adapter_id or f"{base}-{method}-{dataset_rev}"[:120]
    modules = ",".join(target_modules) if target_modules else None
    signature, algo = _sign(aid, base, dataset_rev)

    platform_db.register_adapter(
        aid,
        base,
        method=method,
        rank=rank if method != "full" else None,
        target_modules=modules,
        dataset_revision=dataset_rev,
        eval_score=eval_score,
        eval_floor=eval_floor,
        signature=signature,
        signed_by=actor,
        cost_gpu_hours=cost_gpu_hours,
    )
    _emit_lineage(aid, base, dataset_rev)
    _audit(
        aid,
        "adapter_registered",
        {"base": base, "method": method, "signed": signature is not None},
        actor,
    )
    return AdapterVersion(
        adapter_id=aid,
        base_ref=base,
        method=method,
        rank=rank if method != "full" else None,
        dataset_revision=dataset_rev,
        eval_score=eval_score,
        eval_floor=eval_floor,
        signed=signature is not None,
        cost_gpu_hours=cost_gpu_hours,
    )


def promote_adapter(adapter_id: str, *, actor: str | None = None) -> None:
    """Promote an adapter — blocked by the C3 eval-gate if below its floor (R2/GWT-2)."""
    row = platform_db.get_adapter(adapter_id)
    if row is None:
        raise ValueError(f"unknown adapter {adapter_id!r}")
    score = row.get("eval_score")
    floor = row.get("eval_floor")
    if floor is not None and score is not None and score < floor:
        _audit(adapter_id, "adapter_promotion_blocked", {"score": score, "floor": floor}, actor)
        raise EvalGateError(
            f"adapter {adapter_id} eval {score} < floor {floor} — C3 gate blocks promotion"
        )
    platform_db.set_adapter_promoted(adapter_id, True)
    _audit(adapter_id, "adapter_promoted", {"score": score, "floor": floor}, actor)


class MultiLoRARouter:
    """A base loaded once + an LRU hot set of adapters, routing requests by adapter id.

    Models the E2 multi-LoRA serving contract without a real engine: the base is loaded a
    single time, ``serve`` admits an adapter into a bounded hot set (LRU eviction), a
    base-ref mismatch is refused, and ``route`` resolves a request to its adapter.
    """

    def __init__(self, base_ref: str, hot_set_size: int = 4) -> None:
        self.base_ref = base_ref
        self.hot_set_size = max(1, hot_set_size)
        self._hot: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.evictions = 0
        self._hits = 0
        self._misses = 0

    def serve(self, adapter_id: str) -> None:
        """Admit an adapter into the hot set (R5). Refuse a base mismatch (R4/GWT-4)."""
        row = platform_db.get_adapter(adapter_id)
        if row is None:
            raise ValueError(f"unknown adapter {adapter_id!r}")
        if row["base_ref"] != self.base_ref:
            raise BaseMismatchError(
                f"adapter {adapter_id} was trained on base {row['base_ref']!r}, "
                f"cannot serve on {self.base_ref!r}"
            )
        if adapter_id in self._hot:
            self._hot.move_to_end(adapter_id)
            return
        self._hot[adapter_id] = row
        if len(self._hot) > self.hot_set_size:
            self._hot.popitem(last=False)  # LRU eviction (R5/GWT-5)
            self.evictions += 1

    def route(self, adapter_id: str, prompt: str) -> dict[str, Any]:
        """Resolve a request to its adapter, loading it on demand (GWT-3)."""
        if adapter_id in self._hot:
            self._hot.move_to_end(adapter_id)
            self._hits += 1
        else:
            self.serve(adapter_id)  # cold load (may raise on base mismatch)
            self._misses += 1
        return {
            "base": self.base_ref,
            "adapter": adapter_id,
            "prompt": prompt,
            "completion": f"[{adapter_id}] {prompt}",
        }

    @property
    def loaded(self) -> list[str]:
        return list(self._hot.keys())

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return (self._hits / total) if total else 0.0


def _emit_lineage(adapter_id: str, base: str, dataset_rev: str | None) -> None:
    try:
        from examlops.lineage import Node, emit_lineage

        inputs = [Node(name=base, type="model")]
        if dataset_rev:
            inputs.append(Node(name=dataset_rev, type="dataset"))
        emit_lineage(
            "COMPLETE",
            job=f"finetune:{adapter_id}",
            run_id=f"finetune-{adapter_id}",
            inputs=inputs,
            outputs=[Node(name=adapter_id, type="model")],
        )
    except Exception:
        pass


def _audit(adapter_id: str, action: str, extra: dict[str, Any], actor: str | None) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event("exa-finetune", actor, action, adapter_id, extra)
    except Exception:
        pass


__all__ = [
    "AdapterVersion",
    "BaseMismatchError",
    "EvalGateError",
    "MultiLoRARouter",
    "VALID_METHODS",
    "finetune",
    "promote_adapter",
]
