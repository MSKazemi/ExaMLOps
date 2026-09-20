"""Provider egress control (ADR 0154 d2, d5).

A provider's ``base_url`` decides where prompts are sent, so it is validated before a provider is
built: scheme, no embedded credentials, no cloud-metadata targets, no forbidden vendor, and — for an
``external`` provider — no platform-internal or private address unless the operator listed the host.

This is the *static* half of the control. Resolving the name and pinning the connection to the
address that was checked (DNS-rebinding safety) belongs to the connect path and is tracked in the
plan; a URL check alone does not stop a name that later resolves somewhere else.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

#: Hosts the platform's standing rule forbids: no new Azure activity. Not reducible by
#: configuration — ``EXAMLOPS_GATEWAY_DENY_HOSTS`` can only add to it.
_ALWAYS_DENIED_SUFFIXES = (
    ".openai.azure.com",
    ".cognitiveservices.azure.com",
    ".azure-api.net",
    ".azure.com",
)
_METADATA_NAMES = frozenset({"metadata.google.internal", "metadata", "instance-data"})
_INTERNAL_NAMES = frozenset({"localhost", "host.docker.internal", "gateway.docker.internal"})


class EgressDenied(ValueError):
    """A provider address that the gateway refuses to send prompts to."""


def _csv(name: str) -> tuple[str, ...]:
    return tuple(h.strip().lower() for h in os.getenv(name, "").split(",") if h.strip())


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def validate_base_url(
    url: str, *, locality: str = "local", allowed_hosts: tuple[str, ...] = ()
) -> str:
    """Return ``url`` without a trailing slash, or raise :class:`EgressDenied`.

    ``locality`` is the provider's declared locality (``local`` | ``site`` | ``external``).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EgressDenied(f"provider URL scheme must be http or https, got {parts.scheme!r}")
    if parts.username or parts.password:
        raise EgressDenied("provider URL must not embed credentials; use secret_ref")
    host = (parts.hostname or "").lower()
    if not host:
        raise EgressDenied("provider URL has no host")

    denied = _ALWAYS_DENIED_SUFFIXES + tuple(
        h if h.startswith(".") else f".{h}" for h in _csv("EXAMLOPS_GATEWAY_DENY_HOSTS")
    )
    if any(host == s.lstrip(".") or host.endswith(s) for s in denied):
        raise EgressDenied(
            f"host {host!r} is denied: Azure endpoints are forbidden on this platform "
            "(ADR 0154 d5); this cannot be overridden by configuration"
        )
    ip = _ip(host)
    if host in _METADATA_NAMES or (ip is not None and ip.is_link_local):
        raise EgressDenied(
            f"host {host!r} is a cloud-metadata / link-local target and is never allowed"
        )

    allowed = {h.lower() for h in (*allowed_hosts, *_csv("EXAMLOPS_GATEWAY_ALLOWED_HOSTS"))}
    if locality == "external" and host not in allowed:
        internal = (
            host in _INTERNAL_NAMES
            or "." not in host  # a bare service name (compose/k8s DNS) is platform-internal
            or (ip is not None and (ip.is_private or ip.is_loopback or ip.is_reserved))
        )
        if internal:
            raise EgressDenied(
                f"host {host!r} is platform-internal or private but the provider is declared "
                "external; declare it locality: local/site, or list it in "
                "EXAMLOPS_GATEWAY_ALLOWED_HOSTS on purpose"
            )
    return url.rstrip("/")
