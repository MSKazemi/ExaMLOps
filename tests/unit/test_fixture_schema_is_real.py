"""Guard: a test fixture may not invent a table shape the product does not have.

Most fixtures in this repo seed state by running their own ``CREATE TABLE`` rather than building
the real schema. That is a second, unmaintained copy of something the product already defines, and
on SQLite the copy **wins** — every test gets its own database file, so an invented shape is never
confronted with the real one and can survive indefinitely.

It is not hypothetical. Two have been found by accident:

- ``scale_events.direction`` — a column the product has never had. A test asserted on it for as
  long as it existed, and only surfaced when the suite was pointed at Postgres, where the real
  schema wins because ``CREATE TABLE IF NOT EXISTS`` is a no-op against an existing table.
- ``project_budgets(project, gpu_hours, cost_usd)`` — the real columns are ``gpu_hours_budget``
  and ``cost_budget``. Found by this check, before anything tried to read it.

The subtler cost is a fixture that is a column *subset* of the real table: the product may depend
on a ``NOT NULL`` or a default the fixture quietly dropped, so the test passes against a schema
laxer than production. This guard does not catch that — use ``platform_db.init_db()`` and the
question does not arise — but it does catch every invention.

The schema is read from the product's own source rather than from a list kept here, because a list
kept here would be a third copy with the same failure mode. That means tables created lazily by
their owning module (``connections``, ``workbenches``) are found too; an earlier draft of this
check knew only about ``init_db()`` and reported both as fictional.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# Where the product defines its schema, and where fixtures consume it.
_PRODUCT = [_REPO / "platform" / "cli" / "src" / "examlops", _REPO / "platform" / "services"]
_TESTS = [_REPO / "tests", _REPO / "platform" / "services" / "dashboard" / "backend" / "tests"]

_CREATE = re.compile(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"?([\w.]+)"?\s*\(', re.I)
_ALTER = re.compile(r'ALTER\s+TABLE\s+"?(\w+)"?\s+ADD\s+COLUMN\s+"?(\w+)"?', re.I)

# A column definition starts with the column name; these start a table *constraint* instead.
_CONSTRAINTS = {"primary", "foreign", "unique", "check", "constraint"}

# Scratch tables that are the subject of a test rather than a stand-in for product state: these
# exercise the SQL machinery itself (retry/timeout behaviour, dialect translation), so they are
# deliberately not part of any schema.
_SCRATCH = {
    "t": "tests/unit/test_resilience.py — a table to run a retryable statement against",
    "x": "tests/unit/test_storage_pg_translate.py — a table to translate DDL for",
    "a": "tests/unit/test_storage_pg_translate.py — likewise",
}


def _sql_literals(py: Path) -> list[tuple[int, str]]:
    """Every string literal in *py* that mentions ``CREATE TABLE``, as ``(line, value)``.

    Reading literal *values* rather than raw file text matters more than it looks. DDL here is
    routinely written as adjacent string literals::

        "CREATE TABLE IF NOT EXISTS audit_events ("
        "id INTEGER PRIMARY KEY, ts DATETIME, source TEXT, actor TEXT, "
        "action TEXT, target TEXT, details TEXT)"

    Scanning raw text leaves the quote characters sitting inside the column list, so the column at
    each seam (``action``, here) parses as garbage and is silently dropped. In a guard whose entire
    job is to have no false negatives, that is the worst possible bug — and it is what the first
    version of this file did. Python has already joined those literals by the time they are AST
    constants, so this cannot happen.
    """
    try:
        tree = ast.parse(py.read_text())
    except SyntaxError:  # pragma: no cover - a file that does not parse is not this test's problem
        return []
    return [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "CREATE TABLE" in n.value.upper()
    ]


def _body(src: str, open_paren: int) -> str:
    """The text between a ``CREATE TABLE (`` and its matching ``)``."""
    depth = 0
    for i in range(open_paren, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[open_paren + 1 : i]
    return ""


def _columns(body: str) -> set[str]:
    body = re.sub(r"--[^\n]*", "", body)  # a `-- comment` is not a column
    parts: list[str] = []
    depth: int = 0
    cur: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))

    names = set()
    for part in parts:
        tok = part.strip().split()
        if not tok:
            continue  # a trailing comma before `)` leaves an empty part
        name = tok[0].strip('"').lower()
        if name and name not in _CONSTRAINTS:
            names.add(name)
    return names


def _sources(roots: list[Path], *, tests: bool) -> list[Path]:
    out = []
    for root in roots:
        for py in sorted(root.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            if ("tests" in py.parts) is tests:
                out.append(py)
    return out


def _product_schema() -> dict[str, set[str]]:
    """table -> every column the product declares for it, anywhere."""
    schema: dict[str, set[str]] = {}
    for py in _sources(_PRODUCT, tests=False):
        for _, sql in _sql_literals(py):
            for m in _CREATE.finditer(sql):
                table = m.group(1).lower().split(".")[-1]
                schema.setdefault(table, set()).update(_columns(_body(sql, m.end() - 1)))
        for m in _ALTER.finditer(py.read_text()):  # additive migrations add columns later
            schema.setdefault(m.group(1).lower(), set()).add(m.group(2).lower())
    return schema


def _fixture_tables():
    for py in _sources(_TESTS, tests=True):
        if py.name == Path(__file__).name:
            continue
        for line, sql in _sql_literals(py):
            for m in _CREATE.finditer(sql):
                table = m.group(1).lower().split(".")[-1]
                yield py, line, table, _columns(_body(sql, m.end() - 1))


def test_the_product_schema_is_discoverable():
    """If the scrape breaks, every other assertion here passes vacuously."""
    schema = _product_schema()
    assert len(schema) > 100, f"only found {len(schema)} tables — the schema scrape is broken"
    # A table from `init_db()` and one created lazily by its own module.
    assert "audit_events" in schema
    assert "connections" in schema, "lazily-created tables must be found, or they look fictional"
    assert "gpu_hours_budget" in schema["project_budgets"]


def test_no_fixture_declares_a_table_the_product_does_not_have():
    schema = _product_schema()
    unknown = [
        f"{py.relative_to(_REPO)}:{line} creates '{table}'"
        for py, line, table, _ in _fixture_tables()
        if table not in schema and table not in _SCRATCH
    ]
    assert not unknown, (
        "these fixtures create tables the product never defines:\n  "
        + "\n  ".join(unknown)
        + "\nEither the table was renamed and the fixture was not, or it is a scratch table — "
        "in which case add it to _SCRATCH with the reason."
    )


def test_no_fixture_declares_a_column_the_product_does_not_have():
    schema = _product_schema()
    invented = [
        f"{py.relative_to(_REPO)}:{line} — {table} declares {sorted(cols - schema[table])}"
        for py, line, table, cols in _fixture_tables()
        if table in schema and cols - schema[table]
    ]
    assert not invented, (
        "these fixtures declare columns the product does not have:\n  "
        + "\n  ".join(invented)
        + "\nA fixture that invents a column tests a schema that does not exist. Seed with "
        "`platform_db.init_db()` instead of hand-rolling the DDL."
    )
