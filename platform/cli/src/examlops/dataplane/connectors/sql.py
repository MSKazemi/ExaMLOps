"""SQL connector (ADR 0130 §5): any SQLAlchemy URL + a query or table, streamed, read-only."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from typing import Any

from examlops.dataplane.connectors.base import BaseConnector
from examlops.dataplane.safety import redact
from examlops.dataplane.types import Limits, Probe, SpecError, TableBatch, TableInfo, Watermark

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")


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
        if conn.get("secret"):
            url = url.set(password=conn["secret"])
        return sa.create_engine(url, pool_pre_ping=True)

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

        eng = self._engine(conn)
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

        eng = self._engine(conn)
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
