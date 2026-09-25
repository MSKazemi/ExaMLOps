"""Dry-run and confirmation for every agent-callable write (ADR 0081 rule 3, ADR 0082 layer 4).

ADR 0081's T3 contract says every enabled write is **dry-run-able**, **confirm-gated** (auto-yes
only under an explicit ``--yes``-equivalent), **policy-checked** and **audited**. The policy check
and the audit already live inside each tool (``_agent_write_gate`` / ``_audit_write``); this module
adds the first two to every mutating :class:`~examlops.mcp.tools.ToolSpec` in one wrapper, so a new
tool cannot ship without them (``tests/unit/test_mcp_write_safety.py`` walks the registry).

Every mutating tool gains a keyword-only ``dry_run: bool = False``. ``dry_run=True`` returns what
the call *would* do — intended change, blast radius, the current state it depends on, the policy
decision it would meet, and whether it would need confirmation or a human approval — and mutates
nothing. It is allowed to every principal, an agent included, because it is a read.

High-impact tools (tiers in :func:`confirm_tiers`, default ``B`` and ``C``) additionally gain
``confirm: bool = False``. A **human** principal's direct call without ``confirm=true`` is refused
with ``confirmation_required`` plus the preview, so the MCP client shows its user what is about to
happen and re-sends with ``confirm=true`` — the MCP equivalent of the CLI's ``[y/N]`` prompt.
``EXAMLOPS_MCP_AUTO_CONFIRM`` is the ``--yes`` equivalent for scripted/CI use and must be set
explicitly; nothing is auto-confirmed implicitly (``CI`` alone is deliberately not honoured, so a
CI runner that happens to host an MCP server does not silently drop the gate).

An **agent** principal (``EXAMLOPS_PRINCIPAL_KIND=agent``) cannot confirm for itself — a flag the
model sets is not human consent. Its writes go through plan/apply (ADR 0147), where a tier-B plan
needs a human-minted approval token (:func:`examlops.plans.hitl_required`). Hosts that already
obtained a human decision out of band (Skipper's LangGraph ``interrupt()``) run the call inside
:func:`confirmed`.

Scope decision (consent fatigue, ADR 0082 layer 4): tier-A writes are *not* confirm-gated — they
are the autopilot-OK set, carry MCP ``destructiveHint`` annotations for the client to act on, and
remain dry-run-able, policy-checked and audited.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

__all__ = [
    "CONFIRM_PARAM",
    "DRY_RUN_PARAM",
    "auto_confirm_enabled",
    "confirm_tiers",
    "confirmed",
    "needs_confirmation",
    "with_write_safety",
]

DRY_RUN_PARAM = "dry_run"
CONFIRM_PARAM = "confirm"
#: Control arguments that shape *how* a call runs, never *what* it changes. plan/apply strips them.
CONTROL_PARAMS = frozenset({DRY_RUN_PARAM, CONFIRM_PARAM})

_DEFAULT_CONFIRM_TIERS = frozenset({"B", "C"})
_VALID_TIERS = frozenset({"A", "B", "C"})
_TRUTHY = {"1", "true", "yes", "on"}

_CONFIRMED: contextvars.ContextVar[bool] = contextvars.ContextVar("mcp_confirmed", default=False)


def _parse_tiers(raw: str | None) -> frozenset[str]:
    """Parse a ``A,B,C`` / ``none`` tier list; anything malformed keeps the safe default."""
    if raw is None or not raw.strip():
        return _DEFAULT_CONFIRM_TIERS
    value = raw.strip().lower()
    if value == "none":
        return frozenset()
    tiers = frozenset(t.strip().upper() for t in value.split(",") if t.strip())
    if not tiers or not tiers <= _VALID_TIERS:
        return _DEFAULT_CONFIRM_TIERS  # fail closed: a typo must not switch confirmation off
    return tiers


def confirm_tiers() -> frozenset[str]:
    """Tiers whose direct human calls need ``confirm=true`` (``EXAMLOPS_MCP_CONFIRM_TIERS``)."""
    return _parse_tiers(os.getenv("EXAMLOPS_MCP_CONFIRM_TIERS"))


def auto_confirm_enabled() -> bool:
    """``EXAMLOPS_MCP_AUTO_CONFIRM`` — the explicit ``--yes`` for scripted MCP use."""
    return os.getenv("EXAMLOPS_MCP_AUTO_CONFIRM", "").strip().lower() in _TRUTHY


def needs_confirmation(tier: str) -> bool:
    return tier in confirm_tiers()


@contextmanager
def confirmed() -> Iterator[None]:
    """Mark calls in this context as already confirmed by a human (host-side HITL)."""
    token = _CONFIRMED.set(True)
    try:
        yield
    finally:
        _CONFIRMED.reset(token)


def _principal_kind() -> str:
    kind = os.getenv("EXAMLOPS_PRINCIPAL_KIND", "").strip().lower()
    return "agent" if kind == "agent" else "human"


def _err(message: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "code": code, **extra}


def _preview(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from examlops import plans

    return plans.preview_change(name, args)


def _policy_refusal(preview: dict[str, Any]) -> dict[str, Any] | None:
    """The error the tool's own write gate would return, when the preview says policy refuses.

    Wording matches ``examlops.mcp.tools._agent_write_gate`` so callers see one vocabulary.
    """
    if not preview.get("ok"):
        return None
    policy = (preview.get("preview") or {}).get("policy") or {}
    kind, effect, reason = policy.get("action_kind"), policy.get("effect"), policy.get("reason")
    if effect == "unavailable":
        return _err(f"policy unavailable; refusing agent write ({kind})", "policy_unavailable")
    if effect == "deny":
        return _err(f"policy denied agent write ({kind}): {reason}", "policy_denied")
    if effect == "require_approval":
        return _err(
            f"policy requires human approval for {kind} — not permitted to an agent ({reason})",
            "approval_required",
        )
    return None


def with_write_safety(
    fn: Callable[..., dict[str, Any]], *, name: str, tier: str
) -> Callable[..., dict[str, Any]]:
    """Wrap a mutating tool with keyword-only ``dry_run`` and ``confirm`` parameters.

    Both parameters are on every mutating tool so the schema an MCP client sees never changes
    with an environment variable; whether a call actually *needs* ``confirm`` is decided per call
    from the tool's tier (:func:`needs_confirmation`).
    """
    sig = inspect.signature(fn)
    if DRY_RUN_PARAM in sig.parameters:
        return fn  # already wrapped

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        dry_run = bool(kwargs.pop(DRY_RUN_PARAM, False))
        confirm = bool(kwargs.pop(CONFIRM_PARAM, False))
        from examlops import plans

        if dry_run:
            try:
                bound = sig.bind(*args, **kwargs)
            except TypeError as exc:
                return _err(f"invalid arguments for {name}: {exc}", "invalid_args")
            return _preview(name, dict(bound.arguments))
        # Inside plan (probe) or apply, the plan/apply protocol owns consent: pass straight through.
        # An agent's direct call falls through to the plan gate, which refuses it.
        if (
            plans.current_mode() is None
            and _principal_kind() == "human"
            and needs_confirmation(tier)
            and not (confirm or _CONFIRMED.get() or auto_confirm_enabled())
        ):
            try:
                bound = sig.bind(*args, **kwargs)
            except TypeError as exc:
                return _err(f"invalid arguments for {name}: {exc}", "invalid_args")
            preview = _preview(name, dict(bound.arguments))
            refusal = _policy_refusal(preview)
            if refusal is not None:
                # Policy would refuse anyway: say so, rather than ask to confirm a doomed call.
                # Answered from the preview, never by running the tool, so a policy that flips to
                # allow in between cannot turn this into an unconfirmed write.
                return refusal
            # Fail closed: an unreadable preview still refuses — it never skips the question.
            return _err(
                f"{name} is a tier-{tier} change and needs explicit confirmation: review "
                "the preview, then call again with confirm=true",
                "confirmation_required",
                tool=name,
                tier=tier,
                preview=preview.get("preview") if preview.get("ok") else preview,
            )
        return fn(*args, **kwargs)

    params = list(sig.parameters.values())
    tail = [p for p in params if p.kind is inspect.Parameter.VAR_KEYWORD]
    head = [p for p in params if p.kind is not inspect.Parameter.VAR_KEYWORD]
    extra = [
        inspect.Parameter(p, inspect.Parameter.KEYWORD_ONLY, default=False, annotation=bool)
        for p in (DRY_RUN_PARAM, CONFIRM_PARAM)
    ]
    wrapper.__signature__ = sig.replace(parameters=[*head, *extra, *tail])  # type: ignore[attr-defined]
    # LangChain/pydantic introspectors read __annotations__, not __signature__ (see idempotency).
    wrapper.__annotations__ = {
        **getattr(fn, "__annotations__", {}),
        DRY_RUN_PARAM: bool,
        CONFIRM_PARAM: bool,
    }
    wrapper.__doc__ = (fn.__doc__ or "").rstrip() + (
        "\n\n    Args (write safety, ADR 0081):\n"
        "        dry_run: Preview the change (intent, blast radius, current state, policy decision)"
        " without performing it.\n"
        "        confirm: Explicit confirmation for a high-impact (tier B/C) change, after reviewing"
        " the dry_run preview; an agent principal cannot confirm for itself.\n"
    )
    return wrapper
