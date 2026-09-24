"""Next-Gen 40 · B7 — fine-tuning / PEFT / multi-LoRA serving (ADR 0044).

A PEFT/LoRA (and full-FT) fine-tuning workflow that produces **versioned, signed,
eval-gated, lineage-linked adapters**, served multi-LoRA on a shared base with per-request
routing.

- :mod:`examlops.finetuning.train_lora` is the shipped reference script that **really trains**
  LoRA factors (frozen base + rank-decomposed update, :mod:`examlops.finetuning.lora`) and
  measures the adapter on a held-out split; :mod:`examlops.finetuning.runner` runs it and
  registers the adapter from the run's own metrics.
- ``finetune`` registers an adapter *without training it* — the paper record for an adapter
  trained elsewhere. Any score it is given is stored as **operator-asserted and unverified**
  (``asserted_eval_score``), never as ``eval_score``, which only a measured run may write.
- ``promote_adapter`` enforces a **C3 eval-gate** on the measured score: an adapter below its
  quality floor cannot be promoted (R2/GWT-2), and neither can one whose only score is a claim.
- ``MultiLoRARouter`` loads a base **once** and serves many adapters, selecting per request
  by adapter id, with an **LRU hot set** bounding memory (R5/GWT-3/GWT-5), and **refuses**
  an adapter whose recorded base ref does not match the serving base (R4/GWT-4).

Pure-Python and fully testable — no HF PEFT/TRL, GPU, or serving engine required to
register adapters, enforce the eval-gate, or exercise the routing + LRU semantics.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from examlops import data as platform_db

logger = logging.getLogger(__name__)

VALID_METHODS = ("lora", "qlora", "full")


@dataclass
class AdapterVersion:
    adapter_id: str
    base_ref: str
    method: str
    rank: int | None
    dataset_revision: str | None
    #: Measured on a held-out split by the training run itself. ``None`` when nothing measured it.
    eval_score: float | None
    #: Provenance of :attr:`eval_score`: ``"measured"``, or ``None`` when there is no measurement.
    eval_source: str | None
    eval_metric: str | None
    eval_n: int | None
    #: What an operator said the score was. Unverified by construction — kept apart from
    #: :attr:`eval_score` so nothing can read a claim as a measurement.
    asserted_eval_score: float | None
    asserted_eval_by: str | None
    eval_floor: float | None
    signed: bool
    cost_gpu_hours: float | None
    train_run_id: str | None = None
    adapter_sha256: str | None = None


class BaseMismatchError(RuntimeError):
    """Raised when an adapter is served on a base other than its recorded base ref (R4)."""


class EvalGateError(RuntimeError):
    """Raised when a below-floor adapter is promoted (R2/C3 gate)."""


def _default_actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def _sign(
    adapter_id: str, base_ref: str, dataset_revision: str | None
) -> tuple[str | None, str | None]:
    """Sign the adapter identity with the D3 HMAC key; degrade to unsigned when none is configured.

    The distinction between "no key" and "signing broke" lives in
    :func:`examlops.supplychain.sign_or_explain`, which both signing callers share.
    """
    from examlops.supplychain import sign_or_explain

    payload = f"{adapter_id}|{base_ref}|{dataset_revision or ''}"
    return sign_or_explain(payload, subject=f"adapter {adapter_id}")


def _register(
    aid: str,
    base: str,
    method: str,
    dataset_rev: str | None,
    *,
    rank: int | None,
    target_modules: str | None,
    eval_score: float | None,
    eval_source: str | None,
    eval_metric: str | None,
    eval_n: int | None,
    asserted_eval_score: float | None,
    asserted_eval_by: str | None,
    eval_floor: float | None,
    cost_gpu_hours: float | None,
    train_run_id: str | None,
    adapter_sha256: str | None,
    adapter_uri: str | None,
    actor: str | None,
    action: str,
) -> AdapterVersion:
    """The one path that writes an adapter row — signed, lineage-linked, audited."""
    signature, _algo = _sign(aid, base, dataset_rev)
    platform_db.register_adapter(
        aid,
        base,
        method=method,
        rank=rank,
        target_modules=target_modules,
        dataset_revision=dataset_rev,
        eval_score=eval_score,
        eval_source=eval_source,
        eval_metric=eval_metric,
        eval_n=eval_n,
        asserted_eval_score=asserted_eval_score,
        asserted_eval_by=asserted_eval_by,
        eval_floor=eval_floor,
        signature=signature,
        signed_by=actor,
        cost_gpu_hours=cost_gpu_hours,
        train_run_id=train_run_id,
        adapter_sha256=adapter_sha256,
        adapter_uri=adapter_uri,
    )
    _emit_lineage(aid, base, dataset_rev)
    _audit(
        aid,
        action,
        {
            "base": base,
            "method": method,
            "signed": signature is not None,
            "eval_source": eval_source or ("operator_asserted" if asserted_eval_score else None),
            "eval_score": eval_score,
            "asserted_eval_score": asserted_eval_score,
        },
        actor,
    )
    return AdapterVersion(
        adapter_id=aid,
        base_ref=base,
        method=method,
        rank=rank,
        dataset_revision=dataset_rev,
        eval_score=eval_score,
        eval_source=eval_source,
        eval_metric=eval_metric,
        eval_n=eval_n,
        asserted_eval_score=asserted_eval_score,
        asserted_eval_by=asserted_eval_by,
        eval_floor=eval_floor,
        signed=signature is not None,
        cost_gpu_hours=cost_gpu_hours,
        train_run_id=train_run_id,
        adapter_sha256=adapter_sha256,
    )


def finetune(
    base: str,
    method: str,
    dataset_rev: str,
    *,
    adapter_id: str | None = None,
    rank: int = 8,
    target_modules: list[str] | None = None,
    hyperparams: dict[str, Any] | None = None,
    asserted_eval_score: float | None = None,
    asserted_eval_by: str | None = None,
    eval_floor: float = 0.0,
    cost_gpu_hours: float | None = None,
    actor: str | None = None,
) -> AdapterVersion:
    """Register a signed, lineage-linked adapter **without training it** (R3/GWT-1).

    This is the paper record for an adapter produced elsewhere. It deliberately takes no
    ``eval_score``: a number passed here is a claim, is stored as ``asserted_eval_score`` with
    who claimed it, and cannot clear the C3 gate. To obtain a score the platform will stand
    behind, run :func:`examlops.finetuning.runner.run_finetune`, which measures one.
    """
    if method not in VALID_METHODS:
        raise ValueError(f"method must be one of {VALID_METHODS}, got {method!r}")
    return _register(
        adapter_id or f"{base}-{method}-{dataset_rev}"[:120],
        base,
        method,
        dataset_rev,
        rank=rank if method != "full" else None,
        target_modules=",".join(target_modules) if target_modules else None,
        eval_score=None,
        eval_source=None,
        eval_metric=None,
        eval_n=None,
        asserted_eval_score=asserted_eval_score,
        # A claim is only a claim if it is attributable: whoever recorded it is recorded with it.
        asserted_eval_by=(
            (asserted_eval_by or actor or _default_actor())
            if asserted_eval_score is not None
            else None
        ),
        eval_floor=eval_floor,
        cost_gpu_hours=cost_gpu_hours,
        train_run_id=None,
        adapter_sha256=None,
        adapter_uri=None,
        actor=actor,
        action="adapter_registered",
    )


def register_measured_adapter(
    adapter_id: str,
    base: str,
    *,
    method: str = "lora",
    rank: int | None = None,
    target_modules: list[str] | None = None,
    dataset_revision: str | None = None,
    eval_score: float,
    eval_metric: str,
    eval_n: int,
    eval_floor: float = 0.0,
    cost_gpu_hours: float | None = None,
    train_run_id: str | None = None,
    adapter_sha256: str | None = None,
    adapter_uri: str | None = None,
    actor: str | None = None,
) -> AdapterVersion:
    """Register an adapter a training run produced, with the score **that run measured**.

    Called by :func:`examlops.finetuning.runner.run_finetune` from the reference script's own
    metrics line. ``eval_score`` here is evidence: it is stamped ``eval_source='measured'``
    together with the metric, the held-out sample count, the run id and the adapter digest, so a
    reader can go back to the run that produced it.
    """
    if not eval_metric or eval_n <= 0:
        raise ValueError("a measured score needs its metric and a non-empty held-out sample count")
    return _register(
        adapter_id,
        base,
        method,
        dataset_revision,
        rank=rank if method != "full" else None,
        target_modules=",".join(target_modules) if target_modules else None,
        eval_score=float(eval_score),
        eval_source="measured",
        eval_metric=eval_metric,
        eval_n=int(eval_n),
        asserted_eval_score=None,
        asserted_eval_by=None,
        eval_floor=eval_floor,
        cost_gpu_hours=cost_gpu_hours,
        train_run_id=train_run_id,
        adapter_sha256=adapter_sha256,
        adapter_uri=adapter_uri,
        actor=actor,
        action="adapter_registered_measured",
    )


def promote_adapter(
    adapter_id: str, *, actor: str | None = None, accept_unverified: bool = False
) -> None:
    """Promote an adapter — the C3 eval-gate decides, on **measured** evidence (R2/GWT-2).

    Three outcomes, and the middle one is the honesty fix:

    * a measured score below the floor blocks (as it always did);
    * a floor with **no measured score** blocks too — an operator's assertion, or a row whose
      provenance predates the measured/asserted split, is not evidence that the floor was met.
      ``accept_unverified=True`` is the deliberate, audited override;
    * an asserted score below the floor blocks whatever else is true: a claim can condemn an
      adapter, it just cannot clear one.
    """
    row = platform_db.get_adapter(adapter_id)
    if row is None:
        raise ValueError(f"unknown adapter {adapter_id!r}")
    floor = row.get("eval_floor")
    measured = row.get("eval_score") if row.get("eval_source") == "measured" else None
    asserted = row.get("asserted_eval_score")
    if asserted is None and row.get("eval_source") is None and row.get("eval_score") is not None:
        asserted = row["eval_score"]  # a pre-split row: unknown provenance, treated as a claim
    detail = {"measured": measured, "asserted": asserted, "floor": floor}

    if asserted is not None and floor is not None and asserted < floor:
        _audit(adapter_id, "adapter_promotion_blocked", {**detail, "reason": "asserted"}, actor)
        raise EvalGateError(
            f"adapter {adapter_id}: the operator-asserted score {asserted} is below the floor "
            f"{floor} — C3 gate blocks promotion"
        )
    if measured is not None and floor is not None and measured < floor:
        _audit(adapter_id, "adapter_promotion_blocked", {**detail, "reason": "measured"}, actor)
        raise EvalGateError(
            f"adapter {adapter_id} eval {measured} < floor {floor} — C3 gate blocks promotion"
        )
    if measured is None and floor:
        if not accept_unverified:
            _audit(
                adapter_id, "adapter_promotion_blocked", {**detail, "reason": "unmeasured"}, actor
            )
            raise EvalGateError(
                f"adapter {adapter_id} has no measured eval score, so the floor {floor} was "
                "never shown to be met"
                + (
                    f" (the recorded {asserted} is an operator assertion, not a measurement)"
                    if asserted is not None
                    else ""
                )
                + ". Fine-tune it with `exa finetune --train`, or override deliberately with "
                "`--accept-unverified`."
            )
        _audit(adapter_id, "adapter_promoted_unverified", detail, actor)
    platform_db.set_adapter_promoted(adapter_id, True)
    _audit(adapter_id, "adapter_promoted", detail, actor)


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
    "register_measured_adapter",
]
