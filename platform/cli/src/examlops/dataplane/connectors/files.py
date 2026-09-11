"""Object storage + file URLs (ADR 0130 §5): fsspec for storage, the guarded client for http(s)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from examlops.dataplane.connectors.base import BaseConnector
from examlops.dataplane.safety import (
    _normalize_host,
    check_address,
    guarded_client,
    local_files_allowed,
    redact,
    ssh_auto_add_host_keys,
    url_origin,
)
from examlops.dataplane.types import (
    EgressDenied,
    IncrementalInvalidated,
    LimitExceeded,
    Limits,
    Probe,
    SpecError,
    TableBatch,
    TableInfo,
    Watermark,
)

_SCHEMES = {"s3", "gs", "gcs", "abfs", "az", "sftp", "hdfs", "file", "http", "https"}
# Which URL schemes a source may use with each Named Connection kind this connector accepts. A
# connection's secret is shaped for one kind of endpoint — an S3 key pair for S3, a filesystem
# credential (sftp password, Azure account key, GCS service-account JSON) for a filesystem, a
# bearer token for one http(s) origin — so a source whose URL scheme belongs to another kind is
# refused rather than handing that secret to a protocol and a host it was never issued for (the
# reproduced leak: an `s3` connection's secret key sent as an http Bearer token to whatever host
# `spec.url` named).
#
#   s3  -> s3://
#   fs  -> gs:// gcs:// abfs:// az:// sftp:// hdfs:// file://
#   uri -> http:// https://      (the bearer goes only to the connection's own `uri` origin)
#
# A source with no connection may use any scheme in `_SCHEMES`: it carries no credential. Every
# scheme belongs to exactly one kind, and the kinds are exactly `FilesConnector.connection_kinds`
# (both pinned by tests).
_KIND_SCHEMES: dict[str, frozenset[str]] = {
    "s3": frozenset({"s3"}),
    "fs": frozenset({"gs", "gcs", "abfs", "az", "sftp", "hdfs", "file"}),
    "uri": frozenset({"http", "https"}),
}
# Connection keys naming the one http(s) origin a `uri` connection's bearer token is scoped to.
_ORIGIN_KEYS = ("uri", "url", "base_url")
_FORMATS = {"parquet", "csv", "jsonl", "json"}
_BATCH = 65_536
# Task 22a: live verification on n1 pulled the real PM100 Zenodo record (job_table.parquet, 287
# MB compressed / 454 MB uncompressed, one row group, 231,238 rows, wide list<int32> power-trace
# columns) and the dataplane container was OOM-killed at its 2 GiB limit. Measured peak RSS
# streaming that file through ParquetFile.iter_batches: 1,582 MB with the naive
# `ParquetFile(fh).iter_batches(batch_size=65_536)`; a buffered non-prefetching reader
# (`buffer_size=8<<20, pre_buffer=False`) with `batch_size=8_192, use_threads=False` held it to
# 297 MB. Batch size dominates because the list columns are wide.
_PARQUET_BATCH = 8_192
_DEFAULT_HTTP_TIMEOUT = 30.0
_HTTP_CHUNK = 65_536
_S3_REGION = re.compile(r"[a-z0-9-]+")


def _glob_is_safe(glob: str) -> bool:
    """Reject ``..`` path segments — a source spec must not escape the listed root."""
    return ".." not in PurePosixPath(glob).parts


def _url_error(url: str) -> str | None:
    """Return an error string if ``url`` is not a safe, resolvable fsspec/http URL, else ``None``.

    Three ways a string can *look* like a valid source but actually resolve somewhere the scheme
    check never inspects:

    - a chained fsspec protocol (``simplecache::file://…``) — the outer word is not itself one of
      ``_SCHEMES``, so a naive scheme check never sees the ``file://`` hiding after the ``::``;
    - a schemeless path (``/tmp/x``) — ``urlsplit`` returns an empty scheme, which is not "file",
      so a check that only special-cases the literal ``file`` scheme lets it straight through to
      whatever ``fsspec.core.url_to_fs`` does with a bare path (open the local filesystem);
    - a *bare* colon scheme with no ``//`` authority (``s3:x/y.parquet``, ``hdfs:x``, ``sftp:x``)
      — ``urlsplit`` happily reports an allowed scheme (``s3``), but fsspec's own URL splitter
      only recognises ``scheme://…``; anything else falls through its "no protocol matched" path
      and silently opens a ``LocalFileSystem`` rooted at the current working directory.

    All three are refused here, in one place, so ``validate_spec`` (a list of error strings) and
    ``_check_url`` (raises ``SpecError``) can never drift apart on what counts as a safe URL.
    """
    if "::" in url:
        return "chained fsspec protocols (e.g. 'simplecache::file://...') are not allowed"
    scheme = urlsplit(url).scheme
    if not scheme:
        return "spec.url needs a scheme (e.g. file://, s3://, http://)"
    if scheme not in _SCHEMES:
        return f"spec.url scheme must be one of {sorted(_SCHEMES)}"
    if scheme != "file" and not url.lower().startswith(f"{scheme}://"):
        return (
            f"spec.url must use '{scheme}://' — a bare '{scheme}:' prefix is not a URL fsspec "
            "recognises, and it falls back to reading the local filesystem instead"
        )
    return None


def _refuse_unsafe_fs(fs: Any, scheme: str) -> None:
    """Defence in depth for ``_url_error``/``_check_url``: even if a URL smuggled past every
    syntactic check above, refuse a filesystem this connector must never build through
    ``fsspec.core.url_to_fs`` directly.

    - an *http(s)* filesystem is refused outright, unconditionally — this connector's http(s)
      support (``_http_files``/``probe``'s and ``discover``'s http branches) goes through
      ``safety.guarded_client``/``check_address`` by construction; fsspec's own aiohttp-based
      ``HTTPFileSystem`` resolves DNS and opens sockets its own way, with none of this module's
      DNS-rebinding/redirect/egress-allowlist protection, so it must never be built at all;
    - a *local* filesystem (fsspec's own fallback for anything it can't otherwise resolve) is
      refused unless local files are explicitly allowed.
    """
    from fsspec.implementations.local import LocalFileSystem

    protocol = getattr(fs, "protocol", ())
    protocols = (protocol,) if isinstance(protocol, str) else tuple(protocol)
    if any(p in ("http", "https") for p in protocols):
        raise EgressDenied(
            f"spec.url scheme {scheme!r} resolved to an http(s) filesystem; this connector never "
            "reaches http(s) through fsspec — that would bypass the egress guard entirely"
        )
    is_local = isinstance(fs, LocalFileSystem) or any(p in ("file", "local") for p in protocols)
    if is_local and not local_files_allowed():
        raise SpecError(
            f"spec.url scheme {scheme!r} resolved to a local filesystem, which is disabled; set "
            "EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES=1 only where the service has no access to "
            "platform state"
        )


def _conn_origin(conn: dict[str, Any] | None) -> tuple[str, str, int] | None:
    """The http(s) origin the connection itself configures (``uri``/``url``/``base_url``)."""
    for key in _ORIGIN_KEYS:
        value = (conn or {}).get(key)
        if value:
            return url_origin(str(value))
    return None


def _http_headers(conn: dict[str, Any] | None, url: str) -> dict[str, str]:
    """Auth header shared by ``probe``/``discover`` and ``_http_files`` so they never diverge.

    The bearer is attached only when ``url``'s origin (scheme, host, port) is the origin the
    connection itself configures. ``spec.url`` is source content: without this rule it chose where
    the connection's secret went. A connection that configures no origin sends nothing, and a
    foreign-origin URL is still read — anonymously. Redirects are covered by ``guarded_client``,
    which drops this header on any cross-origin hop.
    """
    secret = (conn or {}).get("secret")
    origin = _conn_origin(conn)
    if not secret or origin is None or url_origin(url) != origin:
        return {}
    return {"Authorization": f"Bearer {secret}"}


def _check_connection_scheme(scheme: str, conn: dict[str, Any] | None) -> None:
    """Refuse a URL scheme that does not belong to the connection's kind (``_KIND_SCHEMES``).

    A connection dict without a ``kind`` (a direct library call; ``resolve_connection`` always
    sets one) is not kind-checked — its secret is still scoped by ``_http_headers`` and the sftp
    host rule, which never look at the kind.
    """
    if not conn or "kind" not in conn:
        return
    kind = str(conn.get("kind") or "")
    allowed = _KIND_SCHEMES.get(kind)
    if allowed is None:
        raise SpecError(
            f"the files connector does not take a {kind!r} connection kind "
            f"(expected one of {sorted(_KIND_SCHEMES)})"
        )
    if scheme not in allowed:
        raise SpecError(
            f"spec.url scheme {scheme!r} does not match the connection kind {kind!r}, which "
            f"serves {sorted(allowed)} — a connection's credential is only used for its own kind"
        )


def _port(value: Any, default: int = 22) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SpecError("an sftp port must be an integer") from None


def _check_sftp_credential_host(conn: dict[str, Any] | None, host: str, kwargs: Any) -> None:
    """The connection's password goes only to the host (and port) the connection itself names.

    The URL wins over the connection for ``host``/``port`` (see ``_storage_options``), so without
    this ``sftp://anywhere/x`` in a source spec chose where a Named Connection's password went.
    When the connection's secret is not what would be sent (no secret, or the URL carries its own
    password) there is nothing to scope.
    """
    cfg = conn or {}
    secret = cfg.get("secret")
    if not secret or kwargs.get("password") != secret:
        return
    own = _normalize_host(str(cfg.get("host") or ""))
    if not own:
        raise SpecError(
            "this connection's secret is an sftp password, so the connection must name its host "
            "(config.host) — a source URL may not choose where the password goes"
        )
    if _normalize_host(host) != own or _port(kwargs.get("port")) != _port(cfg.get("port")):
        raise SpecError(
            "spec.url names an sftp host or port other than the connection's own (config.host / "
            "config.port); the connection's password is only sent to that host"
        )


_GUARDED_SFTP_CLS: type[Any] | None = None


def _guarded_sftp_class() -> type[Any]:
    """Build (once, lazily — paramiko is an optional dependency) fsspec's ``SFTPFileSystem`` with
    a ``_connect`` that is safe to hand a Named Connection's password to.

    fsspec's own ``_connect`` sets ``paramiko.AutoAddPolicy()`` (trust any host key: a
    man-in-the-middle is handed the password) and connects to the name it was given (no egress
    check: ``sftp://minio/`` or a metadata address is reachable). This one:

    - egress-checks the host with ``safety.check_address`` and opens the TCP connection to the
      address that check approved — no second DNS lookup, as for http(s) sources — then hands
      paramiko that socket plus the *name*, which is what known_hosts entries are keyed on;
    - loads the user/system known_hosts and ``EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS`` (optional; an
      unreadable file is an error, not a silent fallback), and uses ``RejectPolicy`` unless
      ``EXAMLOPS_SSH_AUTO_ADD_HOST_KEYS`` is truthy — the platform's existing SSH semantics;
    - never offers the service's own SSH agent or key files to a source's host (a source never
      borrows the service's identity, as the S3 path never borrows its AWS one).

    Keys are loaded with ``load_system_host_keys``, so an auto-added key is never written back to
    an operator's known_hosts file.
    """
    global _GUARDED_SFTP_CLS
    if _GUARDED_SFTP_CLS is not None:
        return _GUARDED_SFTP_CLS

    import socket

    import paramiko
    from fsspec.implementations.sftp import SFTPFileSystem

    class _GuardedSFTPFileSystem(SFTPFileSystem):
        def _connect(self) -> None:
            port = _port(self.ssh_kwargs.get("port"))
            ip = check_address(str(self.host or ""), port)
            client = paramiko.SSHClient()
            client.load_system_host_keys()  # ~/.ssh/known_hosts, if present
            known = os.getenv("EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS", "").strip()
            if known:
                try:
                    client.load_system_host_keys(os.path.expanduser(known))
                except OSError:
                    raise SpecError(
                        "EXAMLOPS_DATAPLANE_SSH_KNOWN_HOSTS names a file that cannot be read"
                    ) from None
            client.set_missing_host_key_policy(
                paramiko.AutoAddPolicy() if ssh_auto_add_host_keys() else paramiko.RejectPolicy()
            )
            reserved = ("port", "sock", "allow_agent", "look_for_keys")
            kwargs = {k: v for k, v in self.ssh_kwargs.items() if k not in reserved}
            timeout = kwargs.get("timeout") or _DEFAULT_HTTP_TIMEOUT
            sock = socket.create_connection((ip, port), timeout=timeout)
            try:
                client.connect(
                    self.host,
                    port=port,
                    sock=sock,
                    allow_agent=False,
                    look_for_keys=False,
                    **kwargs,
                )
                self.client = client
                self.ftp = client.open_sftp()
            except BaseException:
                client.close()
                sock.close()
                raise

    _GUARDED_SFTP_CLS = _GuardedSFTPFileSystem
    return _GUARDED_SFTP_CLS


def _sftp_filesystem(
    url: str, opts: dict[str, Any], conn: dict[str, Any] | None
) -> tuple[Any, str]:
    """``url_to_fs`` for ``sftp://``, building ``_guarded_sftp_class()`` instead of fsspec's own.

    Merges exactly what ``fsspec.core._un_chain`` would (the URL-derived kwargs plus our explicit
    options, disjoint by construction in ``_storage_options``) and returns the same root path.
    """
    from fsspec.utils import infer_storage_options

    kwargs = infer_storage_options(url)
    root = kwargs.pop("path")
    kwargs.pop("protocol", None)
    kwargs.update(opts)
    host = str(kwargs.pop("host", None) or "").strip()
    if not host:
        # `sftp:///x` with no connection host would otherwise reach the local sshd.
        raise SpecError(
            "an sftp source needs a host: sftp://<host>/<path>, or a connection with config.host"
        )
    _check_sftp_credential_host(conn, host, kwargs)
    return _guarded_sftp_class()(host, **kwargs), root


def require_append_only(seen: dict[str, Any], current: dict[str, Any], what: str = "file") -> None:
    """Refuse an incremental read that would not be a pure append (ADR 0130, fix I1).

    ``seen`` is what the parent snapshot already holds (the watermark's ``files`` map: name ->
    fingerprint or checksum), ``current`` what the source lists now. An incremental pull *carries*
    every one of the parent's parts, so a seen entry that changed would be duplicated (old rows
    carried, new rows appended) and a removed one would linger. Either raises
    ``IncrementalInvalidated`` and ``run_pull`` re-reads everything instead. Shared by the files
    and zenodo connectors so the rule cannot drift between them."""
    for name, fingerprint in seen.items():
        if name not in current:
            raise IncrementalInvalidated(
                f"{what} {name!r} was removed since the last snapshot; a full re-read is needed"
            )
        if current[name] != fingerprint:
            raise IncrementalInvalidated(
                f"{what} {name!r} changed since the last snapshot; a full re-read is needed"
            )


def _format_of(path: str, spec: dict[str, Any]) -> str:
    fmt = spec.get("format") or PurePosixPath(path).suffix.lstrip(".").lower()
    if fmt == "ndjson":
        fmt = "jsonl"
    if fmt not in _FORMATS:
        raise SpecError(
            f"cannot tell the format of {path!r}; set spec.format to one of {sorted(_FORMATS)}"
        )
    return fmt


def _fsspec_url(scheme: str, url: str) -> str:
    """Strip a query string from an ``sftp://`` URL before it ever reaches fsspec.

    fsspec's own URL parser (``fsspec.utils.infer_storage_options``) turns a ``?...`` suffix into
    a ``url_query`` kwarg that means nothing to an SSH connection. There is no way to make the
    URL-to-kwargs merge in ``fsspec.core._un_chain`` simply drop a key it derived from the URL —
    see ``_storage_options``'s sftp branch: any key present in *both* the URL-derived kwargs and
    our own explicit options collides with ``TypeError: ... multiple values for keyword
    argument ...``, so the only key we can safely omit from a merge is one the URL never produced
    in the first place. Stripping the query at the source is the only way to keep it out.
    """
    if scheme == "sftp":
        return urlsplit(url)._replace(query="").geturl()
    return url


def _guard_s3_endpoint(conn: dict[str, Any] | None) -> str | None:
    """Egress-check a source connection's S3 endpoint; return the endpoint pyarrow may use.

    ``None`` when the connection names no endpoint: pyarrow then talks to AWS's public S3
    endpoint, which needs no check. Otherwise the host goes through ``check_address`` exactly as an
    http(s) source's does, so a connection cannot aim pyarrow's S3 client at loopback, a metadata
    service or a platform-internal name (``minio``, ``postgres``, …) unless
    ``EXAMLOPS_DATAPLANE_ALLOWED_HOSTS`` names it. Every caller runs this right after
    ``_check_url`` and before any S3 filesystem is built.

    - ``http://``: the result is pinned to the IP ``check_address`` approved, so pyarrow's own
      resolver never looks the name up again — no DNS rebinding between the check and the connect.
    - ``https://``: the hostname is kept, because TLS verifies the certificate against it.
      Residual risk: pyarrow re-resolves the name itself, so an answer that changes between the
      check and the connect (rebinding) is not caught here; the rebound address must still
      present a certificate valid for the name.
    - Redirects (residual, not closable here): pyarrow's S3 client (the AWS C++ SDK) follows
      HTTP 301/307 to any host, and ``pyarrow.fs.S3FileSystem`` has no option to stop it. This
      check covers only the first hop, so a permitted endpoint — and every public host passes
      it — can bounce requests to a host it would refuse. Measured (task 22b fix round 1): a
      307 to an unlisted loopback address was followed for HEAD, and when the HEAD answer carried
      a Content-Length pyarrow also issued the GET there and ingested that body. Only
      allow-list endpoints you trust. The code-level fix is a guarded forward proxy passed as
      ``S3FileSystem(proxy_options=...)`` that re-checks every hop (prototyped in review: it
      stopped the 307 and the internal host got 0 requests) — tracked as a follow-up; until then a
      network-level egress policy is the backstop.

    The platform store (``store.store_from_env``) never comes through here: its endpoint is
    operator configuration.
    """
    from examlops.dataplane.s3 import parse_endpoint

    cfg = conn or {}
    raw = cfg.get("endpoint") or cfg.get("endpoint_url")
    if not raw:
        return None
    try:
        scheme, host, explicit_port = parse_endpoint(str(raw))
    except ValueError as exc:  # the message never echoes the URL
        raise SpecError(f"connection {exc}") from None
    port = explicit_port or (443 if scheme == "https" else 80)
    ip = check_address(host, port)
    if scheme == "https":
        return f"https://{_bracket(host)}" + (f":{explicit_port}" if explicit_port else "")
    return f"http://{_bracket(ip)}:{port}"


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host else host  # an IPv6 literal


def _storage_options(
    scheme: str, conn: dict[str, Any] | None, url: str, *, s3_endpoint: str | None = None
) -> dict[str, Any]:
    cfg = conn or {}
    secret = cfg.get("secret")
    if scheme == "s3":
        key = cfg.get("access_key")
        # No key/secret -> anonymous: a source never borrows the service's own AWS identity.
        opts: dict[str, Any] = {"key": key, "secret": secret, "anon": not (key and secret)}
        # The endpoint comes ONLY from `_guard_s3_endpoint` (egress-checked, http pinned to its
        # IP); a raw connection endpoint must never reach pyarrow — fail closed if one would.
        if s3_endpoint:
            opts["client_kwargs"] = {"endpoint_url": s3_endpoint}
        elif cfg.get("endpoint") or cfg.get("endpoint_url"):
            raise EgressDenied(
                "the S3 connection's endpoint was not egress-checked (_guard_s3_endpoint)"
            )
        region = str(cfg.get("region") or "").strip()
        if region:
            if not _S3_REGION.fullmatch(region):
                raise SpecError(
                    "connection region must be lowercase letters, digits and '-' (e.g. eu-west-1)"
                )
            opts["region"] = region
        return opts
    if scheme in ("abfs", "az"):
        return {"account_name": cfg.get("account_name"), "account_key": secret}
    if scheme == "sftp":
        from fsspec.utils import infer_storage_options

        # `fsspec.implementations.sftp.SFTPFileSystem._get_kwargs_from_urls` is exactly
        # `infer_storage_options(url)` (minus `path`/`protocol`) — mirror it here so we know
        # precisely which keys the URL itself will supply. `fsspec.core._un_chain` merges that
        # dict with these explicit options as `dict(**from_url, **explicit)`; a key present in
        # *both* raises `TypeError` even when the values agree, so any key the URL supplies —
        # `host`, `username`, `password` — must never also appear here, regardless of whether its
        # URL value is truthy. (`sftp://:22/x` derives an empty-string `host`: still a *present*
        # key, not a missing one — checking truthiness instead of membership was the earlier bug
        # in this branch.) A value the URL supplies always wins; the connection's own
        # host/username/secret are only a fallback for what the URL leaves out entirely.
        url_opts = infer_storage_options(_fsspec_url(scheme, url))
        opts = {}
        if "host" not in url_opts:
            opts["host"] = cfg.get("host")
        if "username" not in url_opts:
            opts["username"] = cfg.get("username")
        if "password" not in url_opts:
            opts["password"] = secret
        # The connection's own port, likewise only when the URL names none — so a connection that
        # says `port: 2222` is reached there, and `_check_sftp_credential_host` compares like with
        # like.
        if "port" not in url_opts and cfg.get("port") not in (None, ""):
            opts["port"] = _port(cfg.get("port"))
        return opts
    if scheme in ("gs", "gcs"):
        return {"token": json.loads(secret)} if secret else {}
    return {}


def _open_fs(scheme: str, url: str, conn: dict[str, Any] | None) -> tuple[Any, str]:
    """The one place ``probe`` and ``_list`` build a storage filesystem, so they never diverge.

    ``s3://`` gets its egress-checked endpoint, ``sftp://`` the host-key-verifying, egress-checked
    ``_guarded_sftp_class()``, everything else ``url_to_fs``; ``_refuse_unsafe_fs`` runs on all.
    Callers have already passed the URL through ``_check_url``.
    """
    from examlops.dataplane.s3 import url_to_fs  # s3:// via pyarrow, the rest via fsspec

    s3_endpoint = _guard_s3_endpoint(conn) if scheme == "s3" else None
    fsspec_url = _fsspec_url(scheme, url)
    opts = _storage_options(scheme, conn, url, s3_endpoint=s3_endpoint)
    if scheme == "sftp":
        fs, root = _sftp_filesystem(fsspec_url, opts, conn)
    else:
        fs, root = url_to_fs(fsspec_url, **opts)
    _refuse_unsafe_fs(fs, scheme)
    return fs, root


def _batches(fh: Any, fmt: str) -> Iterator[Any]:
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import pyarrow.json as pajson
    import pyarrow.parquet as pq

    if fmt == "parquet":
        # See _PARQUET_BATCH above: a buffered, non-prefetching reader plus a small batch size is
        # what keeps memory bounded on a file with wide list columns and one large row group.
        reader = pq.ParquetFile(fh, buffer_size=8 << 20, pre_buffer=False)
        yield from reader.iter_batches(batch_size=_PARQUET_BATCH, use_threads=False)
    elif fmt == "csv":
        yield from pacsv.open_csv(fh)
    elif fmt == "jsonl":
        if hasattr(pajson, "open_json"):
            # Streaming reader: batches arrive incrementally instead of `read_json` buffering the
            # whole file into one Table before any batch is ever yielded.
            yield from pajson.open_json(fh)
        else:  # older pyarrow without a streaming JSON reader
            yield from pajson.read_json(fh).to_batches(_BATCH)
    else:
        # A single JSON document (object or array) is whole-document by nature — pyarrow has no
        # streaming reader for a bare JSON array, so this branch stays a single `json.load`.
        data = json.load(io.TextIOWrapper(fh, encoding="utf-8"))
        records = data if isinstance(data, list) else [data]
        yield from pa.Table.from_pylist(records).to_batches(_BATCH)


class FilesConnector(BaseConnector):
    kind = "files"
    connection_kinds = ("s3", "fs", "uri")
    extra = "dataplane-files"
    requires = ("fsspec", "pyarrow")
    connection_required = False
    supports_incremental = True

    def validate_spec(self, spec: dict[str, Any]) -> list[str]:
        errors = []
        url = spec.get("url")
        if not url:
            errors.append("spec.url is required")
        else:
            err = _url_error(url)
            if err:
                errors.append(err)
        if spec.get("format") and spec["format"] not in _FORMATS | {"ndjson"}:
            errors.append(f"spec.format must be one of {sorted(_FORMATS)}")
        glob = spec.get("glob")
        if glob and not _glob_is_safe(glob):
            errors.append("spec.glob must not contain '..' path segments")
        return errors

    def _check_url(self, url: str, conn: dict[str, Any] | None = None) -> str:
        """Gate every entry point (``read``/``probe``/``discover``/``_list``) against reaching
        local disk unless explicitly enabled — the service container must not read platform
        state (ADR 0130 §10). See ``_url_error`` for the three shapes this refuses. With a
        connection, the scheme must also belong to the connection's kind (``_KIND_SCHEMES``).
        """
        err = _url_error(url)
        if err:
            raise SpecError(err)
        scheme = urlsplit(url).scheme
        _check_connection_scheme(scheme, conn)
        if scheme == "file" and not local_files_allowed():
            raise SpecError(
                "local files are disabled in the dataplane; set "
                "EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES=1 only where the service has no access to "
                "platform state"
            )
        return scheme

    def _http_files(
        self, url: str, conn: dict[str, Any] | None, limits: Limits
    ) -> Iterator[tuple[str, str, Any]]:
        parts = urlsplit(url)
        check_address(parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80))
        timeout = limits.max_seconds or _DEFAULT_HTTP_TIMEOUT
        with (
            guarded_client(headers=_http_headers(conn, url), timeout=timeout) as client,
            tempfile.TemporaryFile() as tmp,
        ):
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                hasher = hashlib.sha256()
                total = 0
                for chunk in resp.iter_bytes(chunk_size=_HTTP_CHUNK):
                    total += len(chunk)
                    if limits.max_bytes is not None and total > limits.max_bytes:
                        raise LimitExceeded(
                            f"http download of {parts.path!r} exceeded max_bytes={limits.max_bytes}"
                        )
                    hasher.update(chunk)
                    tmp.write(chunk)
            tmp.seek(0)
            # A validator header, when the server sends one, is cheaper to compare than a hash
            # over the whole body; falling back to the content hash (rather than "" when neither
            # header is present) is what makes an unchanged download compare equal and a changed
            # one compare different — a static "" fingerprint made every incremental http pull
            # look "unchanged" after the first.
            fingerprint = (
                resp.headers.get("etag")
                or resp.headers.get("last-modified")
                or f"sha256:{hasher.hexdigest()}"
            )
            yield PurePosixPath(parts.path).name, fingerprint, tmp

    def _guarded_head(self, conn: dict[str, Any] | None, url: str) -> Any:
        """A guarded HEAD, shared by ``probe`` and ``discover`` for their http(s) branches."""
        parts = urlsplit(url)
        check_address(parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80))
        with guarded_client(headers=_http_headers(conn, url)) as client:
            return client.head(url)

    def probe(self, conn: dict[str, Any] | None, spec: dict[str, Any] | None = None) -> Probe:
        url = (spec or {}).get("url") or (conn or {}).get("uri") or (conn or {}).get("url")
        if not url:
            return Probe(False, "no url to probe")
        try:
            scheme = self._check_url(url, conn)
            if scheme in ("http", "https"):
                resp = self._guarded_head(conn, url)
                return Probe(resp.status_code < 400, f"HTTP {resp.status_code}")
            fs, root = _open_fs(scheme, url, conn)
            return Probe(
                bool(fs.exists(root)), "reachable" if fs.exists(root) else "path not found"
            )
        except Exception as exc:
            return Probe(
                False,
                redact(f"{type(exc).__name__}: {exc}", secrets=[(conn or {}).get("secret") or ""]),
            )

    def discover(self, conn: dict[str, Any] | None, spec: dict[str, Any]) -> list[TableInfo]:
        url = spec.get("url") or ""
        if url and self._check_url(url, conn) in ("http", "https"):
            # An http(s) source is exactly one file — discover it the same guarded way `read()`
            # does (`_http_files`/`probe`), never through `_list`'s fsspec path (see
            # `_refuse_unsafe_fs`: fsspec's `HTTPFileSystem` bypasses the egress guard entirely).
            resp = self._guarded_head(conn, url)
            resp.raise_for_status()
            name = PurePosixPath(urlsplit(url).path).name or url
            table = spec.get("table") or PurePosixPath(name).stem or name
            return [TableInfo(table, url)]
        return [TableInfo(PurePosixPath(p).stem, p) for p, _, _ in self._list(conn, spec)][:500]

    def _list(
        self, conn: dict[str, Any] | None, spec: dict[str, Any]
    ) -> list[tuple[str, str, Any]]:
        url = spec["url"]
        scheme = self._check_url(url, conn)
        if scheme in ("http", "https"):
            # Defence in depth: `discover()` is the only internal caller and it branches away
            # from `_list` for http(s) before ever reaching here (see above); refuse outright so
            # a future caller can't accidentally hand an http(s) URL to fsspec through this path.
            raise EgressDenied(
                "http(s) sources are read directly by this connector (or discovered via a "
                "guarded HEAD in discover()); they are never listed through fsspec"
            )
        glob = spec.get("glob") or "*"
        if not _glob_is_safe(glob):
            raise SpecError("spec.glob must not contain '..' path segments")
        fs, root = _open_fs(scheme, url, conn)
        pattern = f"{root.rstrip('/')}/{glob}" if fs.isdir(root) else root
        out = []
        # `detail=True` returns each match's info from the listing itself — no extra HEAD/stat
        # per matched object (fsspec's AbstractFileSystem.glob, which ArrowFSWrapper inherits).
        matches = fs.glob(pattern, detail=True)
        for path in sorted(matches):
            info = matches[path]
            fp = str(
                info.get("ETag")
                or info.get("etag")
                or info.get("mtime")
                or info.get("LastModified")
                or ""
            )
            out.append((path, f"{fp}:{info.get('size')}", fs))
        return out

    def read(
        self,
        conn: dict[str, Any] | None,
        spec: dict[str, Any],
        since: Watermark | None,
        limits: Limits,
    ) -> Iterator[TableBatch]:
        """Read every matched file; incrementally (``since`` given), only files not seen before.

        Incremental means **append new files only**. A file the parent snapshot already holds
        that changed (fingerprint: ETag/mtime + size) or disappeared raises
        ``IncrementalInvalidated`` — ``run_pull`` then re-reads everything — because the parent's
        copy of it would otherwise be carried alongside the new one.
        """
        seen: dict[str, str] = dict((since or {}).get("files") or {})
        url = spec["url"]
        if self._check_url(url, conn) in ("http", "https"):
            for name, fingerprint, fh in self._http_files(url, conn, limits):
                if since is not None and name in seen:
                    # One URL is one file: unchanged -> nothing new; changed -> not an append.
                    require_append_only({name: seen[name]}, {name: fingerprint})
                    return
                seen[name] = fingerprint
                table = spec.get("table") or PurePosixPath(name).stem
                for batch in _batches(fh, _format_of(name, spec)):
                    yield TableBatch(table, batch, {"files": dict(seen)})
            return
        listing = self._list(conn, spec)
        if since is not None:
            require_append_only(seen, {path: fp for path, fp, _ in listing})
        for path, fingerprint, fs in listing:
            if since is not None and path in seen:
                continue  # already in the parent snapshot, verified unchanged above
            seen[path] = fingerprint
            table = spec.get("table") or PurePosixPath(path).stem
            with fs.open(path, "rb") as fh:
                for batch in _batches(fh, _format_of(path, spec)):
                    yield TableBatch(table, batch, {"files": dict(seen)})
