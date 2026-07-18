"""Config validation across services (Phase 4 item 4.2 slice — `exa env --validate`).

130+ env vars sprawl across the CLI, control plane, dashboard, agent, and bridge with no single
place that says "is this deployment configured coherently?". This module checks the *effective*
environment for internal consistency + enterprise-readiness and returns typed findings, so
`exa env --validate` (and CI) can fail a misconfigured deploy before it starts serving.

Pure over an injected env dict → fully testable; the CLI passes ``os.environ``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

_TRUTHY = {"1", "true", "yes", "on"}
_PLACEHOLDER_TOKENS = {"changeme", "change-me", "secret", "password", "token", "examlops-dev-key"}


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warn" | "ok"
    key: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "key": self.key, "message": self.message}


def _is_url(v: str) -> bool:
    p = urlparse(v)
    return bool(p.scheme in ("http", "https") and p.netloc)


def validate(env: Mapping[str, str]) -> list[Finding]:
    """Return config findings for the effective ``env``. Empty error-level list ⇒ coherent."""
    out: list[Finding] = []

    def err(k, m):
        out.append(Finding("error", k, m))

    def warn(k, m):
        out.append(Finding("warn", k, m))

    # URL-shaped settings must parse.
    for key in ("CONTROL_PLANE_URL", "RAY_SERVE_URL", "MLFLOW_TRACKING_URI", "AGENT_URL"):
        v = env.get(key)
        if v and not _is_url(v):
            err(key, f"not a valid URL: {v!r}")

    # Control-plane token: placeholder / weak.
    tok = env.get("CONTROL_PLANE_TOKEN", "")
    if tok and (tok.lower() in _PLACEHOLDER_TOKENS or len(tok) < 16):
        warn("CONTROL_PLANE_TOKEN", "placeholder or weak (<16 chars) — write endpoints fail closed")

    # Dashboard secrets strength.
    for key in ("DASHBOARD_JWT_SECRET", "DASHBOARD_SECRET_KEY"):
        v = env.get(key)
        if v and len(v) < 32:
            warn(key, "shorter than 32 chars — use a strong random value")

    # Coordinator ↔ its backend.
    if env.get("EXAMLOPS_COORDINATOR", "db").lower() == "redis" and not env.get(
        "EXAMLOPS_REDIS_URL"
    ):
        err("EXAMLOPS_COORDINATOR", "set to 'redis' but EXAMLOPS_REDIS_URL is unset")

    # Event publisher ↔ its broker endpoint.
    pub = env.get("EXAMLOPS_EVENT_PUBLISHER", "log").lower()
    broker_env = {
        "nats": "EXAMLOPS_NATS_URL",
        "kafka": "EXAMLOPS_KAFKA_BROKERS",
        "redis": "EXAMLOPS_REDIS_URL",
    }
    if pub in broker_env and not env.get(broker_env[pub]):
        err("EXAMLOPS_EVENT_PUBLISHER", f"set to '{pub}' but {broker_env[pub]} is unset")

    # DB backend ↔ DSN.
    if env.get("EXAMLOPS_DB_BACKEND", "sqlite").lower() == "postgres" and not (
        env.get("DATABASE_URL") or env.get("EXAMLOPS_POSTGRES_DSN")
    ):
        err("EXAMLOPS_DB_BACKEND", "set to 'postgres' but no DATABASE_URL / EXAMLOPS_POSTGRES_DSN")

    # OIDC coherence.
    if env.get("EXAMLOPS_OIDC_ISSUER"):
        if not env.get("EXAMLOPS_OIDC_JWKS"):
            err(
                "EXAMLOPS_OIDC_JWKS", "OIDC issuer set but no JWKS (URL or inline) to verify tokens"
            )
        if not env.get("EXAMLOPS_OIDC_AUDIENCE"):
            warn(
                "EXAMLOPS_OIDC_AUDIENCE", "OIDC issuer set but no audience — audience check skipped"
            )

    # Autopilot enabled without a kill-switch source is fine, but flag placeholder signing.
    if env.get("EXAMLOPS_SIGNING_KEY", "").lower() in _PLACEHOLDER_TOKENS:
        warn("EXAMLOPS_SIGNING_KEY", "placeholder signing key — signatures are forgeable")

    if not any(f.level == "error" for f in out):
        out.append(Finding("ok", "-", "no configuration errors"))
    return out


def has_errors(findings: list[Finding]) -> bool:
    return any(f.level == "error" for f in findings)
