"""Generalised rollback — an autonomous action must know how to undo itself (ADR 0113 · 0110).

`exa audit autonomy` can already list what the platform did on its own and which of those
declared no inverse. This is the half that makes the list actionable: **an autonomous action
with no `rollback_ref` is refused before execution**, not reported after.

The distinction that makes the rule workable is between an action that *changed something* and
one that only *recorded something*. A suppressed retrain, a policy denial and a cycle-complete
marker mutate no state, so demanding an inverse for them would be a tax that teaches operators
to declare fake ones — and a fake inverse is worse than a missing one, because it reads as an
undo path that does not undo. Only mutating actions are gated.

The registry is deliberately explicit rather than inferred. Every autonomous action either:

- **declares an inverse** — a command template that undoes it; or
- **is marked not autonomously executable**, with the reason recorded, which means the platform
  may still perform it when a human asks but never on its own initiative; or
- **is record-only**, changing nothing.

A new autonomous action that appears in neither is caught by `tests/unit/test_rollback_registry.py`
rather than silently defaulting to "allowed" — the failure mode that would quietly reopen the gap.

Nothing here restricts a *manual* action. A person acting deliberately may do things the platform
must not do to itself; the asymmetry is the point of ADR 0113's autonomy model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "REGISTRY",
    "AutonomousActionRefused",
    "Inverse",
    "build_rollback_ref",
    "describe",
    "is_gated",
    "register",
    "require_rollback",
]

#: The action changed state and can be undone by running ``template``.
MUTATING = "mutating"
#: The action changed nothing — there is nothing to undo.
RECORD_ONLY = "record_only"
#: The action changes state and has no safe inverse, so it is never done autonomously.
NO_AUTONOMY = "no_autonomy"


class AutonomousActionRefused(PermissionError):
    """An autonomous action was attempted without a declared way to undo it.

    A ``PermissionError`` rather than a ``ValueError``: this is a governance refusal, not a bad
    argument, and callers that broadly catch ``ValueError`` should not swallow it.
    """


@dataclass(frozen=True)
class Inverse:
    action: str
    kind: str
    #: Command template that undoes the action, e.g.
    #: ``exa models rollback run {model} --version {previous_version}``.
    template: str | None = None
    #: Why the action has no autonomous inverse (``NO_AUTONOMY`` only).
    reason: str | None = None

    @property
    def requires_rollback_ref(self) -> bool:
        return self.kind == MUTATING

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "kind": self.kind,
            "template": self.template,
            "reason": self.reason,
            "requires_rollback_ref": self.requires_rollback_ref,
        }


def _m(action: str, template: str) -> Inverse:
    return Inverse(action, MUTATING, template=template)


def _r(action: str) -> Inverse:
    return Inverse(action, RECORD_ONLY)


def _n(action: str, reason: str) -> Inverse:
    return Inverse(action, NO_AUTONOMY, reason=reason)


#: Every action reachable from an autonomous path. Keyed by the audit ``action`` string, which is
#: what the evidence chain already records — so the registry and the trail cannot drift apart by
#: naming the same thing differently.
REGISTRY: dict[str, Inverse] = {
    # ── mutating: these changed something, and say what undoes it ────────────
    "autopilot_retrain_triggered": _m(
        "autopilot_retrain_triggered",
        "exa models rollback run {model} --version {previous_version}",
    ),
    "drift_auto_retrain_triggered": _m(
        "drift_auto_retrain_triggered",
        "exa models rollback run {model} --version {previous_version}",
    ),
    "autopilot_promoted": _m(
        "autopilot_promoted",
        "exa models rollback run {model} --alias {alias} --version {previous_version}",
    ),
    # ── record-only: nothing changed, so nothing to undo ─────────────────────
    "autopilot_cycle_complete": _r("autopilot_cycle_complete"),
    "autopilot_skipped": _r("autopilot_skipped"),
    "autopilot_retrain_error": _r("autopilot_retrain_error"),
    "autopilot_promote_error": _r("autopilot_promote_error"),
    "autopilot_promote_blocked": _r("autopilot_promote_blocked"),
    "autopilot_retrain_suppressed": _r("autopilot_retrain_suppressed"),
    "drift_retrain_suppressed": _r("drift_retrain_suppressed"),
    "policy_denied": _r("policy_denied"),
    "human_approval_required": _r("human_approval_required"),
    # ADR 0113 decisions 1/3/4 — contract, autonomy and interrupt events. All record-only:
    # a denial, a grant record, or an interrupt flag changes governance state that is itself
    # audited; none of them is an autonomous mutation needing an inverse.
    "contract_denied": _r("contract_denied"),
    "autonomy_changed": _r("autonomy_changed"),
    "run_kill_requested": _r("run_kill_requested"),
    "run_freeze_requested": _r("run_freeze_requested"),
    "run_killed": _r("run_killed"),
    "run_frozen": _r("run_frozen"),
    "run_resumed": _r("run_resumed"),
    "model_quarantined": _r("model_quarantined"),
    "model_released": _r("model_released"),
    "corruption_detection_error": _r("corruption_detection_error"),
    "parity_gate_evaluated": _r("parity_gate_evaluated"),
    # ── operator switches: mutating, and their inverse is the opposite command ──
    "autopilot_enabled": _m("autopilot_enabled", "exa autopilot disable"),
    "autopilot_disabled": _m("autopilot_disabled", "exa autopilot enable"),
    "drift_auto_retrain_enabled": _m(
        "drift_auto_retrain_enabled", "exa drift auto-retrain disable {model}"
    ),
    "drift_auto_retrain_disabled": _m(
        "drift_auto_retrain_disabled", "exa drift auto-retrain enable {model}"
    ),
    # ── never autonomous: state-changing with no safe inverse ────────────────
    # Setting a baseline overwrites the previous one, and the platform does not retain it. The
    # inverse would be "restore the old baseline", which there is nothing to restore from — so
    # the honest classification is that a machine may not do this on its own initiative.
    "drift_baseline_set": _n(
        "drift_baseline_set",
        "the previous baseline is overwritten and not retained, so there is nothing to restore",
    ),
    "input_baseline_set": _n(
        "input_baseline_set",
        "the previous baseline is overwritten and not retained, so there is nothing to restore",
    ),
    "corruption_baseline_set": _n(
        "corruption_baseline_set",
        "the previous baseline is overwritten and not retained, so there is nothing to restore",
    ),
    "drift_reset": _n("drift_reset", "cleared snapshots are unrecoverable"),
    "input_reset": _n("input_reset", "cleared snapshots are unrecoverable"),
    "secret_written": _n(
        "secret_written",
        "a rotated secret cannot be un-rotated — the previous value is gone by design",
    ),
    "project_deleted": _n(
        "project_deleted",
        "deletion cascades membership, resources and storage bindings; there is no inverse",
    ),
    "retention_pruned": _n(
        "retention_pruned",
        "pruned telemetry is unrecoverable, which is the point of pruning",
    ),
}


def register(inverse: Inverse) -> None:
    """Add or replace a registry entry (for plugins and tests)."""
    REGISTRY[inverse.action] = inverse


def describe(action: str) -> Inverse | None:
    return REGISTRY.get(action)


def is_gated(action: str) -> bool:
    """Does this action need a ``rollback_ref`` before it may run autonomously?

    An **unregistered** action counts as gated. Defaulting an unknown action to "allowed" is the
    failure that quietly reopens the gap this module exists to close: the next autonomous action
    somebody adds would sail through by virtue of nobody having thought about it.
    """
    entry = REGISTRY.get(action)
    if entry is None:
        return True
    return entry.kind in (MUTATING, NO_AUTONOMY)


def build_rollback_ref(action: str, **params: Any) -> str | None:
    """Render the inverse command for ``action``, or ``None`` if it has no template.

    Missing parameters yield ``None`` rather than a half-formatted string: a rollback reference
    with ``{previous_version}`` still in it looks like an undo path and is not one.
    """
    entry = REGISTRY.get(action)
    if entry is None or not entry.template:
        return None
    try:
        return entry.template.format(**params)
    except (KeyError, IndexError):
        return None


def require_rollback(action: str, *, mode: str | None = None, rollback_ref: str | None = None):
    """Refuse an autonomous mutating action that cannot say how it would be undone.

    ``mode`` and ``rollback_ref`` default to the ambient correlation context, so a caller inside
    a ``with correlated(mode=AUTONOMOUS, ...)`` block is checked without passing anything.

    Only ``autonomous`` is gated. A person may deliberately do things the platform must not do to
    itself, and that asymmetry is ADR 0113's autonomy model rather than an oversight.
    """
    from examlops.evidence import AUTONOMOUS, current

    ctx = current()
    effective_mode = mode if mode is not None else ctx.mode
    effective_ref = rollback_ref if rollback_ref is not None else ctx.rollback_ref
    if effective_mode != AUTONOMOUS:
        return None

    entry = REGISTRY.get(action)
    if entry is not None and entry.kind == NO_AUTONOMY:
        raise AutonomousActionRefused(
            f"{action!r} may not be performed autonomously: {entry.reason}. "
            "A person can still do it deliberately."
        )
    if not is_gated(action):
        return None
    if effective_ref:
        return effective_ref

    known = (
        ""
        if entry is not None
        else (
            " This action is not in the rollback registry; an unregistered action is treated as "
            "gated rather than allowed, so that adding a new autonomous action cannot silently "
            "skip this check."
        )
    )
    hint = f" Expected inverse: {entry.template}" if entry and entry.template else ""
    raise AutonomousActionRefused(
        f"refusing autonomous {action!r}: no rollback_ref declared, so there would be no way to "
        f"undo it (ADR 0110 decision 4).{hint}{known}"
    )
