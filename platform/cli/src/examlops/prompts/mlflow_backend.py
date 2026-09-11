"""The MLflow Prompt Registry as the prompt backend (ADR 0009 clause 1).

Selected with ``EXAMLOPS_PROMPT_BACKEND=mlflow``. It implements exactly the seven functions of
:mod:`examlops.data.prompts` with the same signatures and the same row shape, so the CLI, the
dashboard, Skipper, the gateway and the lineage emitter move to MLflow with no change of their
own: prompts then live in the same registry as the models they drive, with MLflow's UI, lineage
links and access control.

**Templates are stored verbatim.** ExaMLOps templates use Python ``str.format`` syntax
(``{var}``); MLflow's own renderer expects ``{{var}}``. Converting between them is lossy at the
edges (escaped braces, format specs), and a prompt that renders differently depending on which
backend holds it would break the one promise a registry makes. So the template is stored exactly
as written, the syntax is recorded in the version tag ``examlops.template_syntax``, and ExaMLOps
renders it itself (``examlops.prompts.render``) — byte-identical on either backend.

ExaMLOps metadata rides in version tags: ``examlops.variables`` and ``examlops.tags`` (JSON),
``examlops.actor``. Labels (``dev``/``staging``/``prod``) are MLflow **aliases**.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

TEMPLATE_SYNTAX = "python-format"
_TAG_VARIABLES = "examlops.variables"
_TAG_TAGS = "examlops.tags"
_TAG_ACTOR = "examlops.actor"
_TAG_SYNTAX = "examlops.template_syntax"


class PromptBackendError(RuntimeError):
    """The MLflow prompt backend is not usable (no tracking URI, MLflow missing, …)."""


def _client() -> Any:
    uri = os.getenv("EXAMLOPS_PROMPT_MLFLOW_URI") or os.getenv("MLFLOW_TRACKING_URI")
    if not uri:
        raise PromptBackendError(
            "EXAMLOPS_PROMPT_BACKEND=mlflow needs MLFLOW_TRACKING_URI (or "
            "EXAMLOPS_PROMPT_MLFLOW_URI) — the registry the prompts live in"
        )
    try:
        from mlflow import MlflowClient
    except ImportError as exc:  # pragma: no cover - mlflow is a platform dependency
        raise PromptBackendError("the MLflow prompt backend needs the mlflow package") from exc
    return MlflowClient(tracking_uri=uri, registry_uri=uri)


def _iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def _row(pv: Any) -> dict[str, Any]:
    """An MLflow ``PromptVersion`` in the row shape ``examlops.data.prompts`` returns."""
    tags = dict(pv.tags or {})
    template = pv.template if isinstance(pv.template, str) else json.dumps(pv.template)
    return {
        "name": pv.name,
        "version": int(pv.version),
        "template": template,
        "variables": tags.get(_TAG_VARIABLES) or "[]",
        "tags": tags.get(_TAG_TAGS) or "{}",
        "actor": tags.get(_TAG_ACTOR),
        "created_at": _iso(getattr(pv, "creation_timestamp", None)),
    }


def _not_found(exc: Exception) -> bool:
    code = getattr(exc, "error_code", "") or ""
    return (
        code in ("RESOURCE_DOES_NOT_EXIST", "INVALID_PARAMETER_VALUE")
        or "not found" in str(exc).lower()
    )


def create_prompt_version(
    name: str,
    template: str,
    *,
    variables: list[str] | None = None,
    tags: dict[str, Any] | None = None,
    actor: str | None = None,
) -> int:
    version_tags = {
        _TAG_VARIABLES: json.dumps(variables or []),
        _TAG_TAGS: json.dumps(tags or {}),
        _TAG_SYNTAX: TEMPLATE_SYNTAX,
    }
    if actor:
        version_tags[_TAG_ACTOR] = actor
    pv = _client().register_prompt(
        name=name,
        template=template,
        commit_message=f"examlops: new version by {actor or 'unknown'}",
        tags=version_tags,
    )
    return int(pv.version)


def get_prompt_version(name: str, version: int) -> dict[str, Any] | None:
    client = _client()
    try:
        pv = client.get_prompt_version(name, int(version))
    except Exception as exc:
        if _not_found(exc):
            return None
        raise
    return _row(pv) if pv is not None else None


def get_prompt_by_label(name: str, label: str) -> dict[str, Any] | None:
    client = _client()
    # Check the prompt first: MLflow reports a missing alias as INVALID_PARAMETER_VALUE, which is
    # also what a genuinely malformed request returns — asking about the prompt keeps "not there"
    # distinguishable from "something is wrong".
    if client.get_prompt(name) is None:
        return None
    try:
        pv = client.get_prompt_version_by_alias(name, label)
    except Exception as exc:
        if _not_found(exc):
            return None
        raise
    return _row(pv)


def _versions(name: str) -> list[Any]:
    client = _client()
    if client.get_prompt(name) is None:
        return []
    out: list[Any] = []
    token = None
    while True:
        page = client.search_prompt_versions(name, max_results=200, page_token=token)
        out.extend(page)
        token = getattr(page, "token", None)
        if not token:
            return out


def list_prompt_versions(name: str) -> list[dict[str, Any]]:
    return sorted((_row(v) for v in _versions(name)), key=lambda r: -r["version"])


def list_prompt_labels(name: str) -> list[dict[str, Any]]:
    # `search_prompt_versions` returns versions with *empty* aliases (MLflow 3.11); only a single
    # `get_prompt_version` fills them in. So each version is fetched once — prompts have tens of
    # versions, not thousands, and a label list that silently came back empty is the alternative.
    client = _client()
    labels: list[dict[str, Any]] = []
    for v in _versions(name):
        full = client.get_prompt_version(name, int(v.version))
        for alias in getattr(full, "aliases", None) or []:
            labels.append(
                {
                    "name": name,
                    "label": alias,
                    "version": int(full.version),
                    "updated_at": _iso(getattr(full, "last_updated_timestamp", None)),
                }
            )
    return sorted(labels, key=lambda r: r["label"])


def list_prompt_names() -> list[str]:
    client = _client()
    names: list[str] = []
    token = None
    while True:
        page = client.search_prompts(max_results=1000, page_token=token)
        names.extend(p.name for p in page)
        token = getattr(page, "token", None)
        if not token:
            return sorted(set(names))


def set_prompt_label(name: str, label: str, version: int) -> None:
    _client().set_prompt_alias(name, alias=label, version=int(version))
