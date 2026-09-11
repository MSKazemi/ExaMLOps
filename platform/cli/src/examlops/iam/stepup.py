"""RFC 9470 step-up authentication for high-risk actions (ADR 0120).

A valid session is not always enough: promoting a model to production or revealing a secret should
need a *recent* and *strong enough* authentication. The resource server says so with

    HTTP 401
    WWW-Authenticate: Bearer error="insufficient_user_authentication",
        error_description="...", acr_values="https://refeds.org/profile/mfa", max_age=900

and the client re-authenticates with those ``acr_values`` / ``max_age`` (RFC 9470 §3-4).

Opt-in, never a lock-out: a federated user is challenged only when their center's trust entry has a
``step_up`` section (the center must be able to issue the ``acr`` asked for); a local password
session only when ``EXAMLOPS_IAM_STEP_UP=enforce`` (``EXAMLOPS_IAM_STEP_UP_MAX_AGE``, default 900 s).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from examlops.iam.config import StepUpConfig

ERROR = "insufficient_user_authentication"


@dataclass(frozen=True)
class Challenge:
    description: str
    acr_values: tuple[str, ...] = ()
    max_age: int | None = None

    def www_authenticate(self) -> str:
        parts = [f'error="{ERROR}"', f'error_description="{self.description}"']
        if self.acr_values:
            parts.append(f'acr_values="{" ".join(self.acr_values)}"')
        if self.max_age is not None:
            parts.append(f"max_age={self.max_age}")
        return "Bearer " + ", ".join(parts)

    def as_dict(self) -> dict:
        return {
            "error": ERROR,
            "error_description": self.description,
            "acr_values": list(self.acr_values),
            "max_age": self.max_age,
        }


def local_policy() -> StepUpConfig:
    """Step-up policy for sessions that did not come from a federated IdP."""
    enforce = os.getenv("EXAMLOPS_IAM_STEP_UP", "").strip().lower() == "enforce"
    try:
        max_age = int(os.getenv("EXAMLOPS_IAM_STEP_UP_MAX_AGE", "900"))
    except ValueError:
        max_age = 900
    return StepUpConfig(enabled=enforce, acr_values=(), max_age_s=max_age)


def evaluate(
    policy: StepUpConfig,
    *,
    acr: str | None,
    auth_time: int | float | None,
    now: float | None = None,
) -> Challenge | None:
    """``None`` when the authentication satisfies ``policy``, else the challenge to send."""
    if not policy.enabled:
        return None
    if policy.acr_values and acr not in policy.acr_values:
        return Challenge(
            "a stronger authentication is required for this action",
            policy.acr_values,
            policy.max_age_s,
        )
    t = time.time() if now is None else now
    if auth_time is None or t - float(auth_time) > policy.max_age_s:
        return Challenge(
            "a more recent authentication is required for this action",
            policy.acr_values,
            policy.max_age_s,
        )
    return None
