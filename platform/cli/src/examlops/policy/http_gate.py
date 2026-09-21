"""Policy verdicts for HTTP decision points — the dashboard's write routes and the control plane.

``examlops.cli._policy_gate`` is the gate for a human at a terminal; this is the same contract for
a request/response service, kept in **one** place so the dashboard and the control plane cannot
drift from each other or from the CLI (ADR 0079 decision 2, ADR 0029 decision 3).

Contract (identical to the CLI's, translated to HTTP):

* **No policy file / no matching rule** -> :attr:`Verdict.ok` with **no audit row**, so a route's
  behaviour and audit trail are byte-identical to what they were before it was gated.
* A rule matched (any effect, an explicit ``allow`` included) -> audited as ``policy:<action>``
  under the *caller's* identity, not the server process's. ``mode: monitor`` rules are audited as
  ``policy_monitor:<action>`` and never block.
* ``deny`` -> **403**, the detail naming the rule.
* ``require_approval`` -> **409** unless the caller acknowledged it (header
  :data:`APPROVAL_HEADER`) — the HTTP form of the CLI's default-*no* confirmation prompt. The
  acknowledgement is the *human's* (a dashboard admin, or the ``exa`` operator who answered the
  prompt); nothing here invents an approver, and an acknowledged approval is itself audited.
* An engine bug -> **403**, and ``decide_safe`` audits that policy was unavailable — fail closed,
  the same as the CLI, PlatformAdmin and the autopilot.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

APPROVAL_HEADER = "X-Policy-Approved"
"""Send ``X-Policy-Approved: true`` to confirm a ``require_approval`` decision."""

_ACK: contextvars.ContextVar[bool] = contextvars.ContextVar("examlops_policy_ack", default=False)


@contextlib.contextmanager
def approval_acknowledged() -> Iterator[None]:
    """While active, ``examlops.cli._client`` sends :data:`APPROVAL_HEADER` on its requests.

    ``exa retrain`` enters this only after the human answered its confirmation prompt, so a
    control plane that re-evaluates the same ``retrain`` rule sees the approval it was owed.
    """
    token = _ACK.set(True)
    try:
        yield
    finally:
        _ACK.reset(token)


def approval_ack_active() -> bool:
    return _ACK.get()


def header_asserts_approval(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "confirmed"}


@dataclass(frozen=True)
class Verdict:
    """What an HTTP decision point should do: ``status`` 200 (proceed), 403 (deny) or 409."""

    status: int
    detail: str = ""
    rule: str | None = None
    effect: str = "allow"

    @property
    def ok(self) -> bool:
        return self.status == 200


def _audit(
    source: str, actor: str, tenant: str, name: str, target: str, details: dict[str, Any]
) -> None:
    """The one audit write of this module; a loss is counted, never raised (a verdict stands)."""
    from examlops.data.audit import audit_best_effort

    audit_best_effort(source, actor, name, target, details, tenant=tenant)


def evaluate(
    action: str,
    context: Mapping[str, Any],
    *,
    actor: str,
    source: str,
    tenant: str = "default",
    approved: bool = False,
    approver_ok: bool = True,
) -> Verdict:
    """Consult ``policy.decide_safe`` for ``action``; return the :class:`Verdict`.

    ``approved`` is whether the caller sent the acknowledgement; ``approver_ok`` is whether *this*
    caller is someone entitled to give it (the dashboard passes ``role == admin``).
    """
    from examlops import policy

    ctx = dict(context)
    decision = policy.decide_safe(action, ctx, default_effect=policy.DENY, audit=False)
    target = str(ctx.get("model") or ctx.get("target") or "")

    def _record(name: str, details: dict[str, Any]) -> None:
        _audit(source, actor, tenant, name, target, details)

    if decision.rule is not None:
        _record(
            f"policy:{action}",
            {"effect": decision.effect, "rule": decision.rule, "via": source},
        )
    for rule, would in decision.shadow:
        _record(
            f"policy_monitor:{action}",
            {"mode": policy.MONITOR, "rule": rule, "would_effect": would, "enforced": False},
        )

    if decision.denied:
        if decision.unavailable:  # decide_safe already audited the unavailability itself
            return Verdict(403, f"Denied by policy: {decision.reason}", None, policy.DENY)
        return Verdict(
            403,
            f"Denied by policy rule '{decision.rule}': {decision.reason}",
            decision.rule,
            policy.DENY,
        )
    if decision.requires_approval:
        if approved and approver_ok:
            _record(
                f"policy_approval:{action}",
                {"rule": decision.rule, "approved_by": actor, "via": source},
            )
            return Verdict(200, "", decision.rule, policy.REQUIRE_APPROVAL)
        who = "" if approver_ok else " (an admin must give it)"
        return Verdict(
            409,
            f"Policy rule '{decision.rule}' requires approval for {action}: a human must "
            f"confirm by re-sending the request with '{APPROVAL_HEADER}: true'{who}.",
            decision.rule,
            policy.REQUIRE_APPROVAL,
        )
    return Verdict(200, "", decision.rule, policy.ALLOW)
