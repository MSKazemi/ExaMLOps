"""P3 — Project scoping for serving & pipelines (ADR 0088).

Pure helpers that resolve a model's owning Project (ADR 0086) and turn it into the labels the
serving layer (Prometheus) and the pipeline layer (Prefect deployment tags) attach. Additive and
side-effect-free: a model with no project resolves to ``None`` and every caller degrades to its
existing unscoped behaviour.

Resolution precedence: explicit arg → ``EXAMLOPS_PROJECT`` env → project membership in
``platform.db`` (``get_project_for_model``).
"""

from __future__ import annotations

import os

_NONE_LABEL = "none"


def resolve_project(model: str, *, explicit: str | None = None) -> str | None:
    """Return the project a model belongs to, or ``None``.

    Precedence: ``explicit`` → ``EXAMLOPS_PROJECT`` env → membership lookup. Never raises — a DB
    error or missing table degrades to ``None`` (unscoped).
    """
    if explicit:
        return explicit
    env = os.getenv("EXAMLOPS_PROJECT")
    if env:
        return env
    try:
        from examlops.platform_db import get_project_for_model

        return get_project_for_model(model)
    except Exception:
        return None


def metric_label(model: str, *, explicit: str | None = None) -> str:
    """The Prometheus ``project`` label value for a model — a stable non-empty string."""
    return resolve_project(model, explicit=explicit) or _NONE_LABEL


def prefect_tags(
    model: str, *, explicit: str | None = None, base: list[str] | None = None
) -> list[str]:
    """Prefect deployment tags for a model, appending ``project:<name>`` when scoped.

    Returns a new list; ``base`` (default ``["examlops", "training"]``) is not mutated.
    """
    tags = list(base if base is not None else ["examlops", "training"])
    project = resolve_project(model, explicit=explicit)
    if project:
        tag = f"project:{project}"
        if tag not in tags:
            tags.append(tag)
    return tags
