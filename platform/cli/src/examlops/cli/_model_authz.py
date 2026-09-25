"""Model/dataset-level project enforcement for ``exa`` commands outside ``exa project`` (ADR 0014).

The ``exa project`` commands already refuse a subject without the needed relation on the project.
The commands that mutate a *model* - ``exa pipeline promote`` / ``run``, ``exa serve traffic`` and
``exa drift auto-retrain enable|disable`` - and the ``exa data`` revision commands live elsewhere,
and until now let any subject act on any project's model or dataset. They call
:func:`guard_model` / :func:`guard_dataset` first; both ask
:func:`examlops.authz.guard.resource_allowed`, the same question the control plane's retrain and
approval routes ask, so the CLI and the API cannot disagree.

Contract: a no-op with ``EXAMLOPS_MULTITENANCY`` off (single-tenant behaviour byte-identical);
with it on, a denied subject exits 1 *before* any lookup or side effect, and so does a membership
store that cannot be read (fail closed). The denial itself is audited by ``authz.check``.

The subject is the CLI actor (``EXAMLOPS_ACTOR``, else ``$USER``) - the same subject the
``exa project`` guards use.
"""

from __future__ import annotations

import os

from . import _output

#: Every command that calls :func:`guard_model` / :func:`guard_dataset`, with the relation it
#: requires. ``tests/unit/test_model_authz_cli.py`` asserts each one really calls its guard, so the
#: list cannot claim a gate that is not there.
GUARDED_COMMANDS: dict[str, str] = {
    "pipeline promote": "editor",
    "pipeline run": "editor",
    "serve traffic": "editor",
    "drift auto-retrain enable": "editor",
    "drift auto-retrain disable": "editor",
    "data snapshot": "editor",
    "data list": "viewer",
    "data diff": "viewer",
    "data checkout": "viewer",
    "data validate": "editor",
    "data synth generate": "editor",
    "cards dataset": "editor",
    "project assign": "editor",
    "project assign-model": "editor",
}


def actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def guard_project(project: str, relation: str = "editor") -> None:
    """Exit 1 unless the CLI actor holds ``relation`` on ``project`` (no-op, flag off)."""
    from examlops.authz.guard import ProjectAccessDenied, require

    try:
        require(actor(), relation, project)
    except ProjectAccessDenied as exc:
        _output.error(f"Permission denied: {exc}")


def _guard(kind: str, ref: str, relation: str) -> None:
    from examlops.authz.guard import ModelScopeUnavailable, ProjectAccessDenied, require_resource

    try:
        require_resource(actor(), relation, kind, ref)
    except ModelScopeUnavailable as exc:
        _output.error(
            f"Permission check unavailable, refusing: {exc}",
            hint="The project membership store could not be read; retry when it is reachable.",
        )
    except ProjectAccessDenied as exc:
        _output.error(
            f"Permission denied: {exc}",
            hint=f"Ask a project owner: exa project add-member <project> <you> --role {relation}",
        )


def guard_resource(kind: str, ref: str, relation: str) -> None:
    """Exit 1 unless the CLI actor holds ``relation`` on every project holding ``kind``/``ref``."""
    from examlops import authz

    if not authz.multitenancy_enabled():
        return
    _guard(kind, ref, relation)


def guard_model(model: str | None, relation: str = "editor") -> None:
    """Exit 1 unless the CLI actor holds ``relation`` on every project that holds ``model``.

    ``model=None`` means the command acts on *every* model (``exa pipeline run`` without
    ``--model``): with multi-tenancy on only a platform admin may do that, because no single
    project's grant covers the whole registry.
    """
    from examlops import authz

    if not authz.multitenancy_enabled():
        return
    from examlops.authz.guard import platform_admins

    if model is None:
        if actor() not in platform_admins():
            _output.error(
                f"Permission denied: {actor()} may not act on every model at once "
                "with multi-tenancy on",
                hint="Name one model with --model, or run as a platform admin "
                "(EXAMLOPS_AUTHZ_ADMINS).",
            )
        return
    _guard("model", model, relation)


def guard_dataset(dataset: str, relation: str = "viewer") -> None:
    """Exit 1 unless the CLI actor holds ``relation`` on every project that holds ``dataset``.

    A dataset's project is its ``project_resources`` row of kind ``dataset``
    (``exa project assign <p> <dataset> --kind dataset``); an unassigned dataset is ``default``'s.
    """
    from examlops import authz

    if not authz.multitenancy_enabled():
        return
    _guard("dataset", dataset, relation)
