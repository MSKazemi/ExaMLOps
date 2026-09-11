"""Enterprise identity federation & delegated authorization (ADR 0120).

ExaMLOps is a guest in someone else's data center. The center already knows who its people are
(its IdP — Keycloak, Unity/Helmholtz ID, EGI Check-in, MyAccessID, an LDAP directory behind a
broker) and often already decides what they may do (its PDP — OPA, an AuthZEN service, OpenFGA).
This package makes ExaMLOps a well-behaved relying party and policy enforcement point *inside*
that arrangement instead of a second, competing identity silo:

* :mod:`~examlops.iam.config` — the trust file: one entry per center (issuer, audience, keys,
  claim mapping, tenant binding, PDP), strictly validated, fail-closed.
* :mod:`~examlops.iam.tokens` — one access-token verifier for every resource server
  (RFC 9068 / 8725 / 9700 / 7662), returning a federated :class:`Principal`.
* :mod:`~examlops.iam.claims` — group/entitlement/role mapping (Keycloak, LDAP DNs, AARC-G069).
* :mod:`~examlops.iam.pdp` — ``authorize()``: tenant isolation → local policy → the center's
  PDP (OpenID AuthZEN 1.0 or OPA), deny-overrides, fail-closed.
* :mod:`~examlops.iam.flows` — Authorization Code + PKCE (dashboard BFF), Device Authorization
  Grant (``exa auth login`` on headless HPC nodes), refresh.
* :mod:`~examlops.iam.session` — the CLI's local token cache (0600) and ``oidc-agent`` delegation.
* :mod:`~examlops.iam.stepup` — RFC 9470 step-up checks for high-risk actions.
* :mod:`~examlops.iam.directory` — the federated account directory: JIT records, deactivation,
  tombstones; enforced on every verified token (ADR 0132).
* :mod:`~examlops.iam.scim` — SCIM 2.0 (RFC 7643/7644) provisioning, so a center pushes and
  withdraws accounts instead of waiting for tokens to expire.

Off by default: with no trust file and no ``EXAMLOPS_OIDC_ISSUER``, nothing changes.
"""

from __future__ import annotations

from examlops.iam.config import (
    ROLE_RANK,
    ROLES,
    IamConfig,
    IamConfigError,
    ProviderConfig,
    load_config,
    parse_config,
)
from examlops.iam.pdp import Decision, authorize
from examlops.iam.tokens import (
    AuthenticationError,
    Principal,
    looks_like_jwt,
    verify_access_token,
    verify_bearer,
)


def is_enabled() -> bool:
    """True when at least one trusted identity provider is configured (and the config is valid)."""
    try:
        return load_config().enabled
    except IamConfigError:
        return False


def clear_caches() -> None:
    """Forget cached trust config, discovery/JWKS, introspection and PDP answers (tests, reload)."""
    from examlops.iam import config, directory, metadata, pdp, tokens

    config.clear_cache()
    directory.invalidate()
    directory._last_seen_written.clear()
    metadata.clear_cache()
    tokens.clear_cache()
    pdp.clear_cache()


__all__ = [
    "ROLES",
    "ROLE_RANK",
    "AuthenticationError",
    "Decision",
    "IamConfig",
    "IamConfigError",
    "Principal",
    "ProviderConfig",
    "authorize",
    "clear_caches",
    "is_enabled",
    "load_config",
    "looks_like_jwt",
    "parse_config",
    "verify_access_token",
    "verify_bearer",
]
