"""SQL connector (ADR 0130 §5): any SQLAlchemy URL + a query or table, streamed, read-only."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from typing import Any

from examlops.dataplane.connectors.base import BaseConnector
from examlops.dataplane.safety import check_address, local_files_allowed, redact
from examlops.dataplane.types import (
    EgressDenied,
    Limits,
    Probe,
    SpecError,
    TableBatch,
    TableInfo,
    Watermark,
)

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")
_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306}
# A query parameter that can silently redirect the connection past the netloc host the guard
# would otherwise check: `host`/`port` (postgres and mysql both honour a query override of
# either), `hostaddr` (libpq connects to this IP directly, bypassing hostname resolution
# entirely — and it is literally the connect_args key this module's own hostaddr pin uses),
# `service` (names an opaque pg_service.conf entry this code cannot inspect), `unix_socket`
# (mysql/pymysql). Fix KS round 1, CRITICAL 1.
_HOST_ROUTING_QUERY_KEYS = frozenset({"host", "hostaddr", "unix_socket", "service", "port"})


def _default_port(drivername: str) -> int:
    return _DEFAULT_PORTS.get(drivername.split("+")[0], 0)


def _refuse_host_routing_query_params(url: Any) -> None:
    bad = sorted({str(k).lower() for k in url.query} & _HOST_ROUTING_QUERY_KEYS)
    if bad:
        raise SpecError(
            f"the sql connection URL's query must not set {bad} — a query parameter can "
            "silently override the host/port the egress guard checked; use the URL's netloc "
            "(scheme://host:port/db) instead"
        )


def _dialect_connect_kwargs(url: Any) -> dict[str, Any]:
    """The actual kwargs the dialect hands its DBAPI's ``connect()`` — the ground truth for what
    host a connection reaches, immune to a dialect quirk that resolves a host differently than
    ``url.host``/``.port`` alone would suggest (fix KS round 1, CRITICAL 1)."""
    from sqlalchemy.exc import NoSuchModuleError

    try:
        dialect = url.get_dialect()()
    except NoSuchModuleError:
        raise SpecError(
            f"the sql connection URL names an unknown or uninstalled dialect/driver "
            f"{url.drivername!r} (install its DBAPI package, e.g. the dataplane-sql extra)"
        ) from None
    _, kwargs = dialect.create_connect_args(url)
    return kwargs


def _egress_check_url(url: Any) -> dict[str, Any]:
    """Egress-check ``url``'s effective host before any engine reaches it (ADR 0130 §10, fix KS).

    Refuses a query parameter that could route the connection elsewhere first (CRITICAL 1), then
    reads the connection's actual target from the dialect itself via ``create_connect_args``
    rather than trusting ``url.host``/``.port`` directly — the two agree once the query is clean,
    but this is immune to a dialect quirk neither check anticipated.

    Any URL with no host, or whose effective host is a filesystem path (a Unix socket, or
    sqlite's ``sqlite:///path``), is a local connection, not a network egress: refused unless
    ``safety.local_files_allowed()``, the same gate the files connector applies (CRITICAL 2 — a
    host-less URL, and ``sqlite:``, both bypassed this gate entirely before this fix). A
    multi-host URL (``host1,host2``) is refused outright — checking every alternate is out of
    scope here.

    For ``postgresql+psycopg`` the checked address is pinned via libpq's ``hostaddr`` connect
    arg, returned here for the caller to pass into ``connect_args``, so DNS cannot rebind
    between the check and the connection while TLS still verifies the hostname (``host`` stays
    the name). Other host-having dialects (mysql, postgresql+psycopg2, …) are checked but not
    pinned — a residual, as for an S3 endpoint's ``https://`` path.
    """
    _refuse_host_routing_query_params(url)
    kwargs = _dialect_connect_kwargs(url)
    host = kwargs.get("host")
    if host and "," in str(host):
        raise SpecError("multi-host URLs are not supported by the dataplane egress guard")
    if not host or "/" in str(host):
        if not local_files_allowed():
            raise SpecError(
                "this sql URL has no network host (a local file or Unix-socket connection); "
                "local connections are disabled in the dataplane. Set "
                "EXAMLOPS_DATAPLANE_ALLOW_LOCAL_FILES=1 only where the service has no access to "
                "platform state"
            )
        return {}
    port = kwargs.get("port")
    port = int(port) if port not in (None, "") else _default_port(url.drivername)
    ip = check_address(str(host), port)
    if url.drivername == "postgresql+psycopg":
        return {"hostaddr": ip}
    return {}


def _typed(value: Any) -> tuple[Any, str]:
    if isinstance(value, bool):
        return int(value), "int"
    if isinstance(value, int):
        return value, "int"
    if isinstance(value, float):
        return value, "float"
    if isinstance(value, dt.datetime):
        return value.isoformat(), "datetime"
    if isinstance(value, dt.date):
        return value.isoformat(), "date"
    return str(value), "str"


def _untyped(wm: Watermark) -> Any:
    kind, value = wm.get("type"), wm.get("value")
    if kind == "datetime" and isinstance(value, str):
        return dt.datetime.fromisoformat(value)
    if kind == "date" and isinstance(value, str):
        return dt.date.fromisoformat(value)
    return value


def begin_read_only(conn: Any, *, timeout_s: float) -> None:
    """Make the session read-only where the dialect allows it; bound statement time."""
    name = conn.dialect.name
    ms = int(timeout_s * 1000)
    if name == "postgresql":
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {ms}")
    elif name in ("mysql", "mariadb"):
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        if name == "mysql":
            conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {ms}")
    elif name == "sqlite":
        conn.exec_driver_sql("PRAGMA query_only = ON")


class SqlConnector(BaseConnector):
    kind = "sql"
    connection_kinds = ("sql",)
    extra = "dataplane-sql"
    requires = ("sqlalchemy", "pyarrow")
    supports_incremental = True

    def validate_spec(self, spec: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        query, table = spec.get("query"), spec.get("table")
        if not query and not table:
            errors.append("spec.query or spec.table is required")
        if query and table:
            errors.append("give spec.query or spec.table, not both")
        for key in ("table", "watermark_column"):
            if spec.get(key) and not _IDENT.fullmatch(str(spec[key])):
                errors.append(f"spec.{key} must be an identifier (schema.table allowed)")
        if spec.get("incremental") and not spec.get("watermark_column"):
            errors.append("incremental sql sources need spec.watermark_column")
        return errors

    def _engine(self, conn: dict[str, Any] | None) -> Any:
        import sqlalchemy as sa
        from sqlalchemy.engine import make_url

        if not conn or not conn.get("url"):
            raise SpecError(
                "sql connection needs config.url (a SQLAlchemy URL without the password)"
            )
        url = make_url(conn["url"])
        connect_args = _egress_check_url(url)
        if conn.get("secret"):
            url = url.set(password=conn["secret"])
        return sa.create_engine(url, pool_pre_ping=True, connect_args=connect_args)

    def _checked_engine(self, conn: dict[str, Any] | None) -> Any:
        """``_engine()``, but an egress/spec failure comes back redacted before it leaves this
        connector — the same path ``probe()`` uses (fix KS round 1, Important 5). Used by
        ``discover()``/``read()``; ``probe()`` already wraps everything more broadly itself."""
        try:
            return self._engine(conn)
        except (EgressDenied, SpecError) as exc:
            secret = (conn or {}).get("secret")
            raise type(exc)(redact(str(exc), secrets=[secret] if secret else [])) from None

    def probe(self, conn: dict[str, Any] | None, spec: dict[str, Any] | None = None) -> Probe:
        secret = (conn or {}).get("secret")
        eng = None
        try:
            import sqlalchemy as sa

            eng = self._engine(conn)
            with eng.connect() as c:
                c.execute(sa.text("SELECT 1"))
            return Probe(True, f"{eng.dialect.name} reachable")
        except Exception as exc:
            return Probe(
                False, redact(f"{type(exc).__name__}: {exc}", secrets=[secret] if secret else [])
            )
        finally:
            if eng is not None:
                eng.dispose()

    def discover(self, conn: dict[str, Any] | None, spec: dict[str, Any]) -> list[TableInfo]:
        import sqlalchemy as sa

        eng = self._checked_engine(conn)
        try:
            return [TableInfo(n) for n in sa.inspect(eng).get_table_names()[:500]]
        finally:
            eng.dispose()

    def read(
        self,
        conn: dict[str, Any] | None,
        spec: dict[str, Any],
        since: Watermark | None,
        limits: Limits,
    ) -> Iterator[TableBatch]:
        """Stream the query/table; incrementally, only rows with ``watermark_column > since``.

        Incremental means **insert-only**: an incremental snapshot carries every row the parent
        held and appends the new ones, so ``watermark_column`` must be monotonic and set once per
        row (an increasing id, ``created_at``). A column that moves when a row is *updated*
        (``updated_at``) re-reads that row and the snapshot then holds it twice; a changed spec
        (table/query) makes ``run_pull`` read everything instead. For mutable rows, pull full.
        """
        import pyarrow as pa
        import sqlalchemy as sa

        eng = self._checked_engine(conn)
        prep = eng.dialect.identifier_preparer

        def quoted(ident: str) -> str:
            return ".".join(prep.quote(p) for p in str(ident).split("."))

        base = spec.get("query") or f"SELECT * FROM {quoted(spec['table'])}"
        wm_col = spec.get("watermark_column")
        sql = f"SELECT * FROM ({base}) AS dp_src"
        params = dict(spec.get("params") or {})
        if since and wm_col and since.get("value") is not None:
            sql += f" WHERE {quoted(wm_col)} > :dp_since"
            params["dp_since"] = _untyped(since)
        if wm_col:
            sql += f" ORDER BY {quoted(wm_col)}"
        table = str(spec.get("output") or str(spec.get("table") or "rows").split(".")[-1])
        chunk = int(spec.get("chunk_rows") or 50_000)
        if limits.max_rows is not None:
            chunk = min(chunk, max(limits.max_rows, 1))
        watermark: Watermark | None = dict(since) if since else None
        try:
            with eng.connect() as c:
                begin_read_only(c, timeout_s=limits.max_seconds or 3600.0)
                stream = eng.dialect.supports_server_side_cursors
                options = {"yield_per": chunk}
                if stream:
                    options["stream_results"] = True
                result = c.execution_options(**options).execute(sa.text(sql), params)
                cols = list(result.keys())
                for part in result.partitions(chunk):
                    records = [dict(zip(cols, row, strict=True)) for row in part]
                    if wm_col:
                        top = max(
                            (r[wm_col] for r in records if r.get(wm_col) is not None),
                            default=None,
                        )
                        if top is not None:
                            value, kind = _typed(top)
                            watermark = {"column": wm_col, "value": value, "type": kind}
                    yield TableBatch(table, pa.RecordBatch.from_pylist(records), watermark)
        finally:
            eng.dispose()
