"""Egress policy, guarded HTTP, redaction and name validation for the dataplane (ADR 0130 §10).

An http-based connector running inside the stack can reach ``mlflow:5000``, MinIO, or a cloud
metadata endpoint. Checking the URL is not enough: the client resolves DNS again when it connects
(DNS rebinding). The guard therefore sits in the network backend: it resolves once, checks every
address, and connects to the checked address — never a second lookup. It also has to survive an
HTTP redirect: a redirect can retarget the connection to a denied address, take a caller's secret
header cross-origin, or silently downgrade https to http.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore

from examlops.dataplane.types import EgressDenied, SpecError

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# Compose service names (every service in platform/infra/docker-compose/docker-compose.yml) and
# well-known internal endpoints. A source may reach one only when an operator allow-lists it
# explicitly. Matched after stripping a trailing dot and brackets and lower-casing, and a name
# starting with the compose project's "examlops-" container-name prefix is treated the same way —
# both are just other spellings of the same internal service.
_INTERNAL_NAMES = frozenset(
    {
        "localhost",
        "mlflow",
        "minio",
        "minio-init",
        "postgres",
        "orchestrator",
        "control-plane",
        "dashboard",
        "agent",
        "ray-serving",
        "prometheus",
        "grafana",
        "loki",
        "tempo",
        "alertmanager",
        "jupyterhub",
        "docker-socket-proxy",
        "dataplane",
        "promtail",
        "seanerbus-bridge",
        "backup",
        "nats",
        "vllm",
        "metadata.google.internal",
        "metadata",
    }
)
_INTERNAL_PREFIX = "examlops-"  # the Compose `container_name:` prefix of every service above

_KEY_ALT = ("token", "secret", "key", "password", "passwd", "signature", "sig", "credential")
_MAX_KEY_LEN = 64
# `k=v` / `k: v` (query strings, config text, header-less "key: value" dumps). The key run is
# *bounded* (`{1,64}`, no alternation inside the repeated group) rather than the previous
# `[\w-]*(alt1|alt2|...)[\w-]*` shape: two unbounded stars around an alternation is catastrophic
# backtracking on a long run of key-shaped characters with no `=`/`:` in sight (measured: ~70s on
# an 8 KB adversarial string) — exactly what a hostile connector's own error text can hand to
# `redact()` while a pull lock is held. Whether the captured key is secret-shaped is decided in
# `_redact_kv` (`_is_secret_key`, plain substring checks — O(len(key)), not regex).
# The colon branch excludes `://` — without it, `https://...` itself reads as a `k:v` pair
# (key "https", value "//..."), and that value swallows everything up to the next whitespace/`&`/`;`
# — including a real `token=abc` further along the same string — before the per-key check ever
# gets a chance to look at it.
#
# The pattern matches only the key and separator; the trailing lookahead requires a value to
# follow but does not consume it. The value (`_KV_VALUE`, greedy and unbounded — a value can be an
# arbitrary token) is matched separately, and only for a secret-shaped key. Consuming it inside
# this pattern made `_redact_kv` quadratic: a non-secret key's value was matched in full, then —
# so a secret nested in it is still found — thrown away and rescanned from just after the key, so
# on separator-dense text with no whitespace (`a:a:a:...`) every value was re-matched from nearly
# every position (measured ~6 s at 100 KB).
_SECRET_KV = re.compile(
    rf"(?<![A-Za-z0-9])([\w-]{{1,{_MAX_KEY_LEN}}})(\s*(?::(?!//)|=)\s*)(?=[^&\s;])"
)
_KV_VALUE = re.compile(r"[^&\s;]+")
# `"k": "v"` (JSON bodies / log dumps) — quotes bound the key on both sides already, so this stays
# a single non-alternating bounded run too, for the same reason.
_SECRET_JSON = re.compile(rf'"([\w-]{{1,{_MAX_KEY_LEN}}})"\s*:\s*"([^"]*)"')


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(alt in lowered for alt in _KEY_ALT)


# `Authorization: Bearer ...` / `X-Api-Key: ...` style headers dumped into an error string.
_SECRET_HEADER = re.compile(
    r"(?i)\b(authorization|x-api-key|api-key|proxy-authorization)\s*:\s*\S+(?:\s+\S+)?"
)
# URL userinfo: everything between `scheme://` and the LAST `@` before the host — the last `@`
# before the next whitespace/`?`/`#`, which is what keeps this correct when the password itself
# contains `/` or `@`. `_redact_userinfo` anchors on each literal `://` and looks *backwards* for
# the scheme, rather than scanning forward for a scheme start: a forward regex either tries every
# character of a long scheme-charset run as a start (quadratic on `aaaa...` or a 100 KB
# `-key-key...`) or, with a lookbehind forcing the scheme to begin at a run boundary, stops
# recognising `1https://`, `-https://`, `...https://` or a >33-letter run before `https://` as a
# URL at all — and leaks the password. The userinfo is deliberately *not* length-capped: the old
# 2048-character cap existed only to bound regex backtracking, and it left a longer password (a
# JWT, say) unmasked; the scan below is linear without it.
_MAX_SCHEME_LEN = 32  # characters after the scheme's first letter; no real scheme is near this
# The scheme is the longest valid one ending at `://`: searched in a window of at most
# `1 + _MAX_SCHEME_LEN` characters (so O(1) work per `://`), `\Z` pinned to the `://` via endpos.
_SCHEME_TAIL = re.compile(rf"(?i)[a-z][a-z0-9+.-]{{0,{_MAX_SCHEME_LEN}}}\Z")
_USERINFO_STOP = re.compile(r"[\s?#]")


def validate_name(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name) or ".." in name:
        raise SpecError(f"invalid {what} {name!r}: use 1-128 of A-Z a-z 0-9 . _ - (no slashes)")
    return name


def allowed_hosts() -> frozenset[str]:
    raw = os.getenv("EXAMLOPS_DATAPLANE_ALLOWED_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def local_files_allowed() -> bool:
    return os.getenv("EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def ssh_auto_add_host_keys() -> bool:
    """Whether an SSH connection may trust an unknown host key (insecure; explicit opt-in only).

    The same variable and the same truthy spellings as the HPC SSH executor
    (``platform/infra/slurm-adapter/executor.py``): unset means paramiko's ``RejectPolicy``.
    """
    return os.getenv("EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _normalize_host(host: str) -> str:
    """Lower-case, drop IPv6 literal brackets, and drop a trailing FQDN dot.

    ``mlflow`` and ``mlflow.`` and ``MLFLOW.`` are the same platform-internal name; comparing the
    raw string against ``_INTERNAL_NAMES`` let the dotted/upper-cased spellings through.
    """
    return (host or "").strip().strip("[]").lower().rstrip(".")


def url_origin(url: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of an http(s) URL — the unit a credential is scoped to — else None.

    The host is normalized like ``check_address`` normalizes it and the port defaults per scheme,
    so ``https://Zenodo.org.`` and ``https://zenodo.org:443/x`` are one origin. Anything that is
    not a parseable http(s) URL with a host has no origin, and is therefore never "the same
    origin" as anything — a caller that attaches a credential only on a match fails closed.
    """
    try:
        parts = urlsplit(str(url).strip())
        port = parts.port
    except ValueError:  # an out-of-range or non-numeric port
        return None
    scheme = parts.scheme.lower()
    host = _normalize_host(parts.hostname or "")
    if scheme not in ("http", "https") or not host:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def same_origin(a: str, b: str) -> bool:
    """True only when both URLs have an http(s) origin (``url_origin``) and it is the same one."""
    origin = url_origin(a)
    return origin is not None and origin == url_origin(b)


def _in_allowed_cidr(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allow: frozenset[str]
) -> bool:
    for entry in allow:
        if "/" in entry:
            try:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
    return False


def _is_denied_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` — or the IPv4 address it embeds, for an IPv4-mapped IPv6 address — is not a
    routable public address.

    ``is_global`` is the load-bearing check here: it (unlike ``is_private``/``is_reserved``) also
    excludes the shared/carrier-grade-NAT range ``100.64.0.0/10`` (RFC 6598), which is where a
    cloud metadata endpoint such as Alibaba's ``100.100.100.200`` lives. The older
    private/loopback/link-local/reserved/multicast/unspecified checks are kept too, since
    ``is_global`` alone still admits some ranges (e.g. multicast) this guard also wants to deny.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    for candidate in (ip, mapped) if mapped is not None else (ip,):
        if (
            not candidate.is_global
            or candidate.is_private
            or candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_reserved
            or candidate.is_multicast
            or candidate.is_unspecified
        ):
            return True
    return False


def check_address(host: str, port: int, *, resolver: Any = socket.getaddrinfo) -> str:
    """Resolve ``host`` once, check every address, return the first allowed one."""
    name = _normalize_host(host)
    allow = allowed_hosts()
    if not name:
        raise EgressDenied("empty host")
    explicitly_allowed = name in allow
    # The container-name prefix only ever applies to a single-label compose hostname
    # (`examlops-mlflow`); a multi-label public FQDN that merely starts with the same prefix
    # (`examlops-docs.example.org`) is not a compose container and must not be denied as one.
    is_internal = name in _INTERNAL_NAMES or ("." not in name and name.startswith(_INTERNAL_PREFIX))
    if is_internal and not explicitly_allowed:
        raise EgressDenied(
            f"{host} is a platform-internal name; add it to EXAMLOPS_DATAPLANE_ALLOWED_HOSTS "
            "to use it as a source"
        )
    infos = resolver(name, port, proto=socket.IPPROTO_TCP)
    if not infos:
        raise EgressDenied(f"{host} did not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if explicitly_allowed or _in_allowed_cidr(ip, allow):
            continue
        if _is_denied_ip(ip):
            raise EgressDenied(
                f"{host} resolves to {ip} (non-public); allow-list the host or CIDR in "
                "EXAMLOPS_DATAPLANE_ALLOWED_HOSTS"
            )
    return str(infos[0][4][0])


class _GuardedBackend(httpcore.NetworkBackend):
    """httpcore network backend that connects only to an address ``check_address`` approved.

    TLS is unaffected: httpcore passes the URL's host as ``server_hostname`` when it starts TLS on
    the returned stream, so SNI and certificate checks still use the name, not the IP.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        ip = check_address(host, port, resolver=socket.getaddrinfo)
        return self._inner.connect_tcp(
            ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(self, *a: Any, **kw: Any) -> Any:
        raise EgressDenied("unix sockets are not an allowed dataplane egress")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def _origin(url: Any) -> tuple[str, str | None, int | None]:
    port = url.port
    if port is None:
        port = {"http": 80, "https": 443}.get(url.scheme)
    return (url.scheme, url.host, port)


_GUARDED_CLIENT_CLS: type[Any] | None = None


def _guarded_client_class() -> type[Any]:
    """Build (once, lazily) an ``httpx.Client`` subclass safe to follow a redirect with.

    A plain ``httpx.Client`` only strips ``Authorization`` on a cross-origin redirect; a caller's
    own secret header (``X-Api-Key``, a custom bearer header, ...) rides along to whatever the
    redirect target is. This subclass drops every header **the caller supplied to
    ``guarded_client``** on a cross-origin hop, and refuses an https-to-http downgrade redirect
    outright (a redirect is exactly how a compromised/malicious server would demote a request off
    TLS to read the credentials in flight).
    """
    global _GUARDED_CLIENT_CLS
    if _GUARDED_CLIENT_CLS is not None:
        return _GUARDED_CLIENT_CLS

    import httpx

    class _GuardedClient(httpx.Client):
        _caller_header_names: frozenset[str] = frozenset()

        def _redirect_headers(self, request: Any, url: Any, method: str) -> Any:
            headers = super()._redirect_headers(request, url, method)
            if request.url.scheme == "https" and url.scheme == "http":
                raise EgressDenied(f"refusing an https-to-http downgrade redirect to {url.host!r}")
            if _origin(url) != _origin(request.url):
                for name in self._caller_header_names:
                    headers.pop(name, None)
            return headers

    _GUARDED_CLIENT_CLS = _GuardedClient
    return _GUARDED_CLIENT_CLS


def guarded_client(*, timeout: float = 30.0, headers: dict[str, str] | None = None) -> Any:
    """An ``httpx.Client`` whose every TCP connection passes the egress guard.

    ``trust_env=False``: proxy env vars would route requests through a transport the guard does
    not wrap. The private-attribute wiring is pinned by a test so an httpx upgrade fails loudly.
    """
    import httpx

    headers = dict(headers or {})
    transport = httpx.HTTPTransport(retries=0)
    pool = transport._pool  # type: ignore[attr-defined]
    pool._network_backend = _GuardedBackend(pool._network_backend)
    client_cls = _guarded_client_class()
    client = client_cls(
        transport=transport,
        timeout=timeout,
        headers=headers,
        follow_redirects=True,
        trust_env=False,
    )
    client._caller_header_names = frozenset(k.lower() for k in headers)
    return client


def _redact_json_match(match: re.Match[str]) -> str:
    key = match.group(1)
    return f'"{key}": "***"' if _is_secret_key(key) else match.group(0)


def _redact_kv(text: str) -> str:
    """Redact ``k=v``/``k: v`` pairs without a non-secret match hiding a secret nested in its value.

    A value runs to the next whitespace/``&``/``;``, so the value of a *non-secret* key such as
    ``dataplane:8080/v1/pull?token=abc`` contains the real secret ``token=abc``. Consuming that
    value as one span (``re.sub``) would never look inside it again. So ``_SECRET_KV`` matches only
    key + separator: a secret key's value is then matched, masked, and skipped (nothing left to
    find there); a non-secret key's value is never consumed — scanning simply continues right
    after its separator, and finds any secret nested in it. Every character is examined by a
    bounded number of match attempts, so this is linear in ``len(text)``.
    """
    out: list[str] = []
    pos = 0
    end = len(text)
    while pos < end:
        match = _SECRET_KV.search(text, pos)
        if match is None:
            break
        out.append(text[pos : match.end()])
        pos = match.end()
        if _is_secret_key(match.group(1)):
            value = _KV_VALUE.match(text, pos)
            out.append("***")
            if value is not None:  # always, given the pattern's lookahead
                pos = value.end()
    out.append(text[pos:])
    return "".join(out)


def _redact_userinfo(text: str) -> str:
    """Mask URL userinfo (``scheme://user:pass@host`` -> ``scheme://***@host``) in linear time.

    For each ``://`` not already inside a masked span: if a valid scheme ends there, the userinfo
    is everything up to the last ``@`` before the next whitespace/``?``/``#``. The next stop
    character and the next ``@`` are cached across ``://`` occurrences, and a backward ``@`` search
    only ever covers text that has no ``@`` a later search could need, so no stretch of text is
    scanned more than a bounded number of times however densely ``://`` repeats.
    """
    out: list[str] = []
    pos = 0  # text[:pos] is already emitted; a scheme may not start before it
    find_from = 0
    stop = -1  # position of the first stop character at/after the current userinfo start
    next_at = -1  # position of the first `@` at/after the current userinfo start
    size = len(text)
    while True:
        sep = text.find("://", find_from)
        if sep < 0:
            break
        find_from = sep + 3
        if _SCHEME_TAIL.search(text, max(pos, sep - _MAX_SCHEME_LEN - 1), sep) is None:
            continue
        start = sep + 3
        if stop < start:
            found = _USERINFO_STOP.search(text, start)
            stop = found.start() if found is not None else size
        if next_at < start:
            next_at = text.find("@", start)
            if next_at < 0:
                next_at = size
        if next_at >= stop:  # no `@` before the host terminator: no userinfo here
            continue
        at = text.rfind("@", start, stop)
        out.append(text[pos:start])
        out.append("***@")
        pos = find_from = at + 1
    out.append(text[pos:])
    return "".join(out)


def redact(text: str, *, secrets: Iterable[str] = ()) -> str:
    out = _redact_userinfo(str(text))
    out = _SECRET_HEADER.sub(lambda m: f"{m.group(1)}: ***", out)
    out = _SECRET_JSON.sub(_redact_json_match, out)
    out = _redact_kv(out)
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out
