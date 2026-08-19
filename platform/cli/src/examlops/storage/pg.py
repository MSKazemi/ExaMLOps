"""SQLite-dialect → Postgres translation, so the 252 platform helpers migrate unchanged.

**The finding this module is built on.** Every one of the ~252 public helpers in
``examlops.platform_db`` and ``examlops.data.*`` reaches the datastore through exactly one
chokepoint — :func:`examlops.platform_db.get_db`. None of them opens its own connection. That
turns "port 252 helpers to Postgres" (883 ``?`` placeholders, 127 ``CREATE TABLE``, 76
``AUTOINCREMENT``) into "put a translating connection behind one function", which is what this
module is: a thin adapter exposing the small slice of the :mod:`sqlite3` API the helpers actually
use (``execute``/``executescript``/``executemany``/``commit``/``close``, ``fetchone``/``fetchall``,
``rowcount``, ``lastrowid``, and rows addressable **both** by name and by position) on top of
``psycopg``.

**What is translated** (code regions only — string literals are never rewritten):

| SQLite                              | Postgres                                              |
|-------------------------------------|-------------------------------------------------------|
| ``?`` placeholders                  | ``%s``                                                |
| ``INTEGER PRIMARY KEY AUTOINCREMENT``| ``BIGSERIAL PRIMARY KEY``                            |
| ``DATETIME`` / ``REAL`` / ``BLOB``  | ``TEXT`` / ``DOUBLE PRECISION`` / ``BYTEA``           |
| ``CURRENT_TIMESTAMP``               | ``to_char(now() …)`` — a *string*, keeping SQLite's   |
|                                     | ``'YYYY-MM-DD HH:MM:SS'`` text semantics intact       |
| ``datetime('now', ?)``              | ``now() + CAST(%s AS interval)`` rendered the same way |
| ``julianday(a) - julianday(b)``     | ``EXTRACT(EPOCH FROM …)/86400``                       |
| ``INSERT OR REPLACE``               | ``ON CONFLICT (<pk>) DO UPDATE SET``                  |
| ``INSERT OR IGNORE``                | ``ON CONFLICT DO NOTHING``                            |
| ``PRAGMA table_info(t)``            | ``information_schema.columns`` shaped like the pragma |
| ``BEGIN IMMEDIATE``                 | ``pg_advisory_xact_lock`` — the same cross-process    |
|                                     | write mutex the audit hash-chain relies on            |

Keeping timestamps as **text** is deliberate: 146 columns are declared ``DATETIME NOT NULL DEFAULT
CURRENT_TIMESTAMP`` and helpers compare, slice and return them as strings. Mapping them to a real
``timestamp`` would silently change 252 helpers' return types; mapping them to ``TEXT`` changes
nothing above the seam. Moving to native timestamps is a later, separately-verified step.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: SQLite's ``CURRENT_TIMESTAMP`` renders ``'YYYY-MM-DD HH:MM:SS'`` in UTC. This is the
#: byte-identical Postgres expression, used for defaults *and* comparisons.
_NOW_TEXT = "to_char((now() AT TIME ZONE 'UTC'),'YYYY-MM-DD HH24:MI:SS')"

#: Schema identifiers that Postgres reserves but SQLite does not, so they must be quoted. Derived
#: by scanning all 429 column names in the platform schema against Postgres's reserved-word list —
#: exactly one collides. The platform uses no window functions, so quoting it unconditionally in
#: code regions is unambiguous. Re-run that scan when adding a column with a keyword-ish name.
_RESERVED_IDENTIFIERS = ("window",)

_TYPE_MAP = (
    (r"\bDATETIME\b", "TEXT"),
    (r"\bREAL\b", "DOUBLE PRECISION"),
    (r"\bBLOB\b", "BYTEA"),
)


def _split_literals(sql: str) -> list[tuple[bool, str]]:
    """Split into ``(is_literal, text)`` runs so rewrites never touch quoted strings or comments.

    Comments count as literal: the schema DDL is heavily commented, and a ``--`` comment containing
    a semicolon would otherwise be split into a bogus statement (and its prose rewritten as SQL).
    """
    out: list[tuple[bool, str]] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            out.append((False, "".join(buf)))
            buf = []
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append((True, sql[i:j]))
            i = j
            continue
        if ch == "/" and sql.startswith("/*", i):
            out.append((False, "".join(buf)))
            buf = []
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append((True, sql[i:j]))
            i = j
            continue
        if ch == "'":
            out.append((False, "".join(buf)))
            buf = []
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # escaped ''
                        j += 2
                        continue
                    break
                j += 1
            out.append((True, sql[i : j + 1]))
            i = j + 1
            continue
        buf.append(ch)
        i += 1
    out.append((False, "".join(buf)))
    return out


def _map_code(fn: Callable[[str], str], sql: str) -> str:
    return "".join(text if lit else fn(text) for lit, text in _split_literals(sql))


def _julianday(sql: str) -> str:
    """``(julianday(A) - julianday(B)) * 86400`` → an epoch-second difference."""
    return re.sub(
        r"julianday\(([^()]+)\)\s*-\s*julianday\(([^()]+)\)",
        r"(EXTRACT(EPOCH FROM ((\1)::timestamp - (\2)::timestamp))/86400.0)",
        sql,
        flags=re.IGNORECASE,
    )


def _pragma_table_info(sql: str) -> str | None:
    """``PRAGMA table_info(t)`` → a result set shaped like the pragma (``cid,name,type,…``)."""
    m = re.match(r"\s*PRAGMA\s+table_info\(\s*([A-Za-z0-9_]+)\s*\)\s*;?\s*$", sql, re.IGNORECASE)
    if not m:
        return None
    return (
        "SELECT (ordinal_position - 1) AS cid, column_name AS name, data_type AS type, "
        "CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull, "
        "column_default AS dflt_value, 0 AS pk "
        f"FROM information_schema.columns WHERE table_name = '{m.group(1)}' "
        "ORDER BY ordinal_position"
    )


_TRIGGER_RE = re.compile(
    r"\s*CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+"
    r"(BEFORE|AFTER)\s+(\w+)\s+ON\s+(\w+)\s+"
    r"BEGIN\s+SELECT\s+RAISE\(\s*ABORT\s*,\s*'([^']*)'\s*\)\s*;?\s*END",
    re.IGNORECASE | re.DOTALL,
)


def _append_only_trigger(sql: str) -> str | None:
    """Port SQLite's ``RAISE(ABORT, …)`` guard triggers, which enforce the append-only audit log.

    These are a *security control* (D4 tamper-evidence on ``audit_events``), so they are translated
    rather than skipped: Postgres needs a ``plpgsql`` function, one per distinct message, plus a
    row-level trigger that calls it.
    """
    m = _TRIGGER_RE.search(sql)  # search, not match: the statement may carry a leading comment
    if not m:
        return None
    name, timing, event, table, message = m.groups()
    fn = f"examlops_raise_{name}"
    msg = message.replace("'", "''")
    return (
        f"CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$ "
        f"BEGIN RAISE EXCEPTION '{msg}'; END; $$ LANGUAGE plpgsql; "
        f"CREATE OR REPLACE TRIGGER {name} {timing.upper()} {event.upper()} ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {fn}()"
    )


def translate(sql: str, *, pk_lookup: Callable[[str], Sequence[str]] | None = None) -> str:
    """Rewrite one SQLite statement into its Postgres equivalent.

    ``pk_lookup(table)`` supplies the conflict target for ``INSERT OR REPLACE``; without it (or
    when the table has no key) the statement degrades to a plain ``INSERT`` and logs, rather than
    guessing a target and silently corrupting upsert semantics.
    """
    stripped = sql.strip()

    pragma = _pragma_table_info(stripped)
    if pragma is not None:
        return pragma
    if re.match(r"\s*PRAGMA\b", stripped, re.IGNORECASE):
        return "SELECT 1"  # journal_mode / synchronous / busy_timeout are SQLite-only knobs
    if re.match(r"\s*BEGIN\s+IMMEDIATE\b", stripped, re.IGNORECASE):
        # SQLite's RESERVED lock ≙ a transaction-scoped advisory lock: the same "one writer at a
        # time, released on commit/rollback" guarantee the audit hash-chain depends on.
        return "SELECT pg_advisory_xact_lock(hashtext('examlops-platform-write'))"

    # A statement often arrives with its leading comment attached (the schema DDL is commented
    # per table), so classify on the code, not on the raw text.
    code_only = "".join(seg for lit, seg in _split_literals(stripped) if not lit)
    trigger = _append_only_trigger(stripped)
    if trigger is not None:
        return trigger

    is_ddl = bool(re.match(r"\s*CREATE\s+TABLE\b", code_only.strip(), re.IGNORECASE))
    conflict = ""

    def code(seg: str) -> str:
        seg = seg.replace("?", "%s")
        if is_ddl:
            seg = re.sub(
                r"\bINTEGER\s+PRIMARY\s+KEY(\s+AUTOINCREMENT)?\b",
                "BIGSERIAL PRIMARY KEY",
                seg,
                flags=re.IGNORECASE,
            )
            for pat, repl in _TYPE_MAP:
                seg = re.sub(pat, repl, seg, flags=re.IGNORECASE)
        # julianday() first: rewriting CURRENT_TIMESTAMP into a to_char(...) call would put
        # parentheses inside its arguments and hide the pattern.
        seg = _julianday(seg)
        seg = re.sub(r"\bCURRENT_TIMESTAMP\b", _NOW_TEXT, seg, flags=re.IGNORECASE)
        for word in _RESERVED_IDENTIFIERS:
            seg = re.sub(rf'(?<!")\b{word}\b(?!")', f'"{word}"', seg, flags=re.IGNORECASE)
        return seg

    m = re.match(r"\s*INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+([A-Za-z0-9_]+)", sql, re.IGNORECASE)
    if m:
        verb, table = m.group(1).upper(), m.group(2)
        sql = re.sub(
            r"\s*INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO",
            "INSERT INTO",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
        if verb == "IGNORE":
            conflict = " ON CONFLICT DO NOTHING"
        else:
            keys = list(pk_lookup(table)) if pk_lookup else []
            cols = _insert_columns(sql)
            updates = [c for c in cols if c not in set(keys)]
            if keys and updates:
                sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in updates)
                conflict = f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {sets}"
            elif keys:
                conflict = f" ON CONFLICT ({', '.join(keys)}) DO NOTHING"
            else:
                logger.warning(
                    "INSERT OR REPLACE INTO %s has no resolvable key: running as a plain INSERT",
                    table,
                )

    out = _map_code(code, sql)
    # datetime('now', ?) — the only SQLite date-modifier form the helpers use (retention prune).
    out = re.sub(
        r"datetime\(\s*'now'\s*,\s*%s\s*\)",
        "to_char(((now() AT TIME ZONE 'UTC') + CAST(%s AS interval)),'YYYY-MM-DD HH24:MI:SS')",
        out,
        flags=re.IGNORECASE,
    )
    return out.rstrip().rstrip(";") + conflict if conflict else out


def _insert_columns(sql: str) -> list[str]:
    m = re.search(r"INSERT\s+INTO\s+[A-Za-z0-9_]+\s*\(([^)]*)\)", sql, re.IGNORECASE)
    return [c.strip() for c in m.group(1).split(",")] if m else []


def split_script(script: str) -> list[str]:
    """Split a multi-statement DDL script on ``;`` outside string literals."""
    stmts, buf = [], []
    for lit, text in _split_literals(script):
        if lit:
            buf.append(text)
            continue
        parts = text.split(";")
        for part in parts[:-1]:
            buf.append(part)
            stmts.append("".join(buf))
            buf = []
        buf.append(parts[-1])
    stmts.append("".join(buf))
    return _rejoin_triggers([s for s in (x.strip() for x in stmts) if s])


def _rejoin_triggers(stmts: list[str]) -> list[str]:
    """A SQLite trigger body carries its own ``;``, so naive splitting cuts it in half."""
    out: list[str] = []
    pending: list[str] = []
    for stmt in stmts:
        if pending:
            pending.append(stmt)
            if re.search(r"\bEND\b\s*$", stmt, re.IGNORECASE):
                out.append("; ".join(pending))
                pending = []
            continue
        if re.search(r"\bCREATE\s+TRIGGER\b", stmt, re.IGNORECASE) and not re.search(
            r"\bEND\b\s*$", stmt, re.IGNORECASE
        ):
            pending = [stmt]
            continue
        out.append(stmt)
    out.extend(pending)
    return out


class PgRow(dict):
    """A row addressable by name *and* by position, like :class:`sqlite3.Row`."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _adapt(params: Any) -> Any:
    """SQLite stores ``bool`` as 0/1 in INTEGER columns; Postgres is strict, so do it here."""
    if params is None:
        return None
    return tuple(int(p) if isinstance(p, bool) else p for p in params)


def _row_factory(cursor: Any) -> Callable[[Sequence[Any]], PgRow]:
    cols = [c.name for c in (cursor.description or [])]

    def make(values: Sequence[Any]) -> PgRow:
        return PgRow(zip(cols, values, strict=False))

    return make


class PgCursor:
    """The slice of :class:`sqlite3.Cursor` the platform helpers use."""

    def __init__(self, cur: Any, returned: PgRow | None = None) -> None:
        self._cur = cur
        self._returned = returned
        self._returned_pending = returned is not None

    def fetchone(self) -> PgRow | None:
        if self._returned_pending:  # an INSERT … RETURNING row captured for lastrowid
            self._returned_pending = False
            return self._returned
        return self._cur.fetchone()

    def fetchall(self) -> list[PgRow]:
        return list(self._cur.fetchall())

    def fetchmany(self, size: int | None = None) -> list[PgRow]:
        return list(self._cur.fetchmany(size) if size is not None else self._cur.fetchmany())

    def __iter__(self) -> Any:
        return iter(self._cur)

    @property
    def rowcount(self) -> int:
        return int(self._cur.rowcount)

    @property
    def lastrowid(self) -> int | None:
        """The new row's ``id``, taken from ``RETURNING`` (Postgres has no rowid)."""
        if self._returned and "id" in self._returned:
            return int(self._returned["id"])
        return None

    @property
    def description(self) -> Any:
        return self._cur.description

    def close(self) -> None:
        self._cur.close()


class PgConnection:
    """A :mod:`sqlite3`-shaped connection over ``psycopg``, translating SQL on the way through."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._pk_cache: dict[str, list[str]] = {}
        self.row_factory: Any = (
            None  # accepted and ignored: rows are already name+index addressable
        )
        self.isolation_level: Any = None

    # -- sqlite3 API ---------------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PgCursor:
        # Transaction control is driven through psycopg's own API: executing a bare COMMIT would
        # desynchronise its transaction state. `_immediate_write` issues these explicitly.
        verb = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if verb in ("COMMIT", "ROLLBACK", "BEGIN", "END"):
            if verb == "COMMIT":
                self.commit()
            elif verb == "ROLLBACK":
                self.rollback()
            return self._noop()
        translated = translate(sql, pk_lookup=self._primary_key)
        returning = False
        if re.match(r"\s*INSERT\b", translated, re.IGNORECASE) and not re.search(
            r"\bRETURNING\b", translated, re.IGNORECASE
        ):
            translated += " RETURNING *"
            returning = True
        cur = self._conn.cursor(row_factory=_row_factory)
        cur.execute(translated, _adapt(params))
        captured = cur.fetchone() if (returning and cur.rowcount) else None
        return PgCursor(cur, captured)

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> PgCursor:
        cur = self._conn.cursor(row_factory=_row_factory)
        cur.executemany(translate(sql, pk_lookup=self._primary_key), [_adapt(p) for p in seq])
        return PgCursor(cur)

    def executescript(self, script: str) -> PgCursor:
        cur = self._conn.cursor(row_factory=_row_factory)
        for stmt in split_script(script):
            cur.execute(translate(stmt, pk_lookup=self._primary_key))
        return PgCursor(cur)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> PgConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.commit() if exc_type is None else self.rollback()

    def _noop(self) -> PgCursor:
        cur = self._conn.cursor(row_factory=_row_factory)
        cur.execute("SELECT 1")
        return PgCursor(cur)

    # -- internals -----------------------------------------------------------------------
    def _primary_key(self, table: str) -> list[str]:
        """Conflict target for ``INSERT OR REPLACE``: the table's PK, else its first unique index."""
        if table in self._pk_cache:
            return self._pk_cache[table]
        cur = self._conn.cursor()
        cur.execute(
            """SELECT a.attname
                 FROM pg_index i
                 JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = to_regclass(%s)
                  AND (i.indisprimary OR i.indisunique)
             ORDER BY i.indisprimary DESC, a.attnum""",
            (table,),
        )
        keys = [r[0] for r in cur.fetchall()]
        # A BIGSERIAL surrogate `id` is never the upsert target the SQLite code meant.
        keys = [k for k in keys if k != "id"] or keys
        self._pk_cache[table] = keys
        return keys


def connect(dsn: str) -> PgConnection:
    """Open a translating Postgres connection (the SQLite-shaped API above)."""
    import psycopg  # noqa: PLC0415 - optional enterprise dependency, imported on use

    return PgConnection(psycopg.connect(dsn))
