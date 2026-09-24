"""Provider egress control (ADR 0154 d2, d5).

A provider's ``base_url`` decides where prompts are sent, so it is validated before a provider is
built: scheme, no embedded credentials, no cloud-metadata targets, no forbidden vendor, and — for an
``external`` provider — no platform-internal or private address unless the operator listed the host.

That is the *static* half of the control, checked once against the URL string. The *dynamic* half
(BL-111, 2026-09-24) re-checks the **resolved address** on every connection: a hostname that passed
the static check at provider-construction time can be repointed by its own DNS operator to a
forbidden address later (DNS rebinding), and a static, one-time string check has nothing to say
about that. :func:`check_resolved_addresses` resolves the host fresh and applies the identical
classification rules to every address it returns, so a rebound name is refused before a request is
sent to it, not after. Redirects to another host need no separate handling here: every provider
client in this package sets ``follow_redirects=False`` explicitly, so a 3xx response is surfaced as
an error rather than silently followed — see each provider's own client construction.

:func:`guarded_async_client` closes the gap ``check_resolved_addresses`` alone leaves open: it pins
the real TCP connection to exactly the address :func:`_resolve_approved_address` just validated —
mirroring the proven mechanism in ``examlops.dataplane.safety._GuardedBackend`` (a custom
``httpcore`` network backend), translated to the async client the gateway's providers use. TLS is
unaffected: httpcore passes the original hostname as ``server_hostname`` when it starts TLS on the
returned stream, so SNI and certificate validation still use the name, never the IP.
``OllamaProvider`` uses this for its real client and falls back to ``check_resolved_addresses``
only when a caller supplies its own transport (tests, or a caller managing its own connection
pooling) — the guarded backend has nothing to wrap in that case.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpcore

if TYPE_CHECKING:
    import httpx

if TYPE_CHECKING:
    pass

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


def _check_denied_suffix(host: str) -> None:
    denied = _ALWAYS_DENIED_SUFFIXES + tuple(
        h if h.startswith(".") else f".{h}" for h in _csv("EXAMLOPS_GATEWAY_DENY_HOSTS")
    )
    if any(host == s.lstrip(".") or host.endswith(s) for s in denied):
        raise EgressDenied(
            f"host {host!r} is denied: Azure endpoints are forbidden on this platform "
            "(ADR 0154 d5); this cannot be overridden by configuration"
        )


def _check_metadata_or_link_local(
    host: str, ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
) -> None:
    if host in _METADATA_NAMES or (ip is not None and ip.is_link_local):
        raise EgressDenied(
            f"host {host!r} is a cloud-metadata / link-local target and is never allowed"
        )


def _check_external_locality(
    host: str,
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
    *,
    locality: str,
    allowed: set[str],
    original_host: str,
) -> None:
    """Refuse a private/internal address declared as ``external``, unless the operator allowed it.

    ``original_host`` (the hostname the operator actually configured) is what
    ``EXAMLOPS_GATEWAY_ALLOWED_HOSTS``/``allowed_hosts`` name — a *resolved IP* is checked against
    the same allow-list under the name the operator wrote, not the address it happened to resolve
    to this time, which is exactly the property a DNS-rebinding check needs.
    """
    if locality != "external" or original_host in allowed:
        return
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


def validate_base_url(
    url: str, *, locality: str = "local", allowed_hosts: tuple[str, ...] = ()
) -> str:
    """Return ``url`` without a trailing slash, or raise :class:`EgressDenied`.

    ``locality`` is the provider's declared locality (``local`` | ``site`` | ``external``). This is
    the *static* check, against the URL string as configured — see :func:`check_resolved_addresses`
    for the per-connection check against where the hostname actually resolves right now.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EgressDenied(f"provider URL scheme must be http or https, got {parts.scheme!r}")
    if parts.username or parts.password:
        raise EgressDenied("provider URL must not embed credentials; use secret_ref")
    host = (parts.hostname or "").lower()
    if not host:
        raise EgressDenied("provider URL has no host")

    _check_denied_suffix(host)
    ip = _ip(host)
    _check_metadata_or_link_local(host, ip)
    allowed = {h.lower() for h in (*allowed_hosts, *_csv("EXAMLOPS_GATEWAY_ALLOWED_HOSTS"))}
    _check_external_locality(host, ip, locality=locality, allowed=allowed, original_host=host)
    return url.rstrip("/")


def check_resolved_addresses(
    url: str,
    *,
    locality: str = "local",
    allowed_hosts: tuple[str, ...] = (),
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> None:
    """Resolve ``url``'s host fresh and apply the same rules :func:`validate_base_url` applies to
    the string — to every address DNS returns *right now*, not the one that may have been true when
    the provider was constructed. Raises :class:`EgressDenied` on the first disallowed address.

    A no-op when the host is already a literal IP (nothing to resolve — the string check already
    covered it) or when resolution itself fails (a dead/unresolvable host is a connectivity problem
    the provider's own connect-timeout handling reports; this function only refuses a resolution
    that *succeeds* somewhere forbidden, it does not stand in for reachability).

    ``resolver`` defaults to :func:`socket.getaddrinfo` and exists so a caller (test or otherwise)
    can inject a deterministic stand-in instead of making a real DNS query — this function is sync
    and makes no timeout guarantee of its own; a caller on an event loop should run it via
    ``asyncio.to_thread`` under an ``asyncio.wait_for`` bound, exactly as
    :class:`~examlops.gateway.providers.ollama.OllamaProvider` does.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host or _ip(host) is not None:
        return
    try:
        # AF_UNSPEC: check both IPv4 and IPv6 answers, since either could be used to connect.
        infos = resolver(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    except OSError:
        return  # resolution failure is a reachability problem, not an egress one
    allowed = {h.lower() for h in (*allowed_hosts, *_csv("EXAMLOPS_GATEWAY_ALLOWED_HOSTS"))}
    seen: set[str] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = str(sockaddr[0])
        if addr in seen:
            continue
        seen.add(addr)
        ip = _ip(addr)
        _check_metadata_or_link_local(addr, ip)
        _check_external_locality(addr, ip, locality=locality, allowed=allowed, original_host=host)


def _resolve_approved_address(
    host: str,
    port: int,
    *,
    locality: str,
    allowed_hosts: tuple[str, ...],
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> str:
    """Resolve ``host`` once, validate every address it returns, and return the first one.

    Unlike :func:`check_resolved_addresses` (a pre-flight probe that treats a resolution failure as
    a reachability problem it stays silent about), this backs a real connection attempt and always
    needs a concrete address back — a resolution failure here is refused, not shrugged off.

    Mirrors ``examlops.dataplane.safety.check_address``'s contract: every returned address is
    checked (a single forbidden answer refuses the whole resolution, not just that one address,
    since an attacker with any hand in the DNS answer should not get a free pass by including one
    innocuous-looking address alongside a malicious one), and the first address is what is actually
    used to connect once all have passed.
    """
    name = host.lower()
    allowed = {h.lower() for h in (*allowed_hosts, *_csv("EXAMLOPS_GATEWAY_ALLOWED_HOSTS"))}
    literal = _ip(name)
    if literal is not None:
        _check_metadata_or_link_local(name, literal)
        _check_external_locality(
            name, literal, locality=locality, allowed=allowed, original_host=name
        )
        return name
    try:
        infos = resolver(name, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise EgressDenied(f"{host!r} did not resolve: {exc}") from None
    if not infos:
        raise EgressDenied(f"{host!r} did not resolve")
    seen: set[str] = set()
    for info in infos:
        addr = str(info[4][0])
        if addr in seen:
            continue
        seen.add(addr)
        ip = _ip(addr)
        _check_metadata_or_link_local(addr, ip)
        _check_external_locality(addr, ip, locality=locality, allowed=allowed, original_host=name)
    return str(infos[0][4][0])


class AsyncGuardedBackend(httpcore.AsyncNetworkBackend):
    """An ``httpcore`` async network backend that connects only to an address this module approved.

    See the module docstring — this is what actually pins a connection, as opposed to
    :func:`check_resolved_addresses`'s pre-flight-only probe.
    """

    def __init__(
        self,
        inner: httpcore.AsyncNetworkBackend,
        *,
        locality: str,
        allowed_hosts: tuple[str, ...],
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        self._inner = inner
        self._locality = locality
        self._allowed_hosts = allowed_hosts
        self._resolver = resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        ip = _resolve_approved_address(
            host,
            port,
            locality=self._locality,
            allowed_hosts=self._allowed_hosts,
            resolver=self._resolver,
        )
        return await self._inner.connect_tcp(
            ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise EgressDenied("unix sockets are not an allowed gateway egress")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_async_client(
    base_url: str,
    *,
    locality: str = "local",
    allowed_hosts: tuple[str, ...] = (),
    timeout: Any = None,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose every real TCP connection is pinned to a checked address.

    ``trust_env=False``: proxy env vars would route requests through a transport the guard does
    not wrap. ``follow_redirects=False``: a 3xx response is surfaced as an error, never silently
    followed to a host that was never checked (ADR 0154 d2). ``resolver`` exists so a test can
    inject a deterministic stand-in for :func:`socket.getaddrinfo`; production leaves it default.
    """
    import httpx

    transport = httpx.AsyncHTTPTransport(retries=0)
    pool = transport._pool  # type: ignore[attr-defined]
    pool._network_backend = AsyncGuardedBackend(
        pool._network_backend, locality=locality, allowed_hosts=allowed_hosts, resolver=resolver
    )
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        trust_env=False,
        follow_redirects=False,
    )
