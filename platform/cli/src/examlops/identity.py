"""Agent identity: principals, scoped grants, JIT leases (ADR 0108, AIDC W3, spec §1).

Every action becomes attributable to a **principal** with a **scope** and an **expiry**, and
autonomous action is distinguishable from delegated action **by credential, not by flag**: an
agent acting AUTONOMOUS and the same agent acting DELEGATED for a user hold *different grants
and therefore different lease ids* (per IETF WIMSE, which classes AI agents as delegated
workloads and requires the distinction be expressible in the identity itself).

The check path is the governance hot path, so it is cheap by construction: **expiry is evaluated
at check time** — no background sweeper exists, and a lease one second past its TTL is denied by
the very next ``check()``. Every denial emits an ``authz_denied`` security event carrying the
principal, the requested operation and the grant id.

Degradation: no external IdP is required. SPIFFE/WIMSE integration is a provider seam
(``identity_provider`` domain); absent one, identity is platform-local and every principal is
labelled ``issuer=local`` so nobody mistakes it for federated identity.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from examlops.data import get_db, init_db

Mode = Literal["AUTONOMOUS", "DELEGATED"]
_MODES = ("AUTONOMOUS", "DELEGATED")
_OPERATIONS = ("read", "write", "export", "admin")

_TS = "%Y-%m-%d %H:%M:%S"


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(dt: datetime) -> str:
    return dt.strftime(_TS)


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        return datetime.strptime(expires_at, _TS).replace(tzinfo=UTC) <= _now()
    except ValueError:
        return True  # unreadable expiry fails closed


@dataclass(frozen=True)
class AgentPrincipal:
    agent_id: str
    name: str
    owner: str
    purpose: str
    parent: str | None
    state: str
    issuer: str = "local"


@dataclass(frozen=True)
class Grant:
    grant_id: str
    principal: str
    resource_scope: dict[str, Any]
    data_scope: dict[str, Any]
    operation_scope: tuple[str, ...]
    mode: str
    on_behalf_of: str | None
    expires_at: str


@dataclass(frozen=True)
class Lease:
    lease_id: str
    grant_id: str
    target: str
    expires_at: str


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    principal: str | None = None
    mode: str | None = None


@dataclass(frozen=True)
class Attribution:
    principal: str
    mode: str
    on_behalf_of: str | None
    chain: tuple[str, ...] = field(default_factory=tuple)
    issuer: str = "local"


def _audit(action: str, target: str | None, details: dict[str, Any]) -> None:
    from examlops.data.audit import write_audit_event

    write_audit_event("identity", None, action, target, details)


def register_agent(
    name: str, owner: str, purpose: str, parent: str | None = None
) -> AgentPrincipal:
    """Register an agent principal (platform-local issuer)."""
    init_db()
    agent_id = f"agent-{uuid.uuid4().hex[:12]}"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_principals (agent_id, name, owner, purpose, parent) "
            "VALUES (?,?,?,?,?)",
            (agent_id, name, owner, purpose, parent),
        )
    _audit("agent_registered", agent_id, {"name": name, "owner": owner, "parent": parent})
    return AgentPrincipal(agent_id, name, owner, purpose, parent, "ACTIVE")


def decommission_agent(agent_id: str, *, reason: str) -> None:
    """Fast shutdown: mark the principal decommissioned and revoke every lease it holds."""
    init_db()
    now = _ts(_now())
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_principals SET state='DECOMMISSIONED', decommissioned_at=? "
            "WHERE agent_id=?",
            (now, agent_id),
        )
        conn.execute(
            """UPDATE identity_leases SET revoked_at=?, revoke_reason=?
               WHERE revoked_at IS NULL AND grant_id IN
                     (SELECT grant_id FROM identity_grants WHERE principal=?)""",
            (now, f"agent decommissioned: {reason}", agent_id),
        )
        conn.execute(
            "UPDATE identity_grants SET revoked_at=? WHERE revoked_at IS NULL AND principal=?",
            (now, agent_id),
        )
    _audit("agent_decommissioned", agent_id, {"reason": reason})


def grant(
    principal: str,
    *,
    resource: dict[str, Any],
    data: dict[str, Any],
    operation: set[str] | tuple[str, ...] | list[str],
    ttl_s: int,
    mode: Mode,
    on_behalf_of: str | None = None,
) -> Grant:
    """Issue a scoped grant to a principal.

    DELEGATED requires ``on_behalf_of`` (the human or system the agent acts for);
    AUTONOMOUS forbids it — the two are different credentials, and conflating them is
    exactly what this module exists to prevent.
    """
    init_db()
    if mode not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")
    ops = tuple(sorted(set(operation)))
    unknown = [o for o in ops if o not in _OPERATIONS]
    if unknown:
        raise ValueError(f"unknown operations {unknown}; allowed: {_OPERATIONS}")
    if mode == "DELEGATED" and not on_behalf_of:
        raise ValueError("DELEGATED grants require on_behalf_of")
    if mode == "AUTONOMOUS" and on_behalf_of:
        raise ValueError("AUTONOMOUS grants must not carry on_behalf_of")
    with get_db() as conn:
        row = conn.execute(
            "SELECT state FROM agent_principals WHERE agent_id=?", (principal,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown principal {principal!r}")
        if row["state"] != "ACTIVE":
            raise ValueError(f"principal {principal!r} is {row['state']}")
        grant_id = f"grant-{uuid.uuid4().hex[:12]}"
        expires = _ts(_now() + timedelta(seconds=ttl_s))
        conn.execute(
            """INSERT INTO identity_grants
               (grant_id, principal, resource_scope, data_scope, operation_scope, mode,
                on_behalf_of, expires_at) VALUES (?,?,?,?,?,?,?,?)""",
            (
                grant_id,
                principal,
                json.dumps(resource),
                json.dumps(data),
                json.dumps(ops),
                mode,
                on_behalf_of,
                expires,
            ),
        )
    _audit(
        "grant_issued",
        principal,
        {"grant_id": grant_id, "mode": mode, "operations": list(ops), "ttl_s": ttl_s},
    )
    return Grant(grant_id, principal, dict(resource), dict(data), ops, mode, on_behalf_of, expires)


def lease(grant_id: str, *, target: str, ttl_s: int) -> Lease:
    """Vend a JIT, single-target, auto-expiring lease under a grant."""
    init_db()
    with get_db() as conn:
        g = conn.execute("SELECT * FROM identity_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if g is None:
            raise ValueError(f"unknown grant {grant_id!r}")
        if g["revoked_at"] or _expired(g["expires_at"]):
            raise ValueError(f"grant {grant_id!r} is revoked or expired")
        lease_id = f"lease-{uuid.uuid4().hex[:12]}"
        # The lease never outlives its grant.
        grant_exp = datetime.strptime(g["expires_at"], _TS).replace(tzinfo=UTC)
        expires = _ts(min(_now() + timedelta(seconds=ttl_s), grant_exp))
        conn.execute(
            "INSERT INTO identity_leases (lease_id, grant_id, target, expires_at) VALUES (?,?,?,?)",
            (lease_id, grant_id, target, expires),
        )
    _audit(
        "lease_issued",
        g["principal"],
        {"lease_id": lease_id, "grant_id": grant_id, "target": target},
    )
    return Lease(lease_id, grant_id, target, expires)


def revoke(lease_id: str, *, reason: str) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            "UPDATE identity_leases SET revoked_at=?, revoke_reason=? "
            "WHERE lease_id=? AND revoked_at IS NULL",
            (_ts(_now()), reason, lease_id),
        )
    _audit("lease_revoked", None, {"lease_id": lease_id, "reason": reason})


def _load_lease_grant(conn: Any, lease_id: str) -> tuple[Any, Any]:
    lr = conn.execute("SELECT * FROM identity_leases WHERE lease_id=?", (lease_id,)).fetchone()
    gr = (
        conn.execute("SELECT * FROM identity_grants WHERE grant_id=?", (lr["grant_id"],)).fetchone()
        if lr is not None
        else None
    )
    return lr, gr


def check(lease_id: str, *, action: str, target: str) -> Decision:
    """The per-hop authorization check — brokers call this on EVERY hop.

    ``action`` names the requested operation (``read``/``write``/``export``/``admin``); the
    lease must be live, its grant live, the operation within the grant's operation scope, and
    the target the one the lease was vended for. Every denial emits an ``authz_denied``
    security event; expiry is evaluated here, at check time, never by a sweeper.
    """
    init_db()

    def _deny(reason: str, principal: str | None = None, grant_id: str | None = None) -> Decision:
        _audit(
            "authz_denied",
            principal,
            {
                "lease_id": lease_id,
                "grant_id": grant_id,
                "operation": action,
                "target": target,
                "reason": reason,
            },
        )
        return Decision(False, reason, principal)

    with get_db() as conn:
        lr, gr = _load_lease_grant(conn, lease_id)
        principal_row = (
            conn.execute(
                "SELECT state FROM agent_principals WHERE agent_id=?", (gr["principal"],)
            ).fetchone()
            if gr is not None
            else None
        )
    if lr is None:
        return _deny("unknown lease")
    if gr is None:
        return _deny("lease has no grant")
    if lr["revoked_at"]:
        return _deny(f"lease revoked: {lr['revoke_reason']}", gr["principal"], gr["grant_id"])
    if _expired(lr["expires_at"]):
        return _deny("lease expired", gr["principal"], gr["grant_id"])
    if gr["revoked_at"] or _expired(gr["expires_at"]):
        return _deny("grant revoked or expired", gr["principal"], gr["grant_id"])
    if principal_row is None or principal_row["state"] != "ACTIVE":
        return _deny("principal not active", gr["principal"], gr["grant_id"])
    ops = tuple(json.loads(gr["operation_scope"]))
    if action not in ops:
        return _deny(
            f"operation {action!r} outside grant scope {list(ops)}",
            gr["principal"],
            gr["grant_id"],
        )
    if target != lr["target"]:
        return _deny(
            f"target {target!r} is not this lease's target {lr['target']!r}",
            gr["principal"],
            gr["grant_id"],
        )
    resource = json.loads(gr["resource_scope"])
    ids = resource.get("ids", "*")
    if ids != "*" and target not in ids:
        return _deny(f"target {target!r} outside resource scope", gr["principal"], gr["grant_id"])
    return Decision(True, "", gr["principal"], gr["mode"])


def whoami(lease_id: str) -> Attribution | None:
    """Attribution for a lease: principal, mode, on_behalf_of, and the parent chain."""
    init_db()
    with get_db() as conn:
        lr, gr = _load_lease_grant(conn, lease_id)
        if lr is None or gr is None:
            return None
        chain: list[str] = []
        current: str | None = gr["principal"]
        while current:
            row = conn.execute(
                "SELECT agent_id, parent, issuer FROM agent_principals WHERE agent_id=?",
                (current,),
            ).fetchone()
            if row is None:
                break
            chain.append(row["agent_id"])
            current = row["parent"]
        issuer = "local"
        if chain:
            row = conn.execute(
                "SELECT issuer FROM agent_principals WHERE agent_id=?", (chain[0],)
            ).fetchone()
            issuer = row["issuer"] if row else "local"
    return Attribution(gr["principal"], gr["mode"], gr["on_behalf_of"], tuple(chain), issuer)
