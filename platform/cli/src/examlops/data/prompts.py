"""examlops.data.prompts — Prompt registry (B1).

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry  # noqa: F401

__all__ = [
    "create_prompt_version",
    "get_prompt_by_label",
    "get_prompt_version",
    "list_prompt_labels",
    "list_prompt_names",
    "list_prompt_versions",
    "set_prompt_label",
]
# `prompt_backend` / `use_backend` choose *where* the helpers above store prompts; they are not
# platform_db helpers, so they stay out of `__all__` (the facade contract: every name in it is the
# same object as `platform_db.<name>`) and are imported by name.

PROMPT_BACKENDS = ("platform_db", "mlflow")
_override: ContextVar[str | None] = ContextVar("prompt_backend_override", default=None)


@contextmanager
def use_backend(name: str) -> Iterator[None]:
    """Run a block against one backend regardless of the environment (used by migration)."""
    if name not in PROMPT_BACKENDS:
        raise ValueError(f"prompt backend {name!r} is not one of {', '.join(PROMPT_BACKENDS)}")
    token = _override.set(name)
    try:
        yield
    finally:
        _override.reset(token)


def prompt_backend() -> str:
    """The prompt registry backend: ``EXAMLOPS_PROMPT_BACKEND`` (default ``platform_db``).

    ``mlflow`` keeps prompts in the MLflow Prompt Registry (ADR 0009 clause 1) through
    :mod:`examlops.prompts.mlflow_backend`, which implements every helper below with the same
    signature and row shape. The dispatch sits at the top of each helper rather than in separate
    functions so ``install_write_retry`` still finds and wraps the SQL writers. An unknown value
    is an error: a typo must not quietly write prompts to the wrong registry.
    """
    forced = _override.get()
    if forced:
        return forced
    name = (os.getenv("EXAMLOPS_PROMPT_BACKEND") or "platform_db").strip().lower()
    if name not in PROMPT_BACKENDS:
        raise ValueError(
            f"EXAMLOPS_PROMPT_BACKEND={name!r} is not one of {', '.join(PROMPT_BACKENDS)}"
        )
    return name


def _mlflow() -> Any:
    """The MLflow backend module when selected, else ``None``."""
    if prompt_backend() != "mlflow":
        return None
    from examlops.prompts import mlflow_backend

    return mlflow_backend


def create_prompt_version(
    name: str,
    template: str,
    *,
    variables: list[str] | None = None,
    tags: dict[str, Any] | None = None,
    actor: str | None = None,
) -> int:
    """Create a new immutable prompt version (spec R1). Returns the new version number."""
    if (mlf := _mlflow()) is not None:
        return int(
            mlf.create_prompt_version(name, template, variables=variables, tags=tags, actor=actor)
        )
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM prompt_versions WHERE name=?", (name,)
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            """INSERT INTO prompt_versions (name, version, template, variables, tags, actor)
               VALUES (?,?,?,?,?,?)""",
            (
                name,
                version,
                template,
                json.dumps(variables or []),
                json.dumps(tags or {}),
                actor,
            ),
        )
    return version


def get_prompt_by_label(name: str, label: str) -> dict[str, Any] | None:
    """Resolve ``name@label`` to its pinned prompt version (spec R3)."""
    if (mlf := _mlflow()) is not None:
        return mlf.get_prompt_by_label(name, label)
    init_db()
    with get_db() as conn:
        lab = conn.execute(
            "SELECT version FROM prompt_labels WHERE name=? AND label=?", (name, label)
        ).fetchone()
    if lab is None:
        return None
    return get_prompt_version(name, int(lab["version"]))


def get_prompt_version(name: str, version: int) -> dict[str, Any] | None:
    if (mlf := _mlflow()) is not None:
        return mlf.get_prompt_version(name, version)
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM prompt_versions WHERE name=? AND version=?", (name, version)
        ).fetchone()
    return dict(row) if row else None


def list_prompt_labels(name: str) -> list[dict[str, Any]]:
    if (mlf := _mlflow()) is not None:
        return mlf.list_prompt_labels(name)
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_labels WHERE name=? ORDER BY label", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_prompt_names() -> list[str]:
    if (mlf := _mlflow()) is not None:
        return mlf.list_prompt_names()
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT name FROM prompt_versions ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def list_prompt_versions(name: str) -> list[dict[str, Any]]:
    if (mlf := _mlflow()) is not None:
        return mlf.list_prompt_versions(name)
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_versions WHERE name=? ORDER BY version DESC", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_prompt_label(name: str, label: str, version: int) -> None:
    """Point a label at a version (spec R8). Caller writes the audit event (R9)."""
    if (mlf := _mlflow()) is not None:
        mlf.set_prompt_label(name, label, version)
        return
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO prompt_labels (name, label, version, updated_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(name, label) DO UPDATE SET
                   version=excluded.version, updated_at=CURRENT_TIMESTAMP""",
            (name, label, version),
        )


install_write_retry(__name__)
