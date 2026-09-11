"""Generic paginated JSON REST connector (ADR 0130 §5)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from examlops.dataplane.connectors.base import BaseConnector
from examlops.dataplane.safety import guarded_client, redact
from examlops.dataplane.types import EgressDenied, Limits, Probe, SpecError, TableBatch, Watermark

_client_factory = guarded_client
_PAGINATION = {"none", "page", "offset", "cursor", "link"}


def _dig(body: Any, dotted: str) -> Any:
    for part in [p for p in (dotted or "").split(".") if p]:
        body = body.get(part) if isinstance(body, dict) else None
    return body


def _origin(url: str) -> tuple[str, str, int | None]:
    """``(scheme, host, port)`` with the scheme's default port filled in, for a same-origin check."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return (scheme, (parts.hostname or "").lower(), port)


class RestConnector(BaseConnector):
    kind = "rest"
    connection_kinds = ("rest",)
    extra = "dataplane-files"
    requires = ("httpx", "pyarrow")
    required_spec = ("path",)
    supports_incremental = True

    def validate_spec(self, spec: dict[str, Any]) -> list[str]:
        errors = super().validate_spec(spec)
        ptype = (spec.get("pagination") or {}).get("type", "none")
        if ptype not in _PAGINATION:
            errors.append(f"spec.pagination.type must be one of {sorted(_PAGINATION)}")
        if spec.get("incremental") and not (
            spec.get("watermark_field") and spec.get("since_param")
        ):
            errors.append("incremental rest sources need spec.watermark_field and spec.since_param")
        return errors

    def _client(self, conn: dict[str, Any] | None, spec: dict[str, Any]) -> Any:
        cfg = conn or {}
        headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
        auth = None
        mode = cfg.get("auth", "bearer" if cfg.get("secret") else "none")
        if mode == "bearer" and cfg.get("secret"):
            headers["Authorization"] = f"Bearer {cfg['secret']}"
        elif mode == "header" and cfg.get("secret"):
            headers[str(cfg.get("header_name") or "X-API-Key")] = str(cfg["secret"])
        elif mode == "basic":
            auth = (str(cfg.get("username") or ""), str(cfg.get("secret") or ""))
        client = _client_factory(headers=headers)
        if auth is not None:
            client.auth = auth
        return client

    def _url(self, conn: dict[str, Any] | None, spec: dict[str, Any]) -> str:
        base = str((conn or {}).get("base_url") or "").rstrip("/")
        if not base:
            raise SpecError("rest connection needs config.base_url")
        return f"{base}/{str(spec['path']).lstrip('/')}"

    def probe(self, conn: dict[str, Any] | None, spec: dict[str, Any] | None = None) -> Probe:
        try:
            with self._client(conn, spec or {}) as client:
                resp = client.get(self._url(conn, spec or {"path": ""}))
            return Probe(resp.status_code < 400, f"HTTP {resp.status_code}")
        except Exception as exc:
            return Probe(
                False,
                redact(f"{type(exc).__name__}: {exc}", secrets=[(conn or {}).get("secret") or ""]),
            )

    def read(
        self,
        conn: dict[str, Any] | None,
        spec: dict[str, Any],
        since: Watermark | None,
        limits: Limits,
    ) -> Iterator[TableBatch]:
        import pyarrow as pa

        pag = dict(spec.get("pagination") or {"type": "none"})
        ptype = pag.get("type", "none")
        params: dict[str, Any] = dict(spec.get("params") or {})
        wm_field = spec.get("watermark_field")
        if since and spec.get("since_param") and since.get("value") is not None:
            params[spec["since_param"]] = since["value"]
        size = int(pag.get("size") or 100)
        page = int(pag.get("start") or (1 if ptype == "page" else 0))
        if ptype == "page":
            params[pag.get("size_param") or "per_page"] = size
        if ptype == "offset":
            params[pag.get("size_param") or "limit"] = size
        start_url = self._url(conn, spec)
        base_origin = _origin(start_url)
        url: str | None = start_url
        watermark = dict(since) if since else None
        pause = 1.0 / float(spec["rate_limit_per_sec"]) if spec.get("rate_limit_per_sec") else 0.0
        table = str(spec.get("table") or "records")
        rows_yielded = 0
        with self._client(conn, spec) as client:
            for _ in range(int(spec.get("max_pages") or 1000)):
                if url is None:
                    break
                if ptype == "page":
                    params[pag.get("param") or "page"] = page
                elif ptype == "offset":
                    params[pag.get("param") or "offset"] = page
                send_params = params if ptype != "link" or page == 0 else None
                resp = client.get(url, params=send_params)
                resp.raise_for_status()
                try:
                    body = resp.json()
                except ValueError as exc:
                    ctype = resp.headers.get("content-type", "unknown")
                    raise SpecError(
                        f"{spec.get('path')!r} returned non-JSON (content-type {ctype!r})"
                    ) from exc
                records = (
                    _dig(body, spec.get("records_path", "")) if spec.get("records_path") else body
                )
                if not isinstance(records, list):
                    raise SpecError(
                        f"spec.records_path {spec.get('records_path')!r} did not select a list"
                    )
                if not records:
                    break
                if wm_field:
                    top: Any = None
                    for r in records:
                        if not isinstance(r, dict):
                            continue
                        value = r.get(wm_field)
                        if value is None:
                            continue
                        if top is None:
                            top = value
                            continue
                        try:
                            newer = value > top
                        except TypeError as exc:
                            raise SpecError(
                                f"spec.watermark_field {wm_field!r} has mixed types "
                                f"({type(top).__name__} and {type(value).__name__}); "
                                "cannot compute a watermark"
                            ) from exc
                        if newer:
                            top = value
                    if top is not None:
                        watermark = {"field": wm_field, "value": top}
                yield TableBatch(table, pa.RecordBatch.from_pylist(records), watermark)
                rows_yielded += len(records)
                # The writer enforces the hard row cap; stop requesting further pages once we've
                # already yielded enough rows to satisfy it, so we don't fetch pages that would
                # just be discarded.
                if limits.max_rows is not None and rows_yielded >= limits.max_rows:
                    break
                if ptype == "none":
                    break
                if ptype == "page":
                    page += 1
                elif ptype == "offset":
                    page += len(records)
                elif ptype == "cursor":
                    cursor = _dig(body, pag.get("cursor_path") or "next")
                    if cursor in (None, ""):
                        break
                    params[pag.get("param") or "cursor"] = cursor
                elif ptype == "link":
                    next_url = resp.links.get("next", {}).get("url")
                    # A link header's target may be relative to the current response's URL (RFC
                    # 8288); resolve it before the next request rather than handing httpx a bare
                    # path. The original query params are not resent on a link-followed page —
                    # the link already encodes the full next-page query.
                    resolved = str(resp.url.join(next_url)) if next_url else None
                    if resolved is not None and _origin(resolved) != base_origin:
                        # A `Link` header is server-controlled response content, not a redirect —
                        # httpx's own cross-origin credential strip never runs for it. Without this
                        # check, a compromised or malicious upstream could point "next" at any host
                        # and receive this source's bearer/API-key header or basic-auth credential.
                        host = urlsplit(resolved).hostname or resolved
                        raise EgressDenied(
                            f"refusing to follow a next link to a different origin host {host!r}"
                        )
                    url = resolved
                    page += 1
                if pause:
                    time.sleep(pause)
