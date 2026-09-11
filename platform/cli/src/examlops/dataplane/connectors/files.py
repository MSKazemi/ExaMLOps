"""Object storage + file URLs (ADR 0130 §5): fsspec for storage, the guarded client for http(s)."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from examlops.dataplane.connectors.base import BaseConnector
from examlops.dataplane.safety import check_address, guarded_client, local_files_allowed, redact
from examlops.dataplane.types import (
    EgressDenied,
    LimitExceeded,
    Limits,
    Probe,
    SpecError,
    TableBatch,
    TableInfo,
    Watermark,
)

_SCHEMES = {"s3", "gs", "gcs", "abfs", "az", "sftp", "hdfs", "file", "http", "https"}
_FORMATS = {"parquet", "csv", "jsonl", "json"}
_BATCH = 65_536
_DEFAULT_HTTP_TIMEOUT = 30.0
_HTTP_CHUNK = 65_536


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


def _http_headers(conn: dict[str, Any] | None) -> dict[str, str]:
    """Auth header shared by ``probe``/``discover`` and ``_http_files`` so they never diverge."""
    if conn and conn.get("secret"):
        return {"Authorization": f"Bearer {conn['secret']}"}
    return {}


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


def _storage_options(scheme: str, conn: dict[str, Any] | None, url: str) -> dict[str, Any]:
    cfg = conn or {}
    secret = cfg.get("secret")
    if scheme == "s3":
        opts: dict[str, Any] = {"key": cfg.get("access_key"), "secret": secret}
        if cfg.get("endpoint") or cfg.get("endpoint_url"):
            opts["client_kwargs"] = {"endpoint_url": cfg.get("endpoint") or cfg.get("endpoint_url")}
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
        return opts
    if scheme in ("gs", "gcs"):
        return {"token": json.loads(secret)} if secret else {}
    return {}


def _batches(fh: Any, fmt: str) -> Iterator[Any]:
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import pyarrow.json as pajson
    import pyarrow.parquet as pq

    if fmt == "parquet":
        yield from pq.ParquetFile(fh).iter_batches(batch_size=_BATCH)
    elif fmt == "csv":
        yield from pacsv.open_csv(fh)
    elif fmt == "jsonl":
        yield from pajson.read_json(fh).to_batches(_BATCH)
    else:
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

    def _check_url(self, url: str) -> str:
        """Gate every entry point (``read``/``probe``/``discover``/``_list``) against reaching
        local disk unless explicitly enabled — the service container must not read platform
        state (ADR 0130 §10). See ``_url_error`` for the three shapes this refuses.
        """
        err = _url_error(url)
        if err:
            raise SpecError(err)
        scheme = urlsplit(url).scheme
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
            guarded_client(headers=_http_headers(conn), timeout=timeout) as client,
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
        with guarded_client(headers=_http_headers(conn)) as client:
            return client.head(url)

    def probe(self, conn: dict[str, Any] | None, spec: dict[str, Any] | None = None) -> Probe:
        url = (spec or {}).get("url") or (conn or {}).get("uri") or (conn or {}).get("url")
        if not url:
            return Probe(False, "no url to probe")
        try:
            scheme = self._check_url(url)
            if scheme in ("http", "https"):
                resp = self._guarded_head(conn, url)
                return Probe(resp.status_code < 400, f"HTTP {resp.status_code}")
            import fsspec

            fsspec_url = _fsspec_url(scheme, url)
            fs, root = fsspec.core.url_to_fs(fsspec_url, **_storage_options(scheme, conn, url))
            _refuse_unsafe_fs(fs, scheme)
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
        if url and self._check_url(url) in ("http", "https"):
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
        import fsspec

        url = spec["url"]
        scheme = self._check_url(url)
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
        fsspec_url = _fsspec_url(scheme, url)
        fs, root = fsspec.core.url_to_fs(fsspec_url, **_storage_options(scheme, conn, url))
        _refuse_unsafe_fs(fs, scheme)
        pattern = f"{root.rstrip('/')}/{glob}" if fs.isdir(root) else root
        out = []
        for path in sorted(fs.glob(pattern)):
            info = fs.info(path)
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
        seen: dict[str, str] = dict((since or {}).get("files") or {})
        url = spec["url"]
        if self._check_url(url) in ("http", "https"):
            for name, fingerprint, fh in self._http_files(url, conn, limits):
                if seen.get(name) == fingerprint and since is not None:
                    return
                seen[name] = fingerprint
                table = spec.get("table") or PurePosixPath(name).stem
                for batch in _batches(fh, _format_of(name, spec)):
                    yield TableBatch(table, batch, {"files": dict(seen)})
            return
        for path, fingerprint, fs in self._list(conn, spec):
            if since is not None and seen.get(path) == fingerprint:
                continue
            seen[path] = fingerprint
            table = spec.get("table") or PurePosixPath(path).stem
            with fs.open(path, "rb") as fh:
                for batch in _batches(fh, _format_of(path, spec)):
                    yield TableBatch(table, batch, {"files": dict(seen)})
