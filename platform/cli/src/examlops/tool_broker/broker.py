"""The tool broker: ``invoke(caller, tool, args)`` between an agent and the tool registry.

Order of checks (ADR 0145 d2), each denial audited and none of them running the tool:

1. the tool exists in the registry;
2. the caller's grant set decides (:func:`~examlops.tool_broker.grants.decide_tool_call`) - for
   ``plan_change``/``apply_plan`` the *target* tool's grant is checked too, so a grant for
   ``apply_plan`` cannot launder a mutation the agent holds no grant for;
3. ``needs_approval`` (or an operator ``tool_call`` policy rule) is satisfied only inside an
   ``apply_plan`` whose human approval token verified (``examlops.plans.approval_active``) - never
   by anything the agent puts in its arguments;
4. DNS-level egress check for URL arguments (:mod:`examlops.dataplane.safety`);
5. rate limits (bounded counters in ``platform.db``);
6. credentials named by the grant are read from :mod:`examlops.secrets` at call time and passed to
   the tool as keyword arguments; a value never appears in an audit row or in the result.

Then the real registry function runs. It carries its own plan gate (an agent principal calling a
mutation directly still gets ``plan_required``) and its own ``agent_write`` policy gate - the broker
reuses both by calling it, and adds nothing that would fork them.

``mode='monitor'`` computes and audits the same decision but never blocks and consumes no quota.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from examlops import plans as _plans
from examlops.data import tool_grants as _store
from examlops.data.audit import audit_best_effort
from examlops.tool_broker.grants import (
    GrantSet,
    ToolCall,
    ToolCaller,
    ToolDecision,
    ToolGrant,
    decide_tool_call,
    url_host,
)
from examlops.tool_broker.service import resolve_grant_set

__all__ = [
    "MODES",
    "BrokerContext",
    "broker_mode",
    "caller_from_env",
    "invoke",
    "redact_args",
    "simulate",
]

MODES = ("off", "monitor", "enforce")
_SOURCE = "tool-broker"
_MAX_VALUE = 500
_MAX_KEYS = 50
_PLAN_TOOLS = ("apply_plan", "plan_change")


def broker_mode() -> str:
    """``EXAMLOPS_TOOL_BROKER``: ``off`` (default, byte-identical) | ``monitor`` | ``enforce``.

    An unrecognised value fails *closed* to ``enforce``: reading it as ``off`` would silently
    disable a control someone tried to enable.
    """
    raw = os.getenv("EXAMLOPS_TOOL_BROKER", "").strip().lower()
    if not raw:
        return "off"
    return raw if raw in MODES else "enforce"


def caller_from_env() -> ToolCaller | None:
    """The identity of the process serving the tools, or ``None`` when none is configured.

    stdio MCP is one client per server process, so the process identity *is* the caller:
    ``EXAMLOPS_AGENT_NAME`` (+ optional ``EXAMLOPS_AGENT_VERSION_ID``, ``EXAMLOPS_AGENT_SUBJECT``,
    ``EXAMLOPS_ON_BEHALF_OF``). A configured broker with no identity treats the caller as
    ``anonymous`` - which has no grant set, so it is not brokered.
    """
    name = os.getenv("EXAMLOPS_AGENT_NAME", "").strip()
    subject = os.getenv("EXAMLOPS_AGENT_SUBJECT", "").strip() or None
    if not name and not subject:
        return None
    return ToolCaller(
        agent=name or subject or "anonymous",
        version_id=os.getenv("EXAMLOPS_AGENT_VERSION_ID", "").strip() or None,
        subject=subject,
        on_behalf_of=os.getenv("EXAMLOPS_ON_BEHALF_OF", "").strip() or None,
        session=os.getenv("EXAMLOPS_AGENT_SESSION", "").strip() or None,
        correlation_id=os.getenv("EXAMLOPS_CORRELATION_ID", "").strip() or None,
    )


@dataclass
class BrokerContext:
    mode: str = "enforce"
    tenant: str = "default"
    #: ``True`` when the *runtime* (never the agent's arguments) already obtained a human's
    #: approval for this call - the ADR 0144 d5 interrupt result. Satisfies ``needs_approval``.
    approved: bool = False
    #: ``{name: ToolSpec}``; ``None`` = the real ``examlops.mcp.tools`` registry.
    tools: Mapping[str, Any] | None = None
    #: passed to ``dataplane.safety.check_address`` (tests inject a fake resolver).
    resolver: Any = None
    now: float | None = None
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    def spec(self, name: str) -> Any:
        if self.tools is not None:
            return self.tools.get(name)
        if "reg" not in self._cache:
            from examlops.mcp.tools import REGISTRY

            self._cache["reg"] = {s.name: s for s in REGISTRY}
        return self._cache["reg"].get(name)


# ── redaction ─────────────────────────────────────────────────────────────────


def _redactor() -> tuple[Callable[[str], bool], Callable[..., str]]:
    """The dataplane's key rule and text redactor - one vocabulary, not a second copy."""
    try:
        from examlops.dataplane.safety import is_secret_key, redact

        return is_secret_key, redact
    except Exception:  # noqa: BLE001 - never fail a call because the redactor could not import

        def _key(k: str) -> bool:
            return any(s in k.lower() for s in ("secret", "token", "password", "key", "auth"))

        return _key, lambda text, secrets=(): _mask(text, secrets)


def _mask(text: str, secrets: Any) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def redact_args(args: Any, secrets: tuple[str, ...] = (), _depth: int = 0) -> Any:
    """A copy of ``args`` safe for an audit row: secret-named keys masked, text redacted, bounded.

    ``secrets`` are the values the broker itself injected; they are masked wherever they appear.
    """
    is_secret, redact = _redactor()
    if _depth > 6:
        return "..."
    if isinstance(args, Mapping):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(args.items()):
            if i >= _MAX_KEYS:
                out["..."] = f"{len(args) - _MAX_KEYS} more"
                break
            out[str(k)] = "***" if is_secret(str(k)) else redact_args(v, secrets, _depth + 1)
        return out
    if isinstance(args, list | tuple):
        return [redact_args(v, secrets, _depth + 1) for v in list(args)[:_MAX_KEYS]]
    if isinstance(args, str):
        text = redact(args, secrets=secrets)
        return text if len(text) <= _MAX_VALUE else text[:_MAX_VALUE] + "...[truncated]"
    return args


def _scrub(obj: Any, secrets: tuple[str, ...]) -> Any:
    """Replace every injected secret value in a tool result (a tool may echo its credential)."""
    if not secrets:
        return obj
    if isinstance(obj, str):
        return _mask(obj, secrets)
    if isinstance(obj, dict):
        return {k: _scrub(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, secrets) for v in obj]
    return obj


# ── the gate ──────────────────────────────────────────────────────────────────


@dataclass
class _Gate:
    decision: ToolDecision
    tier: str
    enforced: bool
    approved: bool = False
    creds: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.enforced and self.decision.effect != "allow"


_RANK = {"deny": 2, "require_approval": 1, "allow": 0}


def _worst(a: ToolDecision, b: ToolDecision) -> ToolDecision:
    return b if _RANK[b.effect] > _RANK[a.effect] else a


def _plan_target(tool: str, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The tool a ``plan_change``/``apply_plan`` call is really about, or ``None``."""
    if tool == "plan_change":
        t = args.get("tool")
        a = args.get("args")
        return (t, dict(a or {})) if isinstance(t, str) else None
    if tool == "apply_plan":
        h = args.get("plan_hash")
        if not isinstance(h, str):
            return None
        got = _plans.get_plan(h)
        plan = got.get("plan") if got.get("ok") else None
        if isinstance(plan, dict) and isinstance(plan.get("tool"), str):
            return plan["tool"], dict(plan.get("args") or {})
    return None


def _plan_approved(tool: str, args: Mapping[str, Any]) -> bool:
    """``apply_plan`` of a plan a human approved, with a token presented (``apply_plan`` itself
    verifies the token and refuses a wrong one, so this only decides whether to *try*)."""
    if tool != "apply_plan" or not args.get("approval_token"):
        return False
    h = args.get("plan_hash")
    got = _plans.get_plan(h) if isinstance(h, str) else {}
    plan = got.get("plan") if got.get("ok") else None
    return bool(isinstance(plan, dict) and plan.get("approved"))


def _approval_given(ctx: BrokerContext, tool: str, args: Mapping[str, Any]) -> bool:
    return _plans.approval_active() or ctx.approved or _plan_approved(tool, args)


def _deny(code: str, reason: str, grant_set: GrantSet | None, **kw: Any) -> ToolDecision:
    return ToolDecision("deny", code, reason, grant_set.subject if grant_set else None, **kw)


def _gate(
    caller: ToolCaller,
    tool: str,
    args: dict[str, Any],
    ctx: BrokerContext,
    *,
    dry: bool,
) -> _Gate:
    """Everything that decides whether the tool may run; touches no tool, writes no audit."""
    enforced = ctx.mode == "enforce"
    spec = ctx.spec(tool)
    if spec is None:
        return _Gate(
            ToolDecision("deny", "unknown_tool", f"{tool!r} is not a registered tool"),
            "read",
            enforced,
        )
    tier = getattr(spec, "tier", "read")
    try:
        grant_set = resolve_grant_set(caller)
    except Exception as exc:  # noqa: BLE001 - cannot tell whether this caller is restricted
        return _Gate(
            ToolDecision("deny", "grants_unavailable", f"grant store unavailable: {exc}"),
            tier,
            enforced,
        )
    decision = decide_tool_call(grant_set, ToolCall(caller, tool, args, tier))
    if tool in _PLAN_TOOLS:
        target = _plan_target(tool, args)
        tspec = ctx.spec(target[0]) if target else None
        if target and tspec is not None:
            inner = decide_tool_call(
                grant_set, ToolCall(caller, target[0], target[1], getattr(tspec, "tier", "read"))
            )
            if tool == "plan_change" and inner.effect == "require_approval":
                # Planning changes nothing; the approval the target needs is asked at apply time.
                inner = ToolDecision("allow", "granted", inner.reason, inner.subject, inner.grant)
            decision = _worst(decision, inner)
        elif target and grant_set is not None:
            decision = _worst(
                decision, _deny("unknown_tool", f"plan target {target[0]!r} is unknown", grant_set)
            )
    gate = _Gate(decision, tier, enforced)
    if decision.effect == "deny":
        return gate
    if decision.effect == "require_approval":
        if _approval_given(ctx, tool, args):
            gate.approved = True
            decision = ToolDecision(
                "allow", "approved", "approved by a human", decision.subject, decision.grant
            )
            gate.decision = decision
        else:
            return gate
    grant = decision.grant
    # Operator policy: an optional `tool_call` rule in policy.yaml. No rule -> allow, no audit row.
    pol = _policy(caller, tool, tier, dry=dry)
    if pol is not None:
        if pol.unavailable or pol.denied:
            gate.decision = _deny(
                "policy_unavailable" if pol.unavailable else "policy_denied",
                pol.reason,
                grant_set,
                grant=grant,
            )
            return gate
        if pol.requires_approval and not _approval_given(ctx, tool, args):
            gate.decision = ToolDecision(
                "require_approval", "policy_requires_approval", pol.reason, decision.subject, grant
            )
            return gate
    if grant is not None:
        stop = _stateful(gate, grant, caller, tool, args, ctx, dry=dry)
        if stop is not None:
            gate.decision = stop
            return gate
    return gate


def _policy(caller: ToolCaller, tool: str, tier: str, *, dry: bool) -> Any:
    from examlops import policy

    ctx = {"tool": tool, "agent": caller.agent, "tier": tier, "target": f"{caller.agent}:{tool}"}
    d = policy.decide_safe("tool_call", ctx, default_effect=policy.DENY, audit=False)
    if d.rule is None and not d.shadow and not d.unavailable:
        return None
    if not dry and (d.rule is not None or d.shadow):
        policy.record_decision("tool_call", ctx, d)
    return d


def _stateful(
    gate: _Gate,
    grant: ToolGrant,
    caller: ToolCaller,
    tool: str,
    args: dict[str, Any],
    ctx: BrokerContext,
    *,
    dry: bool,
) -> ToolDecision | None:
    """Egress resolution, credential injection, rate limits. A denial, or ``None``."""
    sub = gate.decision.subject
    if grant.egress and not dry:
        try:
            from examlops.dataplane.safety import check_address
            from examlops.dataplane.types import EgressDenied
        except Exception as exc:  # noqa: BLE001 - cannot vouch for a destination
            return ToolDecision(
                "deny", "egress_unavailable", f"egress check unavailable: {exc}", sub, grant
            )
        for name in grant.egress.get("url_args", []):
            host = url_host(args.get(name))
            if not host:
                continue
            try:
                kwargs = {"resolver": ctx.resolver} if ctx.resolver is not None else {}
                check_address(host, 443, **kwargs)
            except EgressDenied:
                return ToolDecision(
                    "deny",
                    "egress_denied",
                    f"args.{name}: destination refused by egress policy",
                    sub,
                    grant,
                )
    if grant.credentials:
        stop = _credentials(gate, grant, tool, ctx, dry=dry)
        if stop is not None:
            return stop
    limits: list[tuple[str, int, int]] = []
    now = ctx.now
    import time as _time

    t = _time.time() if now is None else now
    if grant.max_calls_per_minute:
        limits.append(("minute", int(t // 60), grant.max_calls_per_minute))
    if grant.max_calls_per_session:
        if not caller.session:
            return ToolDecision(
                "deny",
                "session_required",
                "this grant limits calls per session but the caller has no session id",
                sub,
                grant,
            )
        limits.append((f"session:{caller.session}", 0, grant.max_calls_per_session))
    if not limits:
        return None
    try:
        if ctx.mode == "enforce" and not dry:
            hit = _store.consume(sub or "", tool, limits, now=t)
        else:  # monitor / simulate: look, never count
            hit = next(
                (s for s, w, cap in limits if _store.counter(sub or "", tool, s, w) >= cap), None
            )
    except Exception as exc:  # noqa: BLE001 - a limit that cannot be counted cannot be honoured
        return ToolDecision(
            "deny", "rate_limit_unavailable", f"rate counter unavailable: {exc}", sub, grant
        )
    if hit is not None:
        kind = "per minute" if hit == "minute" else "per session"
        return ToolDecision(
            "deny", "rate_limited", f"call limit {kind} reached for {tool}", sub, grant
        )
    return None


def _credentials(
    gate: _Gate, grant: ToolGrant, tool: str, ctx: BrokerContext, *, dry: bool
) -> ToolDecision | None:
    sub = gate.decision.subject
    spec = ctx.spec(tool)
    params: Mapping[str, inspect.Parameter]
    try:
        params = inspect.signature(spec.fn).parameters
    except (TypeError, ValueError):
        params = {}
    takes_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    for name in grant.credentials:
        if name not in params and not takes_kwargs:
            return ToolDecision(
                "deny",
                "credential_param_unknown",
                f"{tool} has no parameter {name!r} to inject a credential into",
                sub,
                grant,
            )
    if dry:
        return None
    from examlops import secrets

    for name, path in grant.credentials.items():
        try:
            gate.creds[name] = secrets.get_secret(path, tenant=ctx.tenant, actor=sub)
        except Exception as exc:  # noqa: BLE001 - NotFound / AccessDenied / vault down alike
            gate.creds.clear()
            return ToolDecision(
                "deny",
                "credential_unavailable",
                f"credential for {name!r} could not be read ({type(exc).__name__})",
                sub,
                grant,
            )
    return None


# ── audit ─────────────────────────────────────────────────────────────────────


def _audit_decision(
    caller: ToolCaller, tool: str, args: Mapping[str, Any], gate: _Gate, secrets: tuple[str, ...]
) -> None:
    d = gate.decision
    details: dict[str, Any] = {
        "agent": caller.agent,
        "version_id": caller.version_id,
        "grant_subject": d.subject,
        "on_behalf_of": caller.on_behalf_of,
        "session": caller.session,
        "correlation_id": caller.correlation_id,
        "tool": tool,
        "tier": gate.tier,
        "effect": d.effect,
        "code": d.code,
        "reason": d.reason,
        "problems": list(d.problems),
        "enforced": gate.enforced,
        "approved": gate.approved,
        "args": redact_args(args, secrets),
    }
    audit_best_effort(
        _SOURCE,
        caller.on_behalf_of or caller.agent,
        f"tool_broker:{d.effect}",
        f"{caller.agent}:{tool}",
        details,
    )


def _envelope(gate: _Gate, tool: str) -> dict[str, Any]:
    d = gate.decision
    out: dict[str, Any] = {
        "ok": False,
        "error": f"tool broker: {d.reason}",
        "code": "approval_required" if d.effect == "require_approval" else d.code,
        "broker": {"effect": d.effect, "reason_code": d.code, "tool": tool},
    }
    if d.problems:
        out["problems"] = list(d.problems)
    return out


# ── public API ────────────────────────────────────────────────────────────────


def invoke(
    caller: ToolCaller, tool: str, args: Mapping[str, Any] | None, ctx: BrokerContext | None = None
) -> dict[str, Any]:
    """Run ``tool`` for ``caller`` through the broker; see the module docstring."""
    ctx = ctx or BrokerContext()
    call_args = dict(args or {})
    gate = _gate(caller, tool, call_args, ctx, dry=False)
    secrets = tuple(gate.creds.values())
    _audit_decision(caller, tool, call_args, gate, secrets)
    if gate.blocked:
        return _envelope(gate, tool)
    spec = ctx.spec(tool)
    if spec is None:  # monitor mode on an unknown tool: nothing to run
        return _envelope(gate, tool)
    try:
        out = spec.fn(**call_args, **gate.creds)
    except TypeError as exc:
        return _scrub(
            {"ok": False, "error": f"{tool}: bad arguments ({exc})", "code": "bad_arguments"},
            secrets,
        )
    except Exception as exc:  # noqa: BLE001 - a tool failing is an answer, not a crash
        return _scrub(
            {
                "ok": False,
                "error": f"{tool} failed: {type(exc).__name__}: {exc}",
                "code": "tool_error",
            },
            secrets,
        )
    return _scrub(out, secrets)


def simulate(
    caller: ToolCaller, tool: str, args: Mapping[str, Any] | None, ctx: BrokerContext | None = None
) -> dict[str, Any]:
    """What ``invoke`` would decide, without running the tool, counting quota or reading a secret."""
    ctx = ctx or BrokerContext()
    gate = _gate(caller, tool, dict(args or {}), ctx, dry=True)
    d = gate.decision
    grant = d.grant
    return {
        "ok": True,
        "effect": d.effect,
        "code": d.code,
        "reason": d.reason,
        "problems": list(d.problems),
        "grant_subject": d.subject,
        "tool": tool,
        "tier": gate.tier,
        "limits": (
            {
                "max_calls_per_minute": grant.max_calls_per_minute,
                "max_calls_per_session": grant.max_calls_per_session,
            }
            if grant
            else {}
        ),
        "injects_credentials": sorted(grant.credentials) if grant else [],
        "args": redact_args(args or {}),
        "note": "simulation: no tool ran, no quota was counted, no secret was read",
    }
