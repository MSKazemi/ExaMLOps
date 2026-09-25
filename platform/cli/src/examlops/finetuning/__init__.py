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

The router's mechanism is an :mod:`examlops.finetuning.serving` engine: ``registry`` (routing
only), ``torch`` (real CPU inference through a trained, digest-verified adapter bundle) or
``vllm`` (the E2 server's runtime LoRA API). Engines that serve inference accept only promoted
adapters. Training can run locally or on the phase-23 scheduler
(:mod:`examlops.finetuning.scheduler`), and a trained adapter is logged to MLflow
(:mod:`examlops.finetuning.artifacts`). Registering adapters, the eval-gate and the routing + LRU
semantics need no HF PEFT, GPU or serving engine.
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


def _sign_payload(
    adapter_id: str,
    base_ref: str,
    dataset_revision: str | None,
    adapter_sha256: str | None = None,
    adapter_uri: str | None = None,
) -> str:
    """The signed statement. With no weights attached it is the original identity payload.

    When the row carries the adapter's weights — a tensor digest and/or the location a serving
    engine loads them from — both are bound into the signature too. Otherwise a registry writer
    could keep a valid signature while pointing the row at different weights (a new digest next to
    a new bundle, or a new ``lora_path`` for vLLM), and every serving check downstream would verify
    the substitute against the substituted digest.
    """
    payload = f"{adapter_id}|{base_ref}|{dataset_revision or ''}"
    if adapter_sha256 or adapter_uri:
        payload += f"|sha256:{adapter_sha256 or ''}|uri:{adapter_uri or ''}"
    return payload


def _sign(
    adapter_id: str,
    base_ref: str,
    dataset_revision: str | None,
    adapter_sha256: str | None = None,
    adapter_uri: str | None = None,
) -> tuple[str | None, str | None]:
    """Sign the adapter identity with the D3 HMAC key; degrade to unsigned when none is configured.

    The distinction between "no key" and "signing broke" lives in
    :func:`examlops.supplychain.sign_or_explain`, which both signing callers share.
    """
    from examlops.supplychain import sign_or_explain

    payload = _sign_payload(adapter_id, base_ref, dataset_revision, adapter_sha256, adapter_uri)
    return sign_or_explain(payload, subject=f"adapter {adapter_id}")


def _signing_configured() -> bool:
    """True when a D3 signing key resolves here (so an unsigned row is an anomaly, not policy)."""
    from examlops.supplychain import SigningKeyMissing, _signing_key

    try:
        _signing_key()
    except SigningKeyMissing:
        return False
    except Exception:  # noqa: BLE001 - a broken key store is not "no key"; verification will say so
        return True
    return True


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
    signature, _algo = _sign(aid, base, dataset_rev, adapter_sha256, adapter_uri)
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
    adapter_uri: str | None = None,
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
        adapter_uri=adapter_uri,
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


class UnpromotedAdapterError(RuntimeError):
    """An inference engine was asked to serve an adapter the C3 gate has not promoted."""


class SignatureMismatchError(RuntimeError):
    """An adapter row's HMAC signature does not match its identity — the row was altered."""


def verify_adapter_signature(row: dict[str, Any], *, require_signed: bool = False) -> str:
    """``verified`` / ``unsigned`` / ``unverifiable`` — or raise when the row cannot be trusted.

    The D3 HMAC is recomputed over the same statement :func:`_register` signed: the identity
    ``adapter_id|base_ref|dataset_revision`` plus, when the row carries weights, the tensor digest
    and the adapter URI. A mismatch means the registry row was edited after it was signed — a
    different base or dataset, or different *weights* (digest or location) under a signed id — and
    is refused.

    Fail-closed rules (``require_signed`` is what an engine that serves inference passes):

    * a signer that is configured but **fails** is not "no key": the row is refused, never passed;
    * an **unsigned** row while a signing key is configured here is refused when
      ``require_signed`` — stripping the signature must not be a way around verification. With no
      key configured anywhere, unsigned is the site's documented policy and is reported as such;
    * a signed row with no key configured here is ``unverifiable`` — reported, not a pass.
    """
    import hmac

    from examlops.supplychain import SigningKeyMissing, _hmac_sign

    aid = row["adapter_id"]
    signature = row.get("signature")
    if not signature:
        if require_signed and _signing_configured():
            raise SignatureMismatchError(
                f"adapter {aid} is unsigned but a signing key is configured — an unsigned row "
                "cannot be verified; re-register it (exa finetune / exa serve adapter add)"
            )
        return "unsigned"
    payload = _sign_payload(
        aid,
        row["base_ref"],
        row.get("dataset_revision"),
        row.get("adapter_sha256"),
        row.get("adapter_uri"),
    )
    try:
        expected = _hmac_sign(payload)
    except SigningKeyMissing:
        return "unverifiable"
    except Exception as exc:  # noqa: BLE001 - a broken signer must not read as "no key"
        raise SignatureMismatchError(
            f"adapter {aid}: the signature cannot be checked because signing failed "
            f"({type(exc).__name__}: {exc}); refusing to serve it"
        ) from exc
    if not hmac.compare_digest(str(signature), expected):
        raise SignatureMismatchError(
            f"adapter {aid}: the registry signature does not match its base/dataset/weights "
            "— the row was altered after signing (or signed before weights were bound into the "
            "signature: re-register it); refusing to serve it"
        )
    return "verified"


class MultiLoRARouter:
    """A base loaded once + an LRU hot set of adapters, routing requests by adapter id.

    The policy lives here; the mechanism is an :class:`~examlops.finetuning.serving.AdapterEngine`
    (``registry`` by default — no inference — or ``torch`` / ``vllm``, which really serve through
    the adapter). ``serve`` admits an adapter into a bounded hot set (LRU eviction, which also
    unloads it from the engine), a base-ref mismatch is refused, a signed row whose signature no
    longer matches is refused, and an engine that serves inference only accepts adapters the C3
    gate promoted (``allow_unpromoted=True`` is the audited override).
    """

    def __init__(
        self,
        base_ref: str,
        hot_set_size: int = 4,
        *,
        engine: Any = None,
        allow_unpromoted: bool = False,
        actor: str | None = None,
    ) -> None:
        from examlops.finetuning.serving import RegistryEngine

        self.base_ref = base_ref
        self.hot_set_size = max(1, hot_set_size)
        self.engine = engine or RegistryEngine()
        self.allow_unpromoted = allow_unpromoted
        self.actor = actor
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
        serves = bool(getattr(self.engine, "serves_inference", False))
        try:
            signature = verify_adapter_signature(row, require_signed=serves)
        except SignatureMismatchError as exc:
            _audit(adapter_id, "adapter_serve_refused", {"reason": str(exc)[:300]}, self.actor)
            raise
        if serves and not row.get("promoted"):
            if not self.allow_unpromoted:
                _audit(adapter_id, "adapter_serve_refused", {"reason": "unpromoted"}, self.actor)
                raise UnpromotedAdapterError(
                    f"adapter {adapter_id} is not promoted, so the C3 gate has not passed it; "
                    "promote it (exa serve adapter promote) or pass --allow-unpromoted"
                )
            _audit(
                adapter_id, "adapter_served_unpromoted", {"engine": self.engine.name}, self.actor
            )
        self.engine.load(row)
        self._hot[adapter_id] = {**row, "signature_check": signature}
        if serves:
            _audit(
                adapter_id,
                "adapter_loaded",
                {"engine": self.engine.name, "base": self.base_ref, "signature": signature},
                self.actor,
            )
        if len(self._hot) > self.hot_set_size:
            evicted, _ = self._hot.popitem(last=False)  # LRU eviction (R5/GWT-5)
            self.engine.unload(evicted)
            self.evictions += 1
            logger.info("multi-LoRA hot set evicted %s (size %d)", evicted, self.hot_set_size)

    def route(self, adapter_id: str, prompt: str, **kw: Any) -> dict[str, Any]:
        """Resolve a request to its adapter, loading it on demand (GWT-3)."""
        if adapter_id in self._hot:
            self._hot.move_to_end(adapter_id)
            self._hits += 1
        else:
            self.serve(adapter_id)  # cold load (may raise on base mismatch)
            self._misses += 1
        out = self.engine.generate(adapter_id, prompt, **kw)
        return {
            "base": self.base_ref,
            "adapter": adapter_id,
            "engine": self.engine.name,
            "prompt": prompt,
            **out,
        }

    @property
    def loaded(self) -> list[str]:
        return list(self._hot.keys())

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return (self._hits / total) if total else 0.0

    def stats(self) -> dict[str, Any]:
        return {
            "engine": self.engine.name,
            "hot_set_size": self.hot_set_size,
            "loaded": self.loaded,
            "evictions": self.evictions,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self.hit_rate,
        }


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
    """Fail open, but count the loss: a lost serve-refusal must not read as "never refused"."""
    from examlops.data.audit import audit_best_effort

    audit_best_effort("exa-finetune", actor, action, adapter_id, extra)


__all__ = [
    "AdapterVersion",
    "BaseMismatchError",
    "EvalGateError",
    "MultiLoRARouter",
    "SignatureMismatchError",
    "UnpromotedAdapterError",
    "VALID_METHODS",
    "finetune",
    "promote_adapter",
    "register_measured_adapter",
    "verify_adapter_signature",
]
