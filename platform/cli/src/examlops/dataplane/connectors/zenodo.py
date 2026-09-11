"""Zenodo connector (ADR 0130 §5): any record id, md5-verified downloads."""

from __future__ import annotations

import fnmatch
import hashlib
import tempfile
from collections.abc import Iterator
from typing import Any

from examlops.dataplane.connectors.base import BaseConnector
from examlops.dataplane.connectors.files import _batches, _format_of, require_append_only
from examlops.dataplane.safety import (
    guarded_client,
    redact,
    same_origin,
    url_origin,
    validate_name,
)
from examlops.dataplane.types import (
    DataplaneError,
    LimitExceeded,
    Limits,
    Probe,
    SpecError,
    TableBatch,
    TableInfo,
    Watermark,
)

_client_factory = guarded_client
_DEFAULT_BASE = "https://zenodo.org"
_DEFAULT_HTTP_TIMEOUT = 30.0
_HTTP_CHUNK = 65_536


def _file_url(entry: dict[str, Any]) -> str:
    links = entry.get("links") or {}
    return str(links.get("content") or links.get("self") or links.get("download") or "")


def _hasher(algo: str, key: str) -> Any:
    """``hashlib.new`` for a Zenodo checksum's algorithm, with a friendly error on an unknown one.

    ``usedforsecurity=False`` is passed for md5 specifically — Zenodo checksums are integrity
    checks, not a security boundary, and a FIPS-mode OpenSSL otherwise refuses to construct md5 at
    all.
    """
    try:
        if algo == "md5":
            return hashlib.new("md5", usedforsecurity=False)
        return hashlib.new(algo)
    except ValueError as exc:
        raise DataplaneError(
            f"unsupported checksum algorithm {algo!r} for {key} (record file checksum)"
        ) from exc


def _checksum(entry: dict[str, Any], record: Any) -> tuple[str, str]:
    """Parse a file entry's ``"algo:digest"`` checksum, failing closed.

    A missing, empty or malformed checksum must not silently skip verification — every
    downloaded file is checksum-verified, so a file this can't parse a checksum for is refused
    *before* it is downloaded, rather than streamed unverified.
    """
    algo, sep, digest = str(entry.get("checksum") or "").partition(":")
    if not sep or not algo or not digest:
        raise DataplaneError(
            f"file {entry.get('key')!r} in record {record} has no usable checksum "
            "(expected 'algo:digest'); refusing to download unverified"
        )
    return algo, digest


def _table_name(key: str, spec: dict[str, Any]) -> str:
    """The dataplane table name for one Zenodo file: ``spec.table`` if given, else the file stem.

    Reuses ``safety.validate_name`` (the one implementation of the dataplane naming rule) rather
    than re-deriving the character class, but replaces its error with one that names the offending
    file and points at the escape hatch.
    """
    if spec.get("table"):
        return str(spec["table"])
    stem = key.rsplit(".", 1)[0]
    try:
        return validate_name(stem, "table name")
    except SpecError as exc:
        raise SpecError(
            f"cannot derive a table name from file {key!r} ({exc}); set spec.table"
        ) from exc


class ZenodoConnector(BaseConnector):
    kind = "zenodo"
    connection_kinds = ("zenodo",)
    extra = "dataplane-files"
    requires = ("httpx", "pyarrow")
    required_spec = ("record",)
    connection_required = False
    supports_incremental = True

    def _base(self, conn: dict[str, Any] | None, spec: dict[str, Any]) -> str:
        """The API base URL: the connection's, else ``spec.base_url``, else Zenodo itself.

        A connection that carries a token scopes it to its own server — its ``base_url``, default
        ``https://zenodo.org`` — so a ``spec.base_url`` on any other origin is refused rather than
        silently receiving (or silently ignoring) the token. A token-less source may name any
        Zenodo-compatible server: it sends nothing but the request.
        """
        cfg = conn or {}
        requested = spec.get("base_url")
        if cfg.get("secret") and requested:
            own = str(cfg.get("base_url") or _DEFAULT_BASE)
            if not same_origin(str(requested), own):
                raise SpecError(
                    "spec.base_url must have the same origin as the connection's base_url "
                    f"(default {_DEFAULT_BASE}): the connection's token is only sent to its own "
                    "server"
                )
        return str(cfg.get("base_url") or requested or _DEFAULT_BASE).rstrip("/")

    def _client(self, conn: dict[str, Any] | None, limits: Limits | None = None) -> Any:
        headers = (
            {"Authorization": f"Bearer {conn['secret']}"} if conn and conn.get("secret") else {}
        )
        timeout = (limits.max_seconds if limits else None) or _DEFAULT_HTTP_TIMEOUT
        return _client_factory(headers=headers, timeout=timeout)

    def _record(self, client: Any, base: str, record: Any) -> dict[str, Any]:
        resp = client.get(f"{base}/api/records/{record}")
        resp.raise_for_status()
        return dict(resp.json())

    def probe(self, conn: dict[str, Any] | None, spec: dict[str, Any] | None = None) -> Probe:
        spec = spec or {}
        try:
            with self._client(conn) as client:
                if spec.get("record"):
                    rec = self._record(client, self._base(conn, spec), spec["record"])
                    return Probe(
                        True, f"record {rec.get('id')} with {len(rec.get('files') or [])} files"
                    )
                resp = client.get(f"{self._base(conn, spec)}/api/records", params={"size": 1})
                return Probe(resp.status_code < 400, f"HTTP {resp.status_code}")
        except Exception as exc:
            return Probe(
                False,
                redact(f"{type(exc).__name__}: {exc}", secrets=[(conn or {}).get("secret") or ""]),
            )

    def discover(self, conn: dict[str, Any] | None, spec: dict[str, Any]) -> list[TableInfo]:
        with self._client(conn) as client:
            rec = self._record(client, self._base(conn, spec), spec["record"])
        return [TableInfo(f["key"], f"{f.get('size')} bytes") for f in rec.get("files") or []]

    def read(
        self,
        conn: dict[str, Any] | None,
        spec: dict[str, Any],
        since: Watermark | None,
        limits: Limits,
    ) -> Iterator[TableBatch]:
        pattern = spec.get("files") or "*"
        base = self._base(conn, spec)
        api_origin = url_origin(base)
        # File links come from the record JSON — server-controlled content, not configuration. A
        # link on the API's own origin is fetched with the (possibly authenticated) API client; any
        # other link through a client that carries no credential. `guarded_client` already strips
        # the header on a cross-origin *redirect*; this covers the first request to the link.
        with self._client(conn, limits) as client, self._client(None, limits) as anonymous:
            rec = self._record(client, base, spec["record"])
            chosen = [f for f in rec.get("files") or [] if fnmatch.fnmatch(f["key"], pattern)]
            checksums = {f["key"]: f.get("checksum", "") for f in chosen}
            wm: Watermark = {
                "record": rec.get("id"),
                "version": rec.get("revision"),
                "files": checksums,
            }
            if since is not None:
                # Incremental = append new files only: a file the parent snapshot holds whose
                # checksum changed, or that left the record, forces a full re-read (I1).
                prev = dict(since.get("files") or {})
                if prev == checksums:
                    return
                require_append_only(prev, checksums, what="record file")
                chosen = [f for f in chosen if f["key"] not in prev]
            for entry in chosen:
                # Fail closed *before* downloading: an unparseable checksum must never be
                # silently treated as "nothing to verify".
                algo, expected = _checksum(entry, spec["record"])
                table = _table_name(entry["key"], spec)
                with tempfile.TemporaryFile() as tmp:
                    h = _hasher(algo, entry["key"])
                    total = 0
                    link = _file_url(entry)
                    own = api_origin is not None and url_origin(link) == api_origin
                    with (client if own else anonymous).stream("GET", link) as resp:
                        resp.raise_for_status()
                        for chunk in resp.iter_bytes(chunk_size=_HTTP_CHUNK):
                            total += len(chunk)
                            if limits.max_bytes is not None and total > limits.max_bytes:
                                raise LimitExceeded(
                                    f"download of {entry['key']!r} in record {spec['record']} "
                                    f"exceeded max_bytes={limits.max_bytes}"
                                )
                            h.update(chunk)
                            tmp.write(chunk)
                    if h.hexdigest() != expected:
                        raise DataplaneError(
                            f"checksum mismatch for {entry['key']} in record {spec['record']}"
                        )
                    tmp.seek(0)
                    for batch in _batches(tmp, _format_of(entry["key"], spec)):
                        yield TableBatch(table, batch, wm)
