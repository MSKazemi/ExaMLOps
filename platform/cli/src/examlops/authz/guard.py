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


# ── resource-level enforcement (ADR 0014 decision 4, beyond the project paths) ──────────────────
#
# Every surface that mutates a *model* - the control plane's retrain / approval / change routes and
# the CLI's promote / traffic / auto-retrain / training commands - and every ``exa data`` command
# that records or reads a *dataset*'s revisions asks the same question through
# :func:`resource_allowed`, so a subject that may not edit project ``acme`` cannot retrain, approve
# or promote ``acme``'s models, or snapshot ``acme``'s datasets, through any of those doors.

DEFAULT_PROJECT = "default"

#: Resource kinds whose project is resolved through ``project_resources`` (plus the legacy
#: ``project_models`` table for models). ``scope_audit`` accepts the same kinds as a join key.
RESOURCE_KINDS = ("model", "dataset")

# Platform roles an IdP may assert for a project (ADR 0120) -> the D6 relation they carry.
_IDP_ROLE_RELATION = {"viewer": "viewer", "operator": "editor", "admin": "owner"}


class ModelScopeUnavailable(ProjectAccessDenied):
    """The resource's owning project(s) could not be read: refuse rather than guess (fail closed).

    Named for its first use; raised for every resource kind.
    """


def resource_projects(kind: str, ref: str) -> list[str]:
    """Every project holding ``kind``/``ref`` (case-insensitive), sorted; ``["default"]`` if none.

    ADR 0014 decision 5: a resource no project has claimed belongs to the ``default`` project, so
    the migration grants cover it and a fresh tenant reaches nothing it was not given. Raises
    :class:`ModelScopeUnavailable` when the membership tables cannot be read, so a datastore outage
    can never turn a scoped resource into an open one. Case-insensitive because operators assign
    the registry spelling (``JPCP``) while callers often use the MLflow one (``jpcp``).
    """
    if kind not in RESOURCE_KINDS:
        raise ValueError(f"unsupported resource kind {kind!r}; expected one of {RESOURCE_KINDS}")
    sql = "SELECT project FROM project_resources WHERE kind=? AND lower(ref)=lower(?)"
    params: tuple[str, ...] = (kind, ref)
    if kind == "model":
        # `namespace_models` is the legacy grouping surface; ADR 0086 made a namespace an alias of
        # the project of the same name, and budgets already attribute its models to that project
        # (`get_project_consumption`). Leaving it out here would let a model its project pays for
        # fall back to `default`, reachable by every `default` editor. The `default` namespace is
        # the column's own default, not a claim: it must not add `default` as a second owner of a
        # model a real project holds (which would refuse that project's editors).
        sql += (
            " UNION SELECT project FROM project_models WHERE lower(model)=lower(?)"
            " UNION SELECT namespace FROM namespace_models"
            " WHERE lower(model)=lower(?) AND namespace <> ?"
        )
        params = (kind, ref, ref, ref, DEFAULT_PROJECT)
    try:
        from examlops.data import get_db, init_db

        init_db()
        with get_db() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001 - reported as a refusal, never as an allow
        raise ModelScopeUnavailable(
            f"cannot resolve the project of {kind} '{ref}': {type(exc).__name__}: {exc}"
        ) from exc
    projects = sorted({str(r[0]) for r in rows if r[0]})
    return projects or [DEFAULT_PROJECT]


def model_projects(model: str) -> list[str]:
    """:func:`resource_projects` for a model."""
    return resource_projects("model", model)


def _asserted(asserted_projects: dict[str, str] | None, project: str, relation: str) -> bool:
    """A project role the verified IdP token itself asserts (``operator`` counts as ``editor``)."""
    held = (asserted_projects or {}).get(project)
    if not held:
        return False
    rel = _IDP_ROLE_RELATION.get(held, held)
    return _RANK.get(rel, 0) >= _RANK.get(relation, 99)


def resource_allowed(
    subject: str,
    relation: str,
    kind: str,
    ref: str,
    *,
    actor: str | None = None,
    asserted_projects: dict[str, str] | None = None,
) -> bool:
    """True iff ``subject`` holds ``relation`` on **every** project that holds ``kind``/``ref``.

    Every, not any: a resource shared by two projects is changed for both, so both must consent,
    and the ambiguity resolves to a refusal. Per project a subject passes as a platform admin
    (``EXAMLOPS_AUTHZ_ADMINS``), through the legacy migration grants (``default`` only), through a
    project role asserted by a *verified* IdP token (``asserted_projects``), or through a D6
    relation on ``project:<p>`` or on ``project:<p>/<kind>:<ref>`` itself
    (:func:`examlops.authz.check` walks the parent, and audits every denial). Flag off = allow. A
    membership lookup that fails raises :class:`ModelScopeUnavailable` (audited ``authz_error``);
    callers map it to a refusal.
    """
    if not authz.multitenancy_enabled():
        return True
    if subject in platform_admins():
        return True
    who = actor or subject
    try:
        projects = resource_projects(kind, ref)
    except ModelScopeUnavailable as exc:
        logger.error(
            "%s authz failed closed for %s '%s' on %s: %s", kind, subject, relation, ref, exc
        )
        authz._audit("authz_error", subject, relation, f"{kind}:{ref}", who)
        raise
    for project in projects:
        if _implied(subject, relation, project) or _asserted(asserted_projects, project, relation):
            continue
        if not authz.check(subject, relation, f"{project_object(project)}/{kind}:{ref}", actor=who):
            return False
    return True


def model_allowed(
    subject: str,
    relation: str,
    model: str,
    *,
    actor: str | None = None,
    asserted_projects: dict[str, str] | None = None,
) -> bool:
    """:func:`resource_allowed` for a model."""
    return resource_allowed(
        subject, relation, "model", model, actor=actor, asserted_projects=asserted_projects
    )


def require_resource(
    subject: str,
    relation: str,
    kind: str,
    ref: str,
    *,
    actor: str | None = None,
    asserted_projects: dict[str, str] | None = None,
) -> None:
    """Raise :class:`ProjectAccessDenied` (or :class:`ModelScopeUnavailable`) unless allowed."""
    if resource_allowed(
        subject, relation, kind, ref, actor=actor, asserted_projects=asserted_projects
    ):
        return
    try:
        where = ", ".join(resource_projects(kind, ref))
    except ModelScopeUnavailable:  # pragma: no cover - it was readable a moment ago
        where = "?"
    raise ProjectAccessDenied(f"{subject} lacks '{relation}' on {kind} '{ref}' (project: {where})")


def require_model(
    subject: str,
    relation: str,
    model: str,
    *,
    actor: str | None = None,
    asserted_projects: dict[str, str] | None = None,
) -> None:
    """:func:`require_resource` for a model."""
    require_resource(
        subject, relation, "model", model, actor=actor, asserted_projects=asserted_projects
    )
