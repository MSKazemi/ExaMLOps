"""Correlation context for the evidence chain (ADR 0110 · G1.6 · G1.7).

The audit chain could already answer *what happened*. It could not answer the question the
roadmap's W2 gate actually asks about an autonomous action: **who did it, on whose behalf, under
which mode, and how would it be undone** — because a chain of independent rows records events,
not causation. An orchestrator that triggers a retrain which promotes a model wrote three
unrelated rows.

This module supplies the missing edges as an ambient context rather than a parameter:

- ``correlation_id`` — this unit of work.
- ``parent_correlation_id`` — the unit of work that caused it, set **automatically** when a
  context is entered inside another. That nesting is the orchestrator → tool → downstream chain.
- ``mode`` — ``manual`` · ``delegated`` · ``autonomous``. The distinction ADR 0113 gates on: the
  same agent acting on its own and acting for a person are not the same act.
- ``on_behalf_of`` — the principal the actor is acting for, which ``actor`` alone cannot express.
- ``rollback_ref`` — how to undo it. ADR 0110 decision 4 makes a NULL here on an *autonomous*
  action a policy violation.

**Why ambient and not a parameter.** Roughly two hundred call sites already write audit events.
Threading a correlation id through all of them would be a large, mechanical, error-prone change
whose failure mode is silent — one missed call site is an unexplained gap in a causal chain,
and nothing would report it. A ``contextvars`` context is inherited by everything the unit of
work calls, including across ``await`` boundaries, so a call site gains correlation by being
*inside* the work rather than by remembering to say so.

Nothing here is required: outside any context every field is ``None`` and an event is written
exactly as it was before, which is what keeps this additive over an existing chain whose
integrity is already load-bearing.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "AUTONOMOUS",
    "DELEGATED",
    "MANUAL",
    "MODES",
    "CorrelationContext",
    "correlated",
    "current",
    "new_correlation_id",
]

#: A person did this, directly.
MANUAL = "manual"
#: An agent or automation did this *for* a named principal, who asked for it.
DELEGATED = "delegated"
#: The platform did this on its own initiative — the mode ADR 0113 gates hardest.
AUTONOMOUS = "autonomous"
MODES = (MANUAL, DELEGATED, AUTONOMOUS)


@dataclass(frozen=True)
class CorrelationContext:
    correlation_id: str | None = None
    parent_correlation_id: str | None = None
    mode: str | None = None
    on_behalf_of: str | None = None
    rollback_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "parent_correlation_id": self.parent_correlation_id,
            "mode": self.mode,
            "on_behalf_of": self.on_behalf_of,
            "rollback_ref": self.rollback_ref,
        }

    @property
    def is_empty(self) -> bool:
        """True when this context adds nothing — used to keep the hash byte-identical.

        An event written outside any context must canonicalise exactly as it did before this
        module existed, or every historical row stops verifying.
        """
        return not any(self.as_dict().values())


_EMPTY = CorrelationContext()
_ctx: contextvars.ContextVar[CorrelationContext] = contextvars.ContextVar(
    "examlops_correlation", default=_EMPTY
)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def current() -> CorrelationContext:
    """The active correlation context — empty outside any :func:`correlated` block."""
    return _ctx.get()


@contextmanager
def correlated(
    *,
    correlation_id: str | None = None,
    mode: str | None = None,
    on_behalf_of: str | None = None,
    rollback_ref: str | None = None,
    parent_correlation_id: str | None = None,
    inherit: bool = True,
) -> Iterator[CorrelationContext]:
    """Run a block as one correlated unit of work.

    Entering inside another context makes this one its **child**: ``parent_correlation_id``
    defaults to the enclosing ``correlation_id``, which is what turns a set of rows into a
    reconstructable orchestrator → tool → downstream chain.

    ``inherit`` (default true) carries the enclosing ``mode`` and ``on_behalf_of`` down, because
    a tool called by an autonomous cycle is *also* acting autonomously and recording it as
    ``manual`` would understate what happened. Pass ``inherit=False`` to start a genuinely
    unrelated unit of work in the same process.
    """
    outer = _ctx.get()
    if mode is not None and mode not in MODES:
        raise ValueError(f"mode {mode!r} not in {MODES}")

    ctx = CorrelationContext(
        correlation_id=correlation_id or new_correlation_id(),
        parent_correlation_id=(
            parent_correlation_id
            if parent_correlation_id is not None
            else (outer.correlation_id if inherit else None)
        ),
        mode=mode if mode is not None else (outer.mode if inherit else None),
        on_behalf_of=(
            on_behalf_of if on_behalf_of is not None else (outer.on_behalf_of if inherit else None)
        ),
        # rollback_ref is deliberately NOT inherited: it names how to undo *this* action, and a
        # parent's inverse is not a child's. Inheriting it would let an action claim an undo path
        # that does not undo it — worse than admitting it has none.
        rollback_ref=rollback_ref,
    )
    token = _ctx.set(ctx)
    try:
        yield ctx
    finally:
        _ctx.reset(token)


def with_rollback_ref(ref: str) -> None:
    """Attach an inverse to the *active* unit of work, once it is known.

    A rollback reference often cannot be built before the action runs — you do not know which
    alias to restore until you have read the current one. This sets it on the live context so
    events written afterwards carry it.
    """
    _ctx.set(replace(_ctx.get(), rollback_ref=ref))
