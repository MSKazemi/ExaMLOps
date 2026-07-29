"""Shared provenance + access-scope helpers for mutating ``exa`` commands (N5).

Two cross-cutting concerns for every state-changing command:

1. **``--reason`` provenance** — record *why* a change was made, not just what/who, in the
   audit trail. A reviewer reading `audit_events` later can see the operator's intent.
2. **Early access-scope hint** — surface up front which credential/permission a mutation
   needs, so a missing token is reported *before* the request is fired and rejected
   downstream (a 503/403), not after.

Keep these in one place so every mutating command applies the same governance UX.
"""

from __future__ import annotations

from typing import Any

import typer

from examlops.cli import _output

REASON_HELP = "Why you are making this change (recorded in the audit trail)"


def reason_option() -> Any:
    """Standard ``--reason`` option for a mutating command.

    Usage in a command signature::

        reason: str | None = reason_option(),
    """
    return typer.Option(None, "--reason", help=REASON_HELP)


def audit_details(details: dict[str, Any], reason: str | None) -> dict[str, Any]:
    """Return ``details`` with a ``reason`` key folded in when one was given.

    Returns the original dict unchanged when ``reason`` is falsy, so audit payloads stay
    identical for callers that pass no reason (backward-compatible).
    """
    if reason:
        return {**details, "reason": reason}
    return details


def scope_hint(needs: str, present: bool) -> None:
    """Warn early when a mutation needs a credential/permission that is not configured.

    ``needs`` is a human phrase like ``"a CONTROL_PLANE_TOKEN"``; ``present`` is whether it
    is configured. No-op when present, so it never adds noise to a correctly-set-up run.
    """
    if not present:
        _output.hint(
            f"This action needs {needs} — set it before retrying or the request may be rejected."
        )
