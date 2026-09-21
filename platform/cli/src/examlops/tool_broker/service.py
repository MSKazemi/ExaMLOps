"""Grant management: validate, store, list, remove, load (ADR 0145).

Storage is :mod:`examlops.data.tool_grants`; the schema and the decision are
:mod:`examlops.tool_broker.grants`. Every change is audited (``tool_grant_set`` /
``tool_grant_removed``); the CLI additionally passes it through the ``tool_grant_change`` policy hook.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from examlops.data import tool_grants as store
from examlops.data.audit import audit_best_effort
from examlops.tool_broker.grants import (
    ANY_TOOL,
    GrantError,
    GrantSet,
    ToolCaller,
    ToolGrant,
    parse_grant,
    resolve_subjects,
)

__all__ = [
    "list_grants",
    "load_grant_set",
    "remove_grant",
    "resolve_grant_set",
    "set_grant",
    "subjects",
]

_SOURCE = "tool-broker"


def _who() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def _registry_names() -> set[str]:
    from examlops.mcp.tools import REGISTRY

    return {s.name for s in REGISTRY}


def _audit_change(action: str, subject: str, tool: str, details: dict[str, Any]) -> None:
    audit_best_effort(_SOURCE, _who(), action, f"{subject}:{tool}", details)


def set_grant(
    subject: str,
    tool: str,
    doc: Mapping[str, Any],
    *,
    known_tools: set[str] | None = None,
) -> dict[str, Any]:
    """Validate and store one grant (replacing the subject's grant for that tool)."""
    if not isinstance(subject, str) or not subject.strip():
        raise GrantError(["subject: required (agent name, agent-version id or workload subject)"])
    grant = parse_grant(tool, doc)
    names = known_tools if known_tools is not None else _registry_names()
    if tool != ANY_TOOL and tool not in names:
        raise GrantError([f"tool: {tool!r} is not a registered tool (or '*')"])
    stored = grant.to_doc()
    replaced = store.put_grant(subject, tool, stored, actor=_who())
    _audit_change(
        "tool_grant_set",
        subject,
        tool,
        {"grant": stored, "replaced": replaced},
    )
    return {"ok": True, "subject": subject, "tool": tool, "replaced": replaced, "grant": stored}


def remove_grant(subject: str, tool: str | None = None) -> dict[str, Any]:
    """Remove one grant or all of a subject's. Removing the last one un-brokers the subject."""
    n = store.delete_grant(subject, tool)
    remaining = len(store.get_grants(subject))
    _audit_change(
        "tool_grant_removed",
        subject,
        tool or ANY_TOOL,
        {"removed": n, "remaining": remaining, "all": tool is None},
    )
    return {
        "ok": True,
        "subject": subject,
        "removed": n,
        "remaining": remaining,
        "unbrokered": remaining == 0,
    }


def list_grants(subject: str | None = None) -> list[dict[str, Any]]:
    return [
        {"subject": r["subject"], "tool": r["tool"], **r["grant"]}
        for r in store.list_grants(subject)
    ]


def subjects() -> list[str]:
    return store.list_subjects()


def load_grant_set(subject: str) -> GrantSet | None:
    """The stored grants of exactly ``subject``, or ``None`` when it has none."""
    rows = store.get_grants(subject)
    if not rows:
        return None
    return GrantSet(subject, {r["tool"]: _parse_row(r) for r in rows})


def _parse_row(r: dict[str, Any]) -> ToolGrant:
    return parse_grant(r["tool"], r["grant"])


def resolve_grant_set(caller: ToolCaller) -> GrantSet | None:
    """Most specific set for ``caller`` (version id, then agent name, then workload subject)."""
    for subject in resolve_subjects(caller):
        gs = load_grant_set(subject)
        if gs is not None:
            return gs
    return None
