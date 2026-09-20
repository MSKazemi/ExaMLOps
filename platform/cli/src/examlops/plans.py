"""Plan / apply for agent principals (ADR 0147 decision 2).

An agent principal (``EXAMLOPS_PRINCIPAL_KIND=agent``) never gets implicit consent. Instead of
being refused outright it is handed something to approve:

1. ``plan_change(tool, args)`` computes a **plan document** *without mutating anything* — the
   tool's own policy gate is run in *probe* mode (it records the decision and stops before the
   tool body), the current state the change depends on is snapshotted as **preconditions**, and
   the plan is stored under ``plan_hash = sha256(canonical JSON of tool + args + preconditions)``
   with an expiry (``EXAMLOPS_PLAN_TTL``, default 15 minutes).
2. ``apply_plan(plan_hash, approval_token?)`` **atomically claims** the plan (one conditional
   UPDATE — of N concurrent applies exactly one proceeds), re-checks the preconditions and the
   policy, and executes exactly the planned call. A plan that expired, was already applied, whose
   world changed, or that needs a human approval nobody gave, is refused and nothing else runs.
3. A direct call of a mutating tool by an agent principal is refused with ``plan_required``;
   humans and every read tool are unaffected.

Approval: when policy says a change ``require_approval``, the plan lists it under
``required_approvals`` and a **human** mints a one-time token with :func:`approve_plan` (refused
for an agent principal). Only the token's hash is stored. Stated limit (same as ADR 0147 d2): an
agent that runs with a human's environment is indistinguishable from that human.

This module has no import-time dependency on :mod:`examlops.mcp.tools` (which imports it).
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import hmac
import inspect
import json
import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

__all__ = [
    "DEFAULT_TTL_S",
    "PlanProbe",
    "apply_plan",
    "approval_active",
    "approve_plan",
    "get_plan",
    "list_plans",
    "plan_change",
    "plan_gated",
    "plannable_tools",
    "probing",
]

DEFAULT_TTL_S = 900.0
#: Tools that manage plans themselves; they are never plannable and never plan-gated.
PLAN_TOOLS = frozenset({"plan_change", "apply_plan", "approve_plan"})

_MODE: contextvars.ContextVar[str | None] = contextvars.ContextVar("plan_mode", default=None)
_PROBE: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "plan_probe", default=None
)
_APPROVED: contextvars.ContextVar[bool] = contextvars.ContextVar("plan_approved", default=False)


class PlanProbe(BaseException):  # noqa: N818 - control flow, deliberately not an Exception
    """Raised by the agent write gate in probe mode, after it recorded its decision.

    A ``BaseException`` so a tool's ``except Exception`` can never swallow it and run on.
    """


def ttl_seconds() -> float:
    try:
        value = float(os.getenv("EXAMLOPS_PLAN_TTL", ""))
    except ValueError:
        return DEFAULT_TTL_S
    return value if value > 0 else DEFAULT_TTL_S


def _principal_kind() -> str:
    return (
        "agent" if os.getenv("EXAMLOPS_PRINCIPAL_KIND", "").strip().lower() == "agent" else "human"
    )


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "mcp-agent"


def _err(message: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "code": code, **extra}


# ── context the tool layer consults ───────────────────────────────────────────


def approval_active() -> bool:
    """True inside an :func:`apply_plan` whose human approval token verified."""
    return _APPROVED.get()


@contextmanager
def probing() -> Iterator[list[dict[str, Any]]]:
    """Run tools in probe mode: the write gate records its decision and raises ``PlanProbe``."""
    sink: list[dict[str, Any]] = []
    t_mode, t_probe = _MODE.set("plan"), _PROBE.set(sink)
    try:
        yield sink
    finally:
        _MODE.reset(t_mode)
        _PROBE.reset(t_probe)


def record_probe(entry: dict[str, Any]) -> None:
    """Called by the write gate: hand the decision to the active probe and stop the tool."""
    sink = _PROBE.get()
    if sink is not None:
        sink.append(entry)
        raise PlanProbe()


def plan_gated(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Refuse a direct call of a mutating tool by an agent principal (``plan_required``).

    Plan/apply set a context mode first, which lets the call through; humans are never gated.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if _MODE.get() is None and _principal_kind() == "agent":
            return _err(
                f"plan_required: {fn.__name__} is a mutation; an agent principal must "
                "plan_change() it and apply_plan() the returned plan_hash",
                "plan_required",
                tool=fn.__name__,
            )
        return fn(*args, **kwargs)

    return wrapper


# ── canonical hashing ─────────────────────────────────────────────────────────

_VOLATILE = frozenset({"updated_at", "updated_by", "created_at", "ts", "last_seen", "timestamp"})


def _stable(value: Any) -> Any:
    """Drop clock-derived keys so a re-read of an unchanged world hashes identically."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in sorted(value.items()) if k not in _VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(tool: str, args: dict[str, Any], preconditions: dict[str, Any]) -> str:
    blob = canonical({"tool": tool, "args": args, "preconditions": preconditions})
    return hashlib.sha256(blob.encode()).hexdigest()


# ── per-tool descriptors ──────────────────────────────────────────────────────
# One row per mutating tool. `tests/unit/test_plan_apply.py` fails when a mutating ToolSpec has no
# row in all three tables, so a new tool cannot ship outside plan/apply.

_INTENT: dict[str, Callable[[dict[str, Any]], str]] = {
    "trigger_retrain": lambda a: (
        f"launch a training run of {a['model_name']} on dataset {a['dataset_name']}"
    ),
    "hpc_approve_cluster": lambda a: f"mark HPC cluster {a['name']} ACTIVE (schedulable)",
    "project_assign_model": lambda a: f"assign model {a['model']} to project {a['project']}",
    "project_add_member": lambda a: f"add {a['subject']} to project {a['project']} as {a['role']}",
    "dataplane_pull": lambda a: f"pull dataplane source {a['name']} and commit a snapshot",
    "set_traffic_split": lambda a: (
        f"set {a['model']} traffic to production={a['production']} canary={a['canary']}"
    ),
    "set_drift_autoretrain": lambda a: (
        f"set drift auto-retrain for {a['model']} enabled={a['enabled']} on {a['dataset']}"
    ),
    "set_promotion_rule": lambda a: (
        f"set promotion rule for {a['model']}: {a['metric']} {a['operator']} {a['threshold']}"
    ),
    "disable_challenger": lambda a: f"disable the challenger (shadow eval) for {a['model']}",
    "grant_access": lambda a: f"grant {a['subject']} {a['relation']} on {a['obj']}",
}

#: What one call can reach and how it is undone — the plan's blast radius. (ADR 0113 contracts
#: are per *autonomous behaviour*, not per tool call, so a tool-level statement lives here.)
_BLAST: dict[str, dict[str, Any]] = {
    "trigger_retrain": {
        "scope": "one model's training pipeline",
        "extent": "one flow run; consumes cluster/GPU time",
        "reversible": False,
        "rollback": "cancel the run; the model alias is not moved by a retrain alone",
    },
    "hpc_approve_cluster": {
        "scope": "one HPC cluster",
        "extent": "makes the cluster eligible for job placement",
        "reversible": True,
        "rollback": "exa hpc reject <cluster>",
    },
    "project_assign_model": {
        "scope": "one project's resource membership",
        "extent": "one model row",
        "reversible": True,
        "rollback": "unassign the model from the project",
    },
    "project_add_member": {
        "scope": "one project's access list",
        "extent": "one principal gains a role",
        "reversible": True,
        "rollback": "exa project remove-member",
    },
    "dataplane_pull": {
        "scope": "one dataplane source",
        "extent": "reads the external source, writes one snapshot revision",
        "reversible": True,
        "rollback": "snapshots are immutable and prunable; nothing is overwritten",
    },
    "set_traffic_split": {
        "scope": "live inference traffic of one model",
        "extent": "replaces the production/canary split",
        "reversible": True,
        "rollback": "set the previous split again (see preconditions)",
    },
    "set_drift_autoretrain": {
        "scope": "one model's automated retrain trigger",
        "extent": "replaces the auto-retrain config",
        "reversible": True,
        "rollback": "set the previous config again (see preconditions)",
    },
    "set_promotion_rule": {
        "scope": "one model's promotion gate",
        "extent": "replaces the metric gate",
        "reversible": True,
        "rollback": "set the previous rule again (see preconditions)",
    },
    "disable_challenger": {
        "scope": "one model's shadow evaluation",
        "extent": "turns the challenger off",
        "reversible": True,
        "rollback": "re-enable the challenger",
    },
    "grant_access": {
        "scope": "access control (tier C, human-only for autonomous agents)",
        "extent": "one relation tuple",
        "reversible": True,
        "rollback": "revoke the relation",
    },
}


def _drop_ok(resp: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in resp.items() if k != "ok"}


def _pre_traffic(t: Any, a: dict[str, Any]) -> Any:
    return t.traffic_rules(a["model"]).get("rules")


def _pre_challenger(t: Any, a: dict[str, Any]) -> Any:
    return t.challenger_config(a["model"]).get("config")


def _pre_promotion(t: Any, a: dict[str, Any]) -> Any:
    return t.promotion_rule(a["model"]).get("rule")


def _pre_drift(t: Any, a: dict[str, Any]) -> Any:
    return t.get_drift_autoretrain(a["model"]).get("config")


def _pre_project(t: Any, a: dict[str, Any]) -> Any:
    return {"project_exists": bool(t.project_detail(a["project"]).get("ok"))}


def _pre_cluster(t: Any, a: dict[str, Any]) -> Any:
    rows = t.hpc_clusters().get("clusters") or []
    row = next((r for r in rows if r.get("name") == a["name"]), None)
    return {"state": row.get("state") if row else None, "known": row is not None}


def _pre_grant(t: Any, a: dict[str, Any]) -> Any:
    return t.authz_relations(a["subject"], a["obj"]).get("relations")


def _pre_source(t: Any, a: dict[str, Any]) -> Any:
    rows = t.dataplane_sources(a.get("project") or "").get("sources") or []
    return next((r for r in rows if r.get("name") == a["name"]), None)


def _pre_retrain(t: Any, a: dict[str, Any]) -> Any:
    return {"control_plane_configured": bool(t._cfg().control_plane_token)}


_PRECONDITIONS: dict[str, Callable[[Any, dict[str, Any]], Any]] = {
    "trigger_retrain": _pre_retrain,
    "hpc_approve_cluster": _pre_cluster,
    "project_assign_model": _pre_project,
    "project_add_member": _pre_project,
    "dataplane_pull": _pre_source,
    "set_traffic_split": _pre_traffic,
    "set_drift_autoretrain": _pre_drift,
    "set_promotion_rule": _pre_promotion,
    "disable_challenger": _pre_challenger,
    "grant_access": _pre_grant,
}


def _tools() -> Any:
    from examlops.mcp import tools

    return tools


def plannable_tools() -> dict[str, Any]:
    """Every mutating :class:`ToolSpec` except the plan tools themselves, by name."""
    return {s.name: s for s in _tools().REGISTRY if s.mutating and s.name not in PLAN_TOOLS}


def _snapshot(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    fn = _PRECONDITIONS.get(tool)
    if fn is None:
        raise KeyError(f"no precondition snapshot registered for {tool}")
    return {"state": _stable(fn(_tools(), args))}


def _normalise(spec: Any, args: dict[str, Any]) -> dict[str, Any]:
    sig = inspect.signature(spec.fn)
    given = {k: v for k, v in (args or {}).items() if k != "idempotency_key"}
    bound = sig.bind(**given)
    bound.apply_defaults()
    return {k: v for k, v in bound.arguments.items() if k != "idempotency_key"}


def _probe(spec: Any, args: dict[str, Any]) -> dict[str, Any] | None:
    """Run the tool's own write gate without its body. ``None`` if the gate was never reached."""
    with probing() as sink:
        try:
            spec.fn(**args)
        except PlanProbe:
            pass
    return sink[0] if sink else None


# ── audit ─────────────────────────────────────────────────────────────────────


def _audit(action: str, plan_hash: str, details: dict[str, Any]) -> str | None:
    return _tools()._audit_write(action, plan_hash, details)


# ── plan ──────────────────────────────────────────────────────────────────────


def plan_change(tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute and store a plan for one mutating tool call, without performing it."""
    plannable = plannable_tools()
    spec = plannable.get(tool)
    if spec is None:
        return _err(
            f"{tool!r} is not a plannable mutating tool",
            "not_plannable",
            plannable=sorted(plannable),
        )
    if spec.tier == "C" and _principal_kind() == "agent":
        return _err(f"{tool} is tier C (human-only); an agent cannot plan it", "tier_c_human_only")
    try:
        norm = _normalise(spec, dict(args or {}))
    except TypeError as exc:
        return _err(f"invalid arguments for {tool}: {exc}", "invalid_args")
    gate = _probe(spec, norm)
    if gate is None:
        return _err(f"{tool} did not reach its policy gate; cannot plan it", "plan_unavailable")
    if gate["unavailable"]:
        return _err("policy unavailable; refusing to plan an agent write", "policy_unavailable")
    if gate["denied"]:
        return _err(f"policy denied {gate['action_kind']}: {gate['reason']}", "policy_denied")
    try:
        preconditions = _snapshot(tool, norm)
    except Exception as exc:  # noqa: BLE001 - never plan against state we could not read
        return _err(f"could not snapshot preconditions: {exc}", "plan_unavailable")

    plan_hash = compute_hash(tool, norm, preconditions)
    now = time.time()
    required = ["human_approval"] if gate["requires_approval"] else []
    doc = {
        "plan_hash": plan_hash,
        "tool": tool,
        "args": norm,
        "intended_change": _INTENT[tool](norm),
        "preconditions": preconditions,
        "blast_radius": {**_BLAST[tool], "tier": spec.tier, "destructive": spec.destructive},
        "policy": {"action_kind": gate["action_kind"], "reason": gate["reason"]},
        "required_approvals": required,
        "created_at": now,
        "expires_at": now + ttl_seconds(),
    }
    from examlops.data import plans as store

    try:
        store.init_db()
        outcome = store.put(plan_hash, tool, doc, _actor(), doc["expires_at"])
        stored = store.get(plan_hash)
    except Exception as exc:  # noqa: BLE001
        return _err(f"plan store unavailable: {exc}", "plan_unavailable")
    if stored is not None:
        doc = {**json.loads(stored["plan_json"]), "state": stored["state"]}
        doc["approved"] = bool(stored["approval_hash"])
    if outcome != "existing":
        _audit("plan_created", plan_hash, {"tool": tool, "args": norm, "outcome": outcome})
    return {"ok": True, "plan": doc}


# ── approve (human only) ──────────────────────────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def approve_plan(plan_hash: str) -> dict[str, Any]:
    """A human approves a plan; returns a one-time token for ``apply_plan``. Refused for agents."""
    if _principal_kind() == "agent":
        return _err("an agent principal cannot approve a plan", "approval_requires_human")
    from examlops.data import plans as store

    try:
        store.init_db()
        row = store.get(plan_hash)
    except Exception as exc:  # noqa: BLE001
        return _err(f"plan store unavailable: {exc}", "plan_unavailable")
    if row is None:
        return _err(f"unknown plan {plan_hash}", "plan_not_found")
    token = secrets.token_urlsafe(24)
    if not store.set_approval(plan_hash, _token_hash(token), _actor()):
        return _err(f"plan is {row['state']} or expired; approve a live plan", "plan_not_live")
    _audit("plan_approved", plan_hash, {"approved_by": _actor()})
    return {"ok": True, "plan_hash": plan_hash, "approval_token": token, "approved_by": _actor()}


# ── apply ─────────────────────────────────────────────────────────────────────


def _refuse(plan_hash: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    _audit("plan_apply_refused", plan_hash, {"code": code, "reason": message})
    return _err(message, code, plan_hash=plan_hash, **extra)


def apply_plan(plan_hash: str, approval_token: str | None = None) -> dict[str, Any]:
    """Execute exactly the stored plan once, after re-checking expiry, policy and preconditions."""
    from examlops.data import plans as store

    try:
        store.init_db()
        row = store.get(plan_hash)
    except Exception as exc:  # noqa: BLE001
        return _err(f"plan store unavailable: {exc}", "plan_unavailable")
    if row is None:
        return _refuse(plan_hash, "plan_not_found", f"unknown plan {plan_hash}")
    if row["state"] == "planned" and row["expires_at"] <= time.time():
        store.set_state(plan_hash, "expired", only_from=("planned",))
        return _refuse(plan_hash, "plan_expired", "plan expired; create a new plan")
    if row["state"] != "planned":
        code = "plan_expired" if row["state"] == "expired" else "plan_not_applicable"
        return _refuse(
            plan_hash, code, f"plan is {row['state']}; it can only be applied once, while planned"
        )

    plan = json.loads(row["plan_json"])
    tool, args = plan["tool"], plan["args"]
    spec = plannable_tools().get(tool)
    if spec is None:
        return _refuse(plan_hash, "not_plannable", f"{tool!r} is no longer a plannable tool")

    # Policy is re-evaluated now — it may have changed since the plan was written.
    gate = _probe(spec, args)
    if gate is None or gate["unavailable"]:
        return _refuse(plan_hash, "policy_unavailable", "policy unavailable; refusing to apply")
    if gate["denied"]:
        return _refuse(plan_hash, "policy_denied", f"policy now denies {tool}: {gate['reason']}")
    approved = False
    if gate["requires_approval"] or plan.get("required_approvals"):
        if not approval_token:
            return _refuse(plan_hash, "approval_required", "this plan needs a human approval_token")
        stored = row.get("approval_hash") or ""
        if not stored or not hmac.compare_digest(stored, _token_hash(approval_token)):
            return _refuse(plan_hash, "approval_invalid", "approval_token does not match")
        approved = True

    # Atomic claim — the only place concurrent applies are decided.
    if not store.claim_apply(plan_hash, _actor()):
        current = store.get(plan_hash)
        state = current["state"] if current else "missing"
        return _refuse(
            plan_hash, "plan_not_applicable", f"plan is {state}; another apply claimed it"
        )

    # Preconditions: the world must still be the one that was planned.
    try:
        now_pre = _snapshot(tool, args)
    except Exception as exc:  # noqa: BLE001
        store.finish(plan_hash, "failed", {"error": f"precondition read failed: {exc}"})
        return _refuse(plan_hash, "precondition_unreadable", f"could not re-read state: {exc}")
    if compute_hash(tool, args, now_pre) != plan_hash:
        store.finish(plan_hash, "rejected", {"error": "preconditions changed", "now": now_pre})
        return _refuse(
            plan_hash,
            "precondition_changed",
            "the state this plan depends on changed since it was planned; plan again",
            preconditions_now=now_pre,
        )

    t_mode, t_app = _MODE.set("apply"), _APPROVED.set(approved)
    try:
        result = spec.fn(**args)
    except BaseException as exc:
        store.finish(plan_hash, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        _audit("plan_applied", plan_hash, {"tool": tool, "ok": False, "error": str(exc)})
        raise
    finally:
        _MODE.reset(t_mode)
        _APPROVED.reset(t_app)
    ok = bool(isinstance(result, dict) and result.get("ok"))
    store.finish(plan_hash, "applied" if ok else "failed", result)
    _audit("plan_applied", plan_hash, {"tool": tool, "ok": ok, "approved": approved})
    return {
        "ok": ok,
        "plan_hash": plan_hash,
        "tool": tool,
        "state": "applied" if ok else "failed",
        "result": result,
    }


# ── reads ─────────────────────────────────────────────────────────────────────


def _present(row: dict[str, Any]) -> dict[str, Any]:
    doc = json.loads(row["plan_json"])
    out = {
        **doc,
        "state": row["state"],
        "actor": row["actor"],
        "approved": bool(row["approval_hash"]),
        "approved_by": row["approved_by"],
        "applied_by": row["applied_by"],
    }
    if row["result_json"]:
        out["result"] = json.loads(row["result_json"])
    return out


def get_plan(plan_hash: str) -> dict[str, Any]:
    from examlops.data import plans as store

    try:
        store.init_db()
        row = store.get(plan_hash)
    except Exception as exc:  # noqa: BLE001
        return _err(f"plan store unavailable: {exc}", "plan_unavailable")
    if row is None:
        return _err(f"unknown plan {plan_hash}", "plan_not_found")
    if row["state"] == "planned" and row["expires_at"] <= time.time():
        row = {**row, "state": "expired"}
    return {"ok": True, "plan": _present(row)}


def list_plans(state: str | None = None, limit: int = 50) -> dict[str, Any]:
    from examlops.data import plans as store

    try:
        store.init_db()
        rows = store.list_plans(state, limit)
    except Exception as exc:  # noqa: BLE001
        return _err(f"plan store unavailable: {exc}", "plan_unavailable")
    now = time.time()
    plans = []
    for r in rows:
        if r["state"] == "planned" and r["expires_at"] <= now:
            r = {**r, "state": "expired"}
        p = _present(r)
        plans.append(
            {
                "plan_hash": p["plan_hash"],
                "tool": p["tool"],
                "state": p["state"],
                "intended_change": p["intended_change"],
                "expires_at": p["expires_at"],
            }
        )
    return {"ok": True, "plans": plans}
