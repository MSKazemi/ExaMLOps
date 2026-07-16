"""D6 — Fine-grained RBAC & multi-tenancy authz client (ADR 0014, spec D6).

A relationship model (``owner ⊇ editor ⊇ viewer``) over objects, checked with a
**default-deny** policy (spec R3). Backed by ``platform_db.authz_relations`` (an
OpenFGA backend can be swapped behind :func:`check` later).

**Feature-flagged (spec R9):** unless ``EXAMLOPS_MULTITENANCY`` is truthy, :func:`check`
returns True — single-tenant behaviour is unchanged.

Objects are hierarchical strings (``project:acme/model:JPCP``): a relation on the
parent ``project:acme`` grants the same relation on its children (spec R6).
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}

# Relation implication: a higher rank satisfies any lower-or-equal required relation.
_RANK = {"viewer": 1, "editor": 2, "owner": 3}


def multitenancy_enabled() -> bool:
    return os.getenv("EXAMLOPS_MULTITENANCY", "").strip().lower() in _TRUTHY


def _rank(relation: str) -> int:
    return _RANK.get(relation, 0)


def _parent(obj: str) -> str | None:
    """The parent object (``project:a/model:m`` → ``project:a``), or None."""
    if "/" in obj:
        return obj.rsplit("/", 1)[0]
    return None


def _best_rank_on(subject: str, obj: str) -> int:
    from examlops.platform_db import get_relations_for

    rels = get_relations_for(subject, obj)
    return max((_rank(r) for r in rels), default=0)


def check(subject: str, relation: str, obj: str, *, actor: str | None = None) -> bool:
    """Return True iff ``subject`` holds ``relation`` (or stronger) on ``obj`` (spec R3).

    Default-deny. Walks parent objects so a project-level grant covers its children.
    When multi-tenancy is disabled, always allows (single-tenant compat, R9).
    """
    if not multitenancy_enabled():
        return True
    required = _rank(relation)
    node: str | None = obj
    allowed = False
    while node is not None:
        if _best_rank_on(subject, node) >= required:
            allowed = True
            break
        node = _parent(node)
    if not allowed:
        _audit("authz_deny", subject, relation, obj, actor)
    return allowed


def require(subject: str, relation: str, obj: str, *, actor: str | None = None) -> None:
    """Raise ``PermissionError`` if ``check`` denies (for enforcement points)."""
    if not check(subject, relation, obj, actor=actor):
        raise PermissionError(f"{subject} lacks '{relation}' on '{obj}'")


def grant(subject: str, relation: str, obj: str, *, actor: str | None = None) -> None:
    """Grant a relation (audited, spec: grant)."""
    from examlops.platform_db import grant_relation

    grant_relation(subject, relation, obj, actor=actor)
    _audit("authz_grant", subject, relation, obj, actor)


def revoke(subject: str, relation: str, obj: str, *, actor: str | None = None) -> int:
    from examlops.platform_db import revoke_relation

    n = revoke_relation(subject, relation, obj)
    _audit("authz_revoke", subject, relation, obj, actor)
    return n


def list_objects(subject: str, relation: str | None = None) -> list[dict]:
    """Objects (and relations) granted to ``subject``, optionally filtered by relation."""
    from examlops.platform_db import list_objects_for

    rows = list_objects_for(subject)
    if relation:
        want = _rank(relation)
        rows = [r for r in rows if _rank(r["relation"]) >= want]
    return rows


def _audit(action: str, subject: str, relation: str, obj: str, actor: str | None) -> None:
    try:
        from examlops.platform_db import write_audit_event

        write_audit_event(
            "exa-authz", actor, action, obj, {"subject": subject, "relation": relation}
        )
    except Exception:
        pass
