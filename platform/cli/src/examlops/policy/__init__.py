"""``examlops.policy`` — declarative policy-as-code decision point (ADR 0079, INC-3).

Every mutating operation (retrain, promote, connect-cluster, agent-write) consults :func:`decide`
before acting. A **policy** is a declarative rule authored in ``~/.config/examlops/policy.yaml`` — an
*action*, an optional *condition* over a typed decision context, and an *effect*
(``allow`` / ``deny`` / ``require_approval``). Conditions are evaluated with the **sandboxed**
``simpleeval`` tier (trust tier T2, ADR 0081) — no ``eval``/``exec`` of config. Every decision is
written to ``audit_events`` (EU AI Act Art. 12 alignment).

Backward-compatible by design: with **no policy file** (or no matching rule) the decision is
``allow`` — so existing Phase-29 confirms and the sysadmin approval gate keep running exactly as
before, and policies are *additional* constraints layered on top.

Example ``policy.yaml``::

    policies:
      - action: promote
        when: "rmse_new < rmse_prod and env != 'prod'"   # sandboxed arithmetic/boolean
        effect: allow
      - action: promote
        effect: require_approval           # catch-all: otherwise a human must approve
      - action: agent_write
        when: "action_kind == 'retrain'"
        effect: allow
      - action: agent_write
        effect: deny                       # agents may retrain, nothing else
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("examlops.policy")

ALLOW = "allow"
DENY = "deny"
REQUIRE_APPROVAL = "require_approval"
_VALID_EFFECTS = {ALLOW, DENY, REQUIRE_APPROVAL}


def _normalized_effect(rule: Mapping[str, Any], *, origin: str) -> str:
    """Validate a rule's ``effect``, failing **closed** on anything unrecognized (C8).

    ``effect: Deny`` or ``effect: block`` used to behave as *allow* (any unknown string is not
    ``deny``/``require_approval``, so every ``.allowed``-style check passed). Effects are
    case-insensitive; a value outside {allow, deny, require_approval} is treated as ``deny``
    and logged with the rule's name and where it came from.
    """
    raw = rule.get("effect", ALLOW)
    effect = str(raw).strip().lower()
    if effect not in _VALID_EFFECTS:
        label = str(rule.get("name") or rule.get("action") or "*")
        log.warning(
            "unknown policy effect %r on rule %r (%s) — failing closed (deny); "
            "valid effects: allow, deny, require_approval",
            raw,
            label,
            origin,
        )
        return DENY
    return effect


def _config_dir() -> Path:
    """``EXAMLOPS_CONFIG_DIR`` (shared mount) or ``~/.config/examlops`` — matches providers.loader."""
    env = os.getenv("EXAMLOPS_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".config" / "examlops"


POLICY_YAML = _config_dir() / "policy.yaml"


@dataclass(frozen=True)
class Decision:
    """The outcome of a policy check for one action."""

    effect: str
    rule: str | None
    reason: str

    @property
    def allowed(self) -> bool:
        return self.effect == ALLOW

    @property
    def denied(self) -> bool:
        return self.effect == DENY

    @property
    def requires_approval(self) -> bool:
        return self.effect == REQUIRE_APPROVAL


def load_policies_with_status(
    path: Path | str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """``(rules, error)`` — the ``policies`` list from ``policy.yaml`` and why it is empty.

    A file that cannot be read is **not** the same state as no file at all, and the difference
    matters: the policy layer defaults to ``allow``, so an unparsable ``policy.yaml`` silently
    removes every gate the operator wrote — including the human-approval gate on an autopilot
    promote. Fail-open is deliberate (a broken file must not wedge a mutation path), but it must
    not be *silent*. Callers that can afford to be loud — ``exa policy list``/``test`` — use this
    and say so; :func:`_load_policies` logs a warning and carries on.

    ``error`` is ``None`` for a missing file and for an empty one; both are legitimately "no
    policies". It is set only when the file has content that does not yield rules.
    """
    p = Path(path) if path else POLICY_YAML
    if not p.is_file():
        return [], None
    try:
        import yaml

        with open(p) as fh:
            raw = fh.read()
        data = yaml.safe_load(raw) or {}
    except Exception as exc:  # a malformed file must not break a mutation path
        return [], f"{p} could not be parsed: {exc}"
    if not raw.strip():
        return [], None
    if not isinstance(data, Mapping):
        return [], f"{p} is not a mapping (expected a top-level 'policies:' list)"
    rules = data.get("policies")
    if rules is None:
        return [], f"{p} has no 'policies:' list"
    if not isinstance(rules, list):
        return [], f"{p}: 'policies' must be a list, got {type(rules).__name__}"
    loaded = [dict(r) for r in rules if isinstance(r, Mapping)]
    for r in loaded:  # normalize/validate effects at load time; unknown → deny (fail closed)
        r["effect"] = _normalized_effect(r, origin=str(p))
    return loaded, None


def _load_policies(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Read the ``policies`` list from ``policy.yaml`` (empty when absent/malformed — fail-open)."""
    rules, error = load_policies_with_status(path)
    if error:
        log.warning("policy file ignored — every action falls back to 'allow': %s", error)
    return rules


def _rule_matches(rule: Mapping[str, Any], action: str, context: Mapping[str, Any]) -> bool:
    """Does ``rule`` apply to ``action`` under ``context``? (action match + optional condition)."""
    rule_action = rule.get("action", "*")
    if rule_action != "*" and rule_action != action:
        return False
    when = rule.get("when")
    if not when:
        return True  # catch-all for this action
    from examlops.providers.expression import evaluate_formula

    try:
        return bool(evaluate_formula(str(when), context))
    except Exception:
        # A condition we cannot evaluate (bad expr, or simpleeval not installed) does NOT match —
        # a later catch-all rule decides. Never silently allow on an eval error.
        return False


def decide(
    action: str,
    context: Mapping[str, Any] | None = None,
    *,
    policies: list[dict[str, Any]] | None = None,
    audit: bool = True,
) -> Decision:
    """Return the policy :class:`Decision` for ``action`` under ``context``.

    Rules are evaluated in order; the first matching rule's effect wins. No file / no match → an
    ``allow`` default (backward compatible). ``policies`` injects rules directly (tests); ``audit``
    writes the decision to ``audit_events`` (disable in pure-logic tests).
    """
    ctx = dict(context or {})
    rules = policies if policies is not None else _load_policies()
    decision = Decision(ALLOW, None, "no matching policy — default allow")
    for i, rule in enumerate(rules):
        if _rule_matches(rule, action, ctx):
            # Injected rules (tests / programmatic callers) skip the load-time pass, so the
            # effect is re-validated here; already-normalized values pass through unchanged.
            effect = _normalized_effect(rule, origin="injected policies")
            label = str(rule.get("name") or f"{rule.get('action', '*')}#{i}")
            decision = Decision(effect, label, f"matched policy rule {label!r} → {effect}")
            break
    if audit:
        _audit(action, ctx, decision)
    return decision


def _audit(action: str, context: Mapping[str, Any], decision: Decision) -> None:
    """Record the decision to ``audit_events`` (never raises — audit failure must not block ops)."""
    try:
        from examlops.data.audit import write_audit_event
        from examlops.platform_db import _actor

        write_audit_event(
            source="exa-policy",
            actor=_actor(),
            action=f"policy:{action}",
            target=str(context.get("model") or context.get("target") or ""),
            details={"effect": decision.effect, "rule": decision.rule},
        )
    except Exception:  # pragma: no cover - defensive
        pass
