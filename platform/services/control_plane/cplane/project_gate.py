"""Project-level authorization at the control plane's model routes (ADR 0014 decision 4).

The control plane authorized by *scope* only: a credential holding ``retrain`` could retrain any
tenant-local model, whichever project owned it, and an ``approve`` credential could approve any
model's change. With ``EXAMLOPS_MULTITENANCY`` on, every route that acts on a model now also asks
:func:`examlops.authz.guard.model_allowed` - the same question ``exa pipeline promote`` / ``serve
traffic`` / ``drift auto-retrain`` and the ``exa project`` paths ask - whether the caller holds the
needed relation on **every** project that holds that model. Flag off: nothing changes.

Who the caller is, for the relationship store:

* the shared legacy ``CONTROL_PLANE_TOKEN`` -> ``legacy:operator``, which holds only the ADR 0014
  decision 5 migration grant (``editor`` on project ``default``);
* a federated IdP token (ADR 0120) -> the token's subject, plus any project role the verified
  token itself asserts (``operator`` counts as ``editor``);
* a static or workload credential -> its configured principal (grant it with
  ``exa project add-member <p> <principal> --role editor``, or list it in ``EXAMLOPS_AUTHZ_ADMINS``).

**One table** (:data:`ROUTE_PROJECT`) classifies every mutating route: ``("model", relation)`` when
the route acts on a named model, ``("exempt", reason)`` otherwise. ``tests/test_project_gate.py``
walks the live app and fails on an unclassified route - the same forcing function as
``cplane.policy_gate.ROUTE_POLICY``.

Denied -> 403 (audited ``authz_deny`` by ``authz.check``); the membership store unreadable -> 503
(audited ``authz_error``): an outage never turns a scoped model into an open one. Every decision is
counted in ``control_plane_project_authz_decisions_total{outcome}``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

LEGACY_SUBJECT = "legacy:operator"

# (METHOD, path with any /v1 prefix removed) -> ("model", relation) | ("exempt", reason)
ROUTE_PROJECT: dict[tuple[str, str], tuple[str, str]] = {
    ("POST", "/retrain"): ("model", "editor"),
    ("POST", "/api/changes"): ("model", "editor"),
    ("POST", "/changes"): ("model", "editor"),
    ("POST", "/approve/{model_id}"): ("model", "editor"),
    ("POST", "/reject/{model_id}"): ("model", "editor"),
    ("POST", "/approvals/{model_id}/approve"): ("model", "editor"),
    ("POST", "/approvals/{model_id}/reject"): ("model", "editor"),
    ("DELETE", "/approvals/{approval_id}"): ("model", "editor"),
    ("DELETE", "/commands/{command_id}"): ("model", "editor"),
    ("POST", "/webhooks/modelzoo/gitlab"): (
        "exempt",
        "Inbound provider webhook authenticated by a shared secret: there is no principal to "
        "authorize. With the platform-level `auto_retrain` setting (admin scope, PUT "
        "/modelzoo/config) it queues retrains of every registry model directly, not through a "
        "gated route: that is a platform decision, not any project's.",
    ),
    ("POST", "/webhooks/modelzoo/github"): (
        "exempt",
        "Inbound provider webhook authenticated by HMAC signature; same reasoning as the GitLab "
        "webhook.",
    ),
    ("POST", "/modelzoo/sync"): (
        "exempt",
        "Platform-scoped admin operation on the ModelZoo poller; it names no project's model and "
        "needs the `admin` scope.",
    ),
    ("PUT", "/modelzoo/config"): (
        "exempt",
        "Platform-scoped admin configuration of the ModelZoo poller; names no project's model.",
    ),
    ("POST", "/admin/reload"): (
        "exempt",
        "Platform-scoped admin reload of the registry/config; names no project's model.",
    ),
}


def route_key(method: str, path: str) -> tuple[str, str]:
    """The table key for a live route: ``/v1/x`` and ``/x`` are the same handler."""
    return method.upper(), path[3:] if path.startswith("/v1/") else path


def subject_of(context: Any) -> str:
    """The relationship-store subject for an authenticated request context."""
    if getattr(context, "is_legacy", False):
        return LEGACY_SUBJECT
    return str(context.principal)


def asserted_projects(context: Any) -> dict[str, str] | None:
    """Project roles asserted by a *verified* IdP token; ``None`` for every other credential."""
    identity = getattr(context, "identity", None)
    if identity is None:
        return None
    projects = getattr(identity, "projects", None)
    return dict(projects) if isinstance(projects, dict) else None


def _record(outcome: str) -> None:
    try:
        import metrics as _metrics  # the service's own module (cwd on sys.path)

        _metrics.record_project_authz(outcome)
    except Exception as exc:  # noqa: BLE001 - a metrics failure never changes the decision
        logger.warning("project authz metric not recorded (%s): %s", outcome, exc)


def enforce_models(context: Any, models: Iterable[str], relation: str = "editor") -> None:
    """403 unless the caller holds ``relation`` on every project of every model; 503 if unknown.

    A no-op with multi-tenancy off. Call it first in the handler, before any lookup, so a denied
    caller learns nothing about whether the model or the approval exists.
    """
    from examlops import authz
    from examlops.authz.guard import ModelScopeUnavailable, model_allowed

    if not authz.multitenancy_enabled():
        return
    subject = subject_of(context)
    asserted = asserted_projects(context)
    for model in dict.fromkeys(m for m in models if m):  # de-duplicated, order kept
        try:
            ok = model_allowed(
                subject, relation, model, actor=str(context.principal), asserted_projects=asserted
            )
        except ModelScopeUnavailable as exc:
            _record("unavailable")
            logger.error("project authz unavailable for %s on %s: %s", subject, model, exc)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Project membership is unavailable; refusing rather than guessing",
            ) from exc
        if not ok:
            _record("deny")
            logger.warning("project authz denied %s '%s' on model %s", subject, relation, model)
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"{subject!r} lacks {relation!r} on the project of model {model!r}",
            )
    _record("allow")


def enforce_model(context: Any, model: str, relation: str = "editor") -> None:
    enforce_models(context, [model], relation)
