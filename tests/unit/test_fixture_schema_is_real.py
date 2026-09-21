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
    "jobs": "tests/unit/test_dataplane_sql.py — the remote source database a SQL connector pulls"
    " from, not platform state",
    # tests/integration/test_postgres_service_roles_live.py — stand-ins for what an existing
    # install's MLflow and Prefect databases hold, owned by the superuser, so the test can show
    # the service roles taking them over. Not platform state.
    "checkpoints": "tests/unit/test_suspend_seam.py — the agent-owned LangGraph SqliteSaver table,"
    " read-only from core; its DDL is copied from the agent's store, not platform state",
    "legacy_models": "test_postgres_service_roles_live.py — a superuser-owned MLflow-side table",
    "dashboard_comments": "test_postgres_service_roles_live.py — likewise",
    "legacy_flow_run": "test_postgres_service_roles_live.py — a superuser-owned Prefect-side table",
    "roles_check": "test_postgres_service_roles_live.py — a table the mlflow role creates",
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


def _migration_columns() -> dict[str, set[str]]:
    """Columns added after the fact by ``platform_db._COLUMN_MIGRATIONS``.

    These are real product columns, but they are added by an idempotent ``ALTER TABLE`` at
    init rather than written into the base ``CREATE TABLE`` — so a scrape that only reads
    CREATE statements does not see them (``audit_events.tenant``/``prev_hash``/``hash``,
    ``model_costs.project``, and others). Missing them would make a fixture that declares one
    look like it invented a column, and would make the base CREATE look like it had drifted
    from a second definition that spells the column out inline.
    """
    src = (_REPO / "platform" / "cli" / "src" / "examlops" / "platform_db.py").read_text()
    for node in ast.walk(ast.parse(src)):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            first = node.targets[0]
            target = first.id if isinstance(first, ast.Name) else None
        if target == "_COLUMN_MIGRATIONS" and node.value is not None:
            table_map = ast.literal_eval(node.value)
            return {t.lower(): {c.lower() for c in cols} for t, cols in table_map.items()}
    return {}


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
    for table, cols in _migration_columns().items():
        schema.setdefault(table, set()).update(cols)
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


# ── the product must not describe one table two ways ────────────────────────────────────────
#
# The checks above compare *tests* against the product. This one compares the product against
# itself, and it exists because the dashboard's projects router carried its own CREATE TABLE for
# eight tables under a docstring claiming it matched ``platform_db`` — while three of them did
# not. Its ``authz_relations`` lacked ``UNIQUE (subject, relation, object)``, so on a database the
# dashboard initialised first, ``exa project add-member`` failed outright with "ON CONFLICT clause
# does not match any PRIMARY KEY or UNIQUE constraint". Nothing caught it: both sides used
# ``CREATE TABLE IF NOT EXISTS``, so whichever ran first silently won.

# Alembic migrations are *meant* to redefine a table as the schema evolves; comparing revision N
# against revision N+1 would report every legitimate migration as a divergence.
_MIGRATIONS = ("alembic", "versions", "migrations")


def _constraints(body: str) -> set[str]:
    """Table-level constraints, plus the per-column ones that change behaviour."""
    body = re.sub(r"--[^\n]*", "", body)
    parts, depth, cur = [], 0, []
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

    out: set[str] = set()
    for part in parts:
        tok = part.strip().split()
        if not tok:
            continue
        norm = " ".join(part.split()).upper()
        if tok[0].strip('"').lower() in _CONSTRAINTS:
            out.add(norm)
            continue
        col = tok[0].strip('"').lower()
        for kw in ("NOT NULL", "PRIMARY KEY", "UNIQUE", "AUTOINCREMENT"):
            if kw in norm:
                out.add(f"{col}:{kw}")
        default = re.search(r"DEFAULT\s+(\S+)", norm)
        if default:
            out.add(f"{col}:DEFAULT {default.group(1)}")
    return out


def test_the_product_declares_each_table_only_one_way():
    """Two CREATE TABLEs for one table must agree on columns *and* constraints."""
    defs: dict[str, dict[str, tuple[set[str], set[str]]]] = {}
    for py in _sources(_PRODUCT, tests=False):
        if any(part in _MIGRATIONS for part in py.parts):
            continue
        for _, sql in _sql_literals(py):
            for m in _CREATE.finditer(sql):
                table = m.group(1).lower().split(".")[-1]
                body = _body(sql, m.end() - 1)
                where = str(py.relative_to(_REPO))
                cols, cons = defs.setdefault(table, {}).get(where, (set(), set()))
                defs[table][where] = (cols | _columns(body), cons | _constraints(body))

    problems = []
    for table, per_file in sorted(defs.items()):
        if len(per_file) < 2:
            continue
        files = list(per_file)
        first = per_file[files[0]]
        if all(per_file[f] == first for f in files[1:]):
            continue
        later = _migration_columns().get(table, set())
        all_cols = set().union(*(c for c, _ in per_file.values())) - later
        all_cons = set().union(*(k for _, k in per_file.values()))
        for where, (cols, cons) in per_file.items():
            missing = sorted((all_cols - cols) | {f"[{c}]" for c in (all_cons - cons)})
            if missing:
                problems.append(f"{table}: {where} is missing {missing}")

    assert not problems, (
        "a table is declared two different ways in the product; whichever CREATE TABLE runs "
        "first wins and the other side's assumptions silently break:\n  "
        + "\n  ".join(problems)
        + "\nDeclare it once in platform_db.py and have the other caller use init_db()."
    )


def test_no_dashboard_fixture_builds_its_own_schema():
    """The dashboard tests must use the product's schema, never a hand-written copy of it.

    The checks above catch a fixture that *invents* a column. They cannot catch one that quietly
    *drops* one: a fixture declaring a subset of the real table still passes, while testing
    against a schema laxer than production — a missing NOT NULL or default is invisible until it
    is not. Two of the fixtures converted here did exactly that, declaring ``audit_events``
    without ``NOT NULL`` on ``source``/``action``.

    Calling ``platform_db.init_db()`` removes the whole class, because there is then only one
    definition. For the few tables ``init_db()`` deliberately does not own — ``connections`` and
    ``workbenches``, each created by its own module on first write — seed through that module's
    own API (see ``test_connections_workbenches.py``), which is also the only way the secret
    takes its real indirection.
    """
    dashboard = _REPO / "platform" / "services" / "dashboard" / "backend" / "tests"
    offenders = [
        f"{py.relative_to(_REPO)}:{line} declares {m.group(1)}"
        for py in _sources([dashboard], tests=True)
        for line, sql in _sql_literals(py)
        for m in _CREATE.finditer(sql)
    ]
    assert not offenders, (
        "a dashboard test builds its own schema instead of using the product's:\n  "
        + "\n  ".join(offenders)
        + "\nUse platform_db.init_db(), plus the owning module's API for connections/workbenches."
    )
