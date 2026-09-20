"""Shared policy-as-code gate for human-driven mutating ``exa`` commands (ADR 0079 decision 2).

``exa retrain`` was the first CLI caller; this is the same pattern for the manual promote
(``manual_promote``) and cluster approval (``cluster_approve``) paths, kept in one place so the
callers cannot drift.

Contract:

* No policy file / no matching rule -> :func:`enforce` returns an ``allow`` decision **without
  writing an audit row**, so the command's behaviour and audit trail are byte-identical to
  what they were before the gate existed.
* A matching rule (any effect, including an explicit ``allow``) is audited via
  ``policy.record_decision`` (``audit_best_effort`` underneath).
* ``deny`` -> an error naming the rule, exit code 1 (``_output.error`` exits).
* ``require_approval`` -> returned to the caller, which folds it into its existing confirmation
  prompt (default answer flips to *no*), exactly as ``exa retrain`` does. A human at the keyboard
  is the approver; an agent principal is already refused by ``_output.confirm``.
* An engine bug denies (``decide_safe``) and audits the unavailability.
"""

from __future__ import annotations

from typing import Any

from . import _output


def enforce(action: str, context: dict[str, Any], *, what: str):
    """Consult ``policy.decide_safe`` for ``action``; exit 1 on deny, else return the decision."""
    from examlops import policy

    decision = policy.decide_safe(action, context, default_effect=policy.DENY, audit=False)
    if decision.rule is not None or decision.shadow:  # an operator rule decided/observed this
        policy.record_decision(action, context, decision)
    if decision.denied:
        _output.error(
            f"Policy denied {what}: {decision.reason}",
            hint="See your policy.yaml or run: exa policy list",
        )
    return decision


def enforce_and_confirm(action: str, context: dict[str, Any], *, what: str, prompt: str) -> bool:
    """:func:`enforce`, then — only if a rule says ``require_approval`` — a default-no confirm.

    For mutating commands that have no confirmation of their own. With no policy applying, this
    never prompts, so the command is byte-identical to before. Returns ``False`` when the human
    declined (the caller reports "aborted" and leaves state unchanged).
    """
    decision = enforce(action, context, what=what)
    if decision.requires_approval:
        if not _output.confirm(f"{prompt} [policy requires approval]", default=False):
            _output.info("Aborted — nothing was changed.")
            return False
    return True


def enforce_engine_gate(result: Any, *, what: str) -> None:
    """Exit 1 naming the reason when an armed engine gate (``policy_engine.gates``) denied.

    ``result`` is the ``EngineDecision`` from ``gates.consult`` — ``None`` (gate off) and any
    allow, including a monitor-mode would-deny, return silently.
    """
    if result is not None and not result.allow:
        _output.error(
            f"Policy gate denied {what}: {'; '.join(result.reasons)}",
            hint="Gate modes: policy.yaml `gates:` or EXAMLOPS_POLICY_GATES; see: exa policy list",
        )
