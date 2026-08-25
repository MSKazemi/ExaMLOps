"""Tenant/project memory scoping (X, Phase 8, ADR 0105).

Cross-cutting layer that prefixes memory namespaces with the active tenant (project) so operators
only recall memory they are authorized for, while a shared bucket holds cross-project knowledge
everyone can read. It reuses the platform's tenancy primitives — the `EXAMLOPS_PROJECT` convention
and `examlops.authz` (D6 relationship RBAC) — and is **default-off**: when
`AGENT_MEMORY_TENANT_SCOPED` is false every function collapses to the single-tenant identity, so
memory behaviour is byte-for-byte unchanged.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from skipper import config


@dataclass(frozen=True)
class MemoryIdentity:
    """Verified request owner used to partition durable memory."""

    principal: str
    tenant: str


_REQUEST_IDENTITY: ContextVar[MemoryIdentity | None] = ContextVar(
    "skipper_memory_identity", default=None
)


@contextmanager
def identity_scope(principal: str, tenant: str) -> Iterator[None]:
    """Bind a verified owner for memory operations executed in this context."""
    token = _REQUEST_IDENTITY.set(MemoryIdentity(principal=principal, tenant=tenant))
    try:
        yield
    finally:
        _REQUEST_IDENTITY.reset(token)


def request_identity() -> MemoryIdentity | None:
    return _REQUEST_IDENTITY.get()


def _owner_prefix(identity: MemoryIdentity) -> tuple[str, ...]:
    # The namespace is stable but does not disclose a principal name or tenant in exports.
    owner = hashlib.sha256(f"{identity.tenant}\0{identity.principal}".encode()).hexdigest()[:24]
    return (f"owner:{owner}",)


def active_tenant() -> str | None:
    """The tenant this session's memory is scoped to, or ``None`` (single-tenant / scoping off)."""
    identity = request_identity()
    if identity is not None:
        return identity.tenant
    if not config.AGENT_MEMORY_TENANT_SCOPED:
        return None
    return (os.getenv("EXAMLOPS_PROJECT") or "").strip() or "default"


def shared_bucket() -> str:
    return config.AGENT_MEMORY_SHARED_BUCKET


def _prefix(tenant: str) -> tuple[str, ...]:
    return (f"t:{tenant}",)


def write_prefix() -> tuple[str, ...]:
    """Namespace prefix new memories are WRITTEN under — ``()`` when scoping is off."""
    identity = request_identity()
    if identity is not None:
        return _owner_prefix(identity)
    tenant = active_tenant()
    return _prefix(tenant) if tenant else ()


def can_read(operator: str, tenant: str) -> bool:
    """Authz gate for reading a tenant's memory. Fail-open when authz/multitenancy is unavailable."""
    try:
        from examlops import authz

        if not authz.multitenancy_enabled():
            return True
        return authz.check(operator, "viewer", f"project:{tenant}")
    except Exception:  # noqa: BLE001 - single-tenant / authz absent → permit
        return True


def read_prefixes(operator: str | None = None) -> list[tuple[str, ...]]:
    """Namespace prefixes to SEARCH on recall: the operator's tenant (if authorized) + the shared
    bucket. Returns ``[()]`` when scoping is off — i.e. a single, unprefixed search (unchanged)."""
    identity = request_identity()
    if identity is not None:
        return [_owner_prefix(identity)]
    tenant = active_tenant()
    if not tenant:
        return [()]
    op = operator or config.AGENT_ACTOR
    prefixes: list[tuple[str, ...]] = []
    if can_read(op, tenant):
        prefixes.append(_prefix(tenant))
    shared = _prefix(shared_bucket())
    if shared not in prefixes:
        prefixes.append(shared)
    return prefixes
