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
| ``INTEGER``                          | ``BIGINT`` (SQLite's is 8 bytes, not int4)           |
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

import atexit
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

#: SQLite's ``CURRENT_TIMESTAMP`` renders ``'YYYY-MM-DD HH:MM:SS'`` in UTC. This is the
#: byte-identical Postgres expression, used for defaults *and* comparisons.
_SQLITE_MASTER = (
    "(SELECT tablename AS name, 'table' AS type, tablename AS tbl_name, NULL::text AS sql "
    "   FROM pg_tables  WHERE schemaname = current_schema() "
    " UNION ALL "
    " SELECT indexname AS name, 'index' AS type, tablename AS tbl_name, indexdef AS sql "
    "   FROM pg_indexes WHERE schemaname = current_schema()) AS sqlite_master"
)
"""SQLite's schema catalogue as a subquery.

Both the platform and the dashboard ask ``SELECT 1 FROM sqlite_master WHERE type='table' AND
name=?`` to decide whether an optional table exists yet — a pattern worth keeping rather than
rewriting in ~20 places, so the catalogue is supplied instead.
"""

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
    # SQLite's INTEGER holds up to 8 bytes; Postgres's is int4, which stops at 2,147,483,647.
    # Left alone, every column the schema declares INTEGER silently becomes a narrower type than
    # the one the platform was written against — and it fails at *write* time, as an error the
    # caller sees only once a value gets big enough. `project_storage.used_bytes` is the plain
    # example: any project holding more than ~2 GB cannot record its own usage. Epoch-millisecond
    # timestamps and the other byte counters have the same ceiling. BIGINT is what SQLite meant.
    (r"\bINTEGER\b", "BIGINT"),
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
        # Scoped to the active schema: without this, two platform instances sharing a Postgres
        # database see each other's columns and the additive column migrations silently skip.
        "AND table_schema = current_schema() ORDER BY ordinal_position"
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


def translate(
    sql: str,
    *,
    pk_lookup: Callable[[str], Sequence[str]] | None = None,
    trigger_table: Callable[[str], str | None] | None = None,
    has_id: Callable[[str], bool] | None = None,
) -> str:
    """Rewrite one SQLite statement into its Postgres equivalent.

    ``pk_lookup(table)`` supplies the conflict target for ``INSERT OR REPLACE``; without it (or
    when the table has no key) the statement degrades to a plain ``INSERT`` and logs, rather than
    guessing a target and silently corrupting upsert semantics. ``has_id(table)`` says whether the
    queried table carries the surrogate ``id`` column that stands in for SQLite's ``rowid``.
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

    # SQLite triggers are global; Postgres attaches them to a table, so the DROP needs it.
    m_drop = re.match(
        r"\s*DROP\s+TRIGGER\s+(IF\s+EXISTS\s+)?(\w+)\s*;?\s*$", stripped, re.IGNORECASE
    )
    if m_drop:
        table = trigger_table(m_drop.group(2)) if trigger_table else None
        if table:
            return f"DROP TRIGGER {m_drop.group(1) or ''}{m_drop.group(2)} ON {table}"
        # No such trigger. With IF EXISTS that is a no-op; without it, let Postgres object.
        return "SELECT 1" if m_drop.group(1) else stripped

    is_ddl = bool(re.match(r"\s*CREATE\s+TABLE\b", code_only.strip(), re.IGNORECASE))
    conflict = ""
    rowid_column = _rowid_column(code_only, has_id)

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
        # SQLite's implicit rowid is used as an insertion-order tie-break in ORDER BY. Most
        # platform tables have a BIGSERIAL `id` carrying exactly that meaning; the few with a
        # natural TEXT primary key have no such column, and there the tie-break is dropped
        # rather than invented. (`\b` keeps this from touching `lastrowid`.)
        if rowid_column:
            seg = re.sub(r"\browid\b", rowid_column, seg, flags=re.IGNORECASE)
        else:
            seg = re.sub(r"\s*,\s*rowid\s+(ASC|DESC)\b", "", seg, flags=re.IGNORECASE)
            seg = re.sub(r"\browid\b", "1", seg, flags=re.IGNORECASE)
        seg = _julianday(seg)
        seg = re.sub(r"\bCURRENT_TIMESTAMP\b", _NOW_TEXT, seg, flags=re.IGNORECASE)
        seg = re.sub(
            r"\bFROM\s+sqlite_master\b", f"FROM {_SQLITE_MASTER}", seg, flags=re.IGNORECASE
        )
        # A bare placeholder in a boolean position. `_adapt` sends Python bools as the 0/1
        # SQLite stores in its INTEGER columns, which is right everywhere except here, where
        # Postgres wants a real boolean and will not coerce one. `<> 0` says what SQLite means
        # by a truthy value without depending on a smallint→boolean cast that does not exist.
        seg = re.sub(r"\bWHEN\s+%s\s+THEN\b", "WHEN %s <> 0 THEN", seg, flags=re.IGNORECASE)
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

    out = _datetime_fn(_map_code(code, sql))
    return out.rstrip().rstrip(";") + conflict if conflict else out


def _rowid_column(code_only: str, has_id: Callable[[str], bool] | None) -> str | None:
    """What ``rowid`` should become in this statement — ``id``, or nothing at all.

    Without a catalogue lookup we assume ``id`` (true for all but a handful of tables), which
    is also what the pure-function tests exercise.
    """
    if "rowid" not in code_only.lower():
        return "id"
    if has_id is None:
        return "id"
    m = re.search(r"\bFROM\s+([A-Za-z0-9_]+)", code_only, re.IGNORECASE)
    if not m:
        return "id"
    return "id" if has_id(m.group(1)) else None


def _split_args(inner: str) -> list[str]:
    """Split a call's arguments on top-level commas (arguments may themselves contain calls)."""
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in inner:
        if ch == "," and depth == 0:
            args.append("".join(buf))
            buf = []
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        buf.append(ch)
    args.append("".join(buf))
    return [a.strip() for a in args]


def _datetime_fn(sql: str) -> str:
    """SQLite ``datetime(base, modifier…)`` → a Postgres timestamp rendered back to SQLite's text.

    Handles the whole family the helpers use — ``datetime('now', ?)`` and
    ``datetime(CURRENT_TIMESTAMP, ?)`` (leases, cooldowns, rate-limit windows, retention prune) —
    including the case where ``CURRENT_TIMESTAMP`` has already become a ``to_char(…)`` call, which
    is why the arguments are split with a paren-aware scanner rather than a regex.
    """
    out, i = [], 0
    for m in re.finditer(r"\bdatetime\s*\(", sql, re.IGNORECASE):
        if m.start() < i:
            continue  # inside an argument already consumed
        depth, j = 1, m.end()
        while j < len(sql) and depth:
            depth += (sql[j] == "(") - (sql[j] == ")")
            j += 1
        if depth:
            break  # unbalanced — leave the statement alone
        args = _split_args(sql[m.end() : j - 1])
        base = args[0]
        expr = (
            "(now() AT TIME ZONE 'UTC')"
            if base.strip().strip("'").lower() == "now"
            else f"({_datetime_fn(base)})::timestamp"
        )
        for modifier in args[1:]:
            expr = f"({expr} + CAST({modifier} AS interval))"
        out.append(sql[i : m.start()])
        out.append(f"to_char({expr},'YYYY-MM-DD HH24:MI:SS')")
        i = j
    out.append(sql[i:])
    return "".join(out)


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

    def __eq__(self, other: Any) -> bool:
        """Compare equal to a plain tuple of the same values, as well as to a mapping.

        Without this the seam has a *silent* fidelity gap rather than a loud one. Under SQLite a
        caller who sets ``row_factory=None`` gets a real tuple, so ``row == (0.995, 1)`` is a
        perfectly ordinary assertion; here it returned False and the caller concluded the row was
        wrong rather than that the comparison was. Honouring ``row_factory=None`` literally is not
        the fix — it is already this connection's default and means "ignored", so obeying it would
        hand tuples to all ~252 platform helpers, every one of which reads rows by name.
        """
        if isinstance(other, (tuple, list)):
            return tuple(self.values()) == tuple(other)
        return super().__eq__(other)

    def __iter__(self) -> Any:
        """Iterate the row's *values*, as :class:`sqlite3.Row` does — not its keys.

        This is what makes ``actor, source = row`` work, and it is the other half of the tuple
        fidelity :meth:`__eq__` restores: a caller that can compare a row to a tuple should also
        be able to unpack one. Inheriting dict's key iteration meant the unpack silently bound the
        *column names*, so an assertion read ``'source' == 'dashboard'`` and looked like a data bug.

        Name-keyed use is unaffected: ``row["col"]``, ``in``, ``.keys()`` and ``**row`` all still go
        through dict. ``dict(row)`` is safe too — CPython's dict-merge fast path is guarded on
        ``tp_iter`` being dict's own, so overriding it here routes the copy via ``keys()``.
        """
        return iter(self.values())

    __hash__ = None  # type: ignore[assignment]  # as for dict; spelled out since __eq__ is defined


def _adapt(params: Any) -> Any:
    """SQLite stores ``bool`` as 0/1 in INTEGER columns; Postgres is strict, so do it here."""
    if params is None:
        return None
    return tuple(int(p) if isinstance(p, bool) else p for p in params)


def _demote_numeric(value: Any) -> Any:
    """Return a Postgres ``numeric`` as the number SQLite would have produced.

    Nothing in this schema *declares* a numeric column — the translation in :data:`_TYPE_MAP`
    only ever emits ``TEXT``/``BIGINT``/``DOUBLE PRECISION``/``BYTEA``. So every ``Decimal``
    that reaches a caller is an aggregate artefact: Postgres widens ``SUM(bigint)`` to
    ``numeric``, whereas SQLite keeps ``SUM`` over an INTEGER column an integer.

    Left alone the difference survives arithmetic and hides, then surfaces at the API edge —
    pydantic renders a ``Decimal`` as the JSON *string* ``"12"``, so a dashboard field the
    frontend reads as a number silently becomes text on Postgres and stays a number on SQLite.
    Int when the value is whole, float otherwise, is exactly what the SQLite path returns.
    """
    if isinstance(value, Decimal):
        whole = int(value)
        return whole if value == whole else float(value)
    return value


def _row_factory(cursor: Any) -> Callable[[Sequence[Any]], PgRow]:
    cols = [c.name for c in (cursor.description or [])]

    def make(values: Sequence[Any]) -> PgRow:
        return PgRow(zip(cols, map(_demote_numeric, values), strict=False))

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

    def __init__(self, conn: Any, pool: Any = None) -> None:
        self._conn = conn
        # When this connection came from a pool, `close()` must hand it back rather than drop it —
        # see `_get_pool`. `None` means the unpooled path, where closing really does close.
        self._pool = pool
        self._closed = False
        self._pk_cache: dict[str, list[str]] = {}
        self._has_id_cache: dict[str, bool] = {}
        self.row_factory: Any = (
            None  # accepted and ignored: rows are already name+index addressable
        )
        self.isolation_level: Any = None

    # -- sqlite3 API ---------------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PgCursor:
        # Transaction control is driven through psycopg's own API: executing a bare COMMIT would
        # desynchronise its transaction state. `_immediate_write` issues these explicitly.
        verb = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        # `BEGIN IMMEDIATE` is NOT plain transaction control — it is the platform's cross-process
        # write mutex, and swallowing it here silently removes the lock that admission control and
        # the audit chain depend on. It goes through translation instead.
        locking = re.match(r"\s*BEGIN\s+(IMMEDIATE|EXCLUSIVE)\b", sql, re.IGNORECASE)
        if verb in ("COMMIT", "ROLLBACK", "BEGIN", "END") and not locking:
            if verb == "COMMIT":
                self.commit()
            elif verb == "ROLLBACK":
                self.rollback()
            return self._noop()
        translated = translate(
            sql,
            pk_lookup=self._primary_key,
            trigger_table=self._trigger_table,
            has_id=self._has_id,
        )
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
        cur.executemany(
            translate(sql, pk_lookup=self._primary_key, has_id=self._has_id),
            [_adapt(p) for p in seq],
        )
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
        """Close, or return to the pool — and only once.

        Idempotent because `get_db()` closes in a `finally` while some callers also close
        explicitly. Handing the same connection back to the pool twice would let two callers hold
        it at once, which is a data race that would only show up under load.
        """
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            # Roll back first. A read leaves the connection INTRANS, and the pool would undo it
            # anyway — but at WARNING level, once per request. Discarding uncommitted work on
            # close is also exactly what `sqlite3` does, so nothing above the seam changes.
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001 - a dead connection is the pool's problem, not ours
                pass
            self._pool.putconn(self._conn)
        else:
            self._conn.close()

    def __enter__(self) -> PgConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.commit() if exc_type is None else self.rollback()

    def _trigger_table(self, name: str) -> str | None:
        """The table a trigger is attached to (Postgres needs it; SQLite does not have it)."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname = %s AND NOT t.tgisinternal",
            (name,),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None

    def _has_id(self, table: str) -> bool:
        """Does this table carry the surrogate ``id`` that stands in for SQLite's ``rowid``?

        A handful of tables key on a natural TEXT id (``judge_calibrations``) and have none.
        """
        if table in self._has_id_cache:
            return self._has_id_cache[table]
        cur = self._conn.cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = 'id' AND table_schema = current_schema()",
            (table,),
        )
        found = cur.fetchone() is not None
        self._has_id_cache[table] = found
        return found

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


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# One pool per (dsn, schema) per process. Opening a Postgres connection costs a TCP round trip, a
# TLS handshake and a backend fork — free on SQLite, and paid on *every* `get_db()` call, of which
# a single dashboard page render makes dozens.
_POOLS: dict[tuple[str, str | None], Any] = {}
_POOL_LOCK = threading.Lock()
_POOL_UNAVAILABLE = False


_OFF = ("0", "false", "no", "off")


def _pooling_enabled() -> bool:
    return os.getenv("EXAMLOPS_POSTGRES_POOL", "1").strip().lower() not in _OFF


def _pool_size() -> tuple[int, int]:
    def _int(name: str, default: int) -> int:
        try:
            return max(0, int(os.getenv(name, "").strip() or default))
        except ValueError:
            return default

    max_size = max(1, _int("EXAMLOPS_POSTGRES_POOL_MAX", 10))
    return min(_int("EXAMLOPS_POSTGRES_POOL_MIN", 1), max_size), max_size


def _connect_timeout() -> float:
    """Seconds to wait for the datastore to answer at all. Deliberately small.

    This is *reachability*, not pool saturation: it bounds how long a process waits before
    concluding the datastore is not there. psycopg_pool's own ``timeout`` (30 s by default) is a
    different question — how long to wait for a *free* connection — and is left alone, because
    shortening it would start failing legitimately-busy pools.
    """
    try:
        return max(0.1, float(os.getenv("EXAMLOPS_POSTGRES_CONNECT_TIMEOUT", "").strip() or 2.0))
    except ValueError:
        return 2.0


# Addresses recently found unreachable, and until when. Without this the probe is paid once per
# *connection*, not once per command — invisible against a refused port (instant) but linear
# against a black-holed host, where the budget is a real wait. Measured: `exa audit` cost 5.6 s
# with a 2 s budget because it opens the datastore twice. The entry expires so a long-lived
# process (dashboard, bridge) recovers on its own when the server comes back.
_UNREACHABLE: dict[tuple[str, int], tuple[float, str]] = {}
_UNREACHABLE_LOCK = threading.Lock()


def _unreachable_ttl() -> float:
    """How long a negative result is trusted. Long enough to cover one CLI command."""
    try:
        ttl = float(os.getenv("EXAMLOPS_POSTGRES_UNREACHABLE_TTL", "").strip() or 5.0)
    except ValueError:
        ttl = 5.0
    return max(0.0, ttl)


def _require_reachable(dsn: str) -> None:
    """Fail fast, and clearly, when nothing is listening at the DSN's host:port.

    Without this, an unreachable datastore costs the *pool's* timeout — 30 s — on the first
    connection of every process, because the background workers retry a refused connect while
    ``getconn()`` waits out its full budget. On a CLI, where each command is a fresh process, that
    is 30 s per command for an operator who is very likely diagnosing the outage itself.

    A TCP probe is the honest test and costs one round trip against a healthy server. It runs only
    when the pool for this DSN does not exist yet — i.e. once per process — so the steady-state
    path is unchanged. Cases libpq handles better than we can are skipped rather than guessed at:
    unix sockets, and multi-host DSNs where failover is the whole point.
    """
    import psycopg  # noqa: PLC0415 - optional enterprise dependency

    try:
        info = psycopg.conninfo.conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - an unparseable DSN is psycopg's error to raise, not ours
        return
    host, port = str(info.get("host") or ""), str(info.get("port") or "5432")
    if not host or host.startswith("/") or "," in host or "," in port:
        return  # unix socket or multi-host failover — let libpq decide
    try:
        port_n = int(port)
    except ValueError:
        return
    key = (host, port_n)
    now = time.monotonic()
    with _UNREACHABLE_LOCK:
        cached = _UNREACHABLE.get(key)
        if cached and now < cached[0]:
            raise psycopg.OperationalError(cached[1])
        if cached:
            del _UNREACHABLE[key]  # expired: probe again rather than grow the dict

    timeout = _connect_timeout()
    try:
        with socket.create_connection((host, port_n), timeout=timeout):
            return
    except OSError as exc:
        message = (
            f"unreachable at {host}:{port_n} after {timeout:g}s ({exc}). "
            f"Check EXAMLOPS_POSTGRES_DSN and that the server is running; "
            f"raise EXAMLOPS_POSTGRES_CONNECT_TIMEOUT if the host is simply slow."
        )
        with _UNREACHABLE_LOCK:
            _UNREACHABLE[key] = (time.monotonic() + _unreachable_ttl(), message)
        raise psycopg.OperationalError(message) from exc


def _get_pool(dsn: str, schema: str | None) -> Any:
    """The pool for this (dsn, schema), or ``None`` to open connections directly.

    Returns ``None`` — rather than raising — when pooling is switched off or ``psycopg_pool`` is
    not installed, because an unpooled connection is slower but correct. A missing optional
    dependency must never be the reason the platform cannot reach its database.
    """
    global _POOL_UNAVAILABLE
    if not _pooling_enabled() or _POOL_UNAVAILABLE:
        return None
    key = (dsn, schema)
    with _POOL_LOCK:
        pool = _POOLS.get(key)
        if pool is not None:
            return pool
        try:
            from psycopg_pool import ConnectionPool  # noqa: PLC0415 - optional dependency
        except ImportError:
            _POOL_UNAVAILABLE = True
            logger.info(
                "psycopg_pool is not installed — opening an unpooled connection per call. "
                "Install 'examlops[postgres]' for pooling."
            )
            return None
        min_size, max_size = _pool_size()
        _require_reachable(dsn)  # before _ensure_schema, which would otherwise hang on connect
        _ensure_schema(dsn, schema)
        pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            # Runs once per *physical* connection, not once per checkout, so the search_path is
            # set exactly where it belongs: on the connection, for its whole life.
            configure=_configure_schema(schema),
            # A pooled connection can be closed under us by a restart, a failover or an idle
            # timeout. Checking it on the way out turns that into a transparent reconnect rather
            # than an error handed to a caller who cannot do anything about it.
            check=ConnectionPool.check_connection,
            open=True,
        )
        _POOLS[key] = pool
        return pool


def _ensure_schema(dsn: str, schema: str | None) -> None:
    """Create the schema once, on its own connection, before the pool opens any.

    ``CREATE SCHEMA IF NOT EXISTS`` is not race-safe in Postgres: the pool opens ``min_size``
    connections concurrently, and running it on each of them raced to a duplicate-key error on
    ``pg_namespace``. Doing it once here is both correct and where a one-time bootstrap belongs —
    and the same race across two *processes* is still possible, so the error is tolerated.
    """
    if not schema:
        return
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid schema name {schema!r}")  # never interpolate raw input
    import psycopg  # noqa: PLC0415 - optional enterprise dependency

    with psycopg.connect(dsn) as conn:
        try:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            conn.commit()
        except psycopg.errors.UniqueViolation:  # another process created it at the same moment
            conn.rollback()


def _configure_schema(schema: str | None) -> Callable[[Any], None] | None:
    if not schema:
        return None
    if not _SCHEMA_RE.match(schema):
        raise ValueError(f"invalid schema name {schema!r}")  # never interpolate raw input

    def _configure(conn: Any) -> None:
        conn.execute(f"SET search_path TO {schema}")
        conn.commit()

    return _configure


@atexit.register
def close_pools() -> None:
    """Close every pool at interpreter exit so a short-lived CLI process does not hang on its
    pool's background worker threads."""
    with _POOL_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:  # noqa: BLE001 - shutting down; nothing useful to do with an error
            pass


def connect(dsn: str, *, schema: str | None = None) -> PgConnection:
    """Open a translating Postgres connection (the SQLite-shaped API above).

    ``schema`` scopes the whole platform to one Postgres schema, created on demand — the
    equivalent of pointing ``PLATFORM_DB`` at a different file. Used for test isolation and for
    keeping several instances on one server.

    Connections come from a per-``(dsn, schema)`` pool when ``psycopg_pool`` is available
    (``EXAMLOPS_POSTGRES_POOL=0`` opts out; ``…_POOL_MIN``/``…_POOL_MAX`` size it). Closing the
    returned object hands the connection back rather than dropping it.
    """
    pool = _get_pool(dsn, schema)
    if pool is not None:
        return PgConnection(pool.getconn(), pool=pool)

    import psycopg  # noqa: PLC0415 - optional enterprise dependency, imported on use

    _require_reachable(dsn)  # the unpooled path pays libpq's own connect budget otherwise
    conn = psycopg.connect(dsn)
    if schema:
        if not _SCHEMA_RE.match(schema):
            raise ValueError(f"invalid schema name {schema!r}")  # never interpolate raw input
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        conn.execute(f"SET search_path TO {schema}")
        conn.commit()
    return PgConnection(conn)
