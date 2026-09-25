"""Typed exceptions of the public SDK (ADR 0078).

Every SDK operation raises one of these instead of leaking an internal exception type
(``cli._client.ClientError``, ``mlflow_paging.PagingError``, ``sqlite3`` errors). A caller that
wants "anything went wrong" catches :class:`SDKError`; one that wants to branch catches the
subclass. The hierarchy is part of the semver'd surface.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SDKError",
    "NotFoundError",
    "UnavailableError",
    "IncompleteReadError",
    "InvalidArgumentError",
    "ConfirmationRequiredError",
    "PolicyDeniedError",
    "ApprovalRequiredError",
    "GateRefusedError",
]


class SDKError(Exception):
    """Base class of every error the SDK raises. ``status`` is the upstream HTTP status, if any."""

    def __init__(self, message: str, *, status: int | None = None, **detail: Any) -> None:
        super().__init__(message)
        self.status = status
        self.detail: dict[str, Any] = dict(detail)


class NotFoundError(SDKError):
    """The named object (model, version, run, cluster) does not exist."""


class UnavailableError(SDKError):
    """A backing service (control plane, MLflow, platform datastore) could not be reached."""


class IncompleteReadError(SDKError):
    """A paginated read could not be completed; no partial answer is returned in its place."""


class InvalidArgumentError(SDKError, ValueError):
    """An argument was rejected before anything was contacted or changed."""


class ConfirmationRequiredError(SDKError):
    """A mutating call was made without ``confirm=True`` (and without ``dry_run=True``).

    The SDK's analogue of the CLI's confirmation prompt: a program has no keyboard, so consent is
    an explicit argument, never a default. Nothing was changed.
    """


class PolicyDeniedError(SDKError):
    """The policy-as-code engine (ADR 0079) denied the operation. Nothing was changed."""


class ApprovalRequiredError(SDKError):
    """A policy rule requires human approval and ``approved=True`` was not passed."""


class GateRefusedError(SDKError):
    """A governance gate (eval, parity, SLO, compliance, fairness, …) refused the operation."""


def from_client_error(exc: BaseException) -> SDKError:
    """Map a transport-layer ``ClientError`` onto the SDK hierarchy, keeping its message."""
    status = getattr(exc, "status", None)
    message = str(exc)
    if status == 404:
        return NotFoundError(message, status=status)
    if status is None or status >= 500 or status in (408, 429):
        return UnavailableError(message, status=status)
    return SDKError(message, status=status)
