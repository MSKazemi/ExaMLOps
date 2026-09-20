"""Project-level enforcement points (ADR 0014 decision 4): the CLI and dashboard ask one question.

``allowed(subject, relation, project)`` is the single yes/no every ``exa project`` mutation and every
dashboard project route consults, so the two surfaces cannot disagree. It sits on
:func:`examlops.authz.check` (native table or OpenFGA) and adds only what a *platform* needs and a
relationship store cannot express:

* **Flag off = allow** (``EXAMLOPS_MULTITENANCY`` unset): single-tenant behaviour is untouched.
* **Bootstrap admins** - ``EXAMLOPS_AUTHZ_ADMINS`` (comma-separated subjects) may do anything, so a
  fresh multi-tenant deployment can create its first owners.
* **Legacy shared-password sessions** (ADR 0014 decision 5): the dashboard's password login has no
  per-user identity, so it maps to the stable subjects ``legacy:admin`` / ``legacy:operator`` /
  ``legacy:viewer`` with the migration grants *owner / editor / viewer on project ``default``* and
  nothing else. Grant them more explicitly (``exa project grant legacy:admin owner project:x``).
* **Creator becomes owner** - :func:`register_creator` grants the creating subject ``owner`` on a
  project it just created, since nobody can hold a relation on an object that did not exist.
"""

from __future__ import annotations

import logging
import os

from examlops import authz

logger = logging.getLogger(__name__)

_LEGACY = {"admin": "owner", "operator": "editor", "viewer": "viewer"}
_RANK = {"viewer": 1, "editor": 2, "owner": 3}


class ProjectAccessDenied(PermissionError):
    """The subject lacks the required relation on the project (maps to HTTP 403 / CLI exit 1)."""


def project_object(project: str) -> str:
    return f"project:{project}"


def legacy_subject(role: str) -> str:
    return f"legacy:{role}"


def platform_admins() -> set[str]:
    raw = os.getenv("EXAMLOPS_AUTHZ_ADMINS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _implied(subject: str, relation: str, project: str) -> bool:
    """The ADR 0014 migration grants for legacy password sessions (project ``default`` only)."""
    if project != "default" or not subject.startswith("legacy:"):
        return False
    held = _LEGACY.get(subject.split(":", 1)[1])
    return held is not None and _RANK[held] >= _RANK.get(relation, 99)


def allowed(subject: str, relation: str, project: str, *, actor: str | None = None) -> bool:
    if not authz.multitenancy_enabled():
        return True
    if subject in platform_admins() or _implied(subject, relation, project):
        return True
    return authz.check(subject, relation, project_object(project), actor=actor or subject)


def require(subject: str, relation: str, project: str, *, actor: str | None = None) -> None:
    """Raise :class:`ProjectAccessDenied` unless :func:`allowed`."""
    if not allowed(subject, relation, project, actor=actor):
        raise ProjectAccessDenied(f"{subject} lacks '{relation}' on project '{project}'")


def register_creator(subject: str, project: str) -> bool:
    """Make ``subject`` owner of a project it just created (multi-tenancy on, real subject only)."""
    if not authz.multitenancy_enabled() or subject.startswith("legacy:"):
        return False
    authz.grant(subject, "owner", project_object(project), actor=subject)
    return True


def project_of(obj: str) -> str | None:
    """The project a hierarchical object string lives under, or ``None`` (``platform:core`` ...)."""
    root = obj.split("/", 1)[0]
    return root.partition(":")[2] if root.startswith("project:") else None
