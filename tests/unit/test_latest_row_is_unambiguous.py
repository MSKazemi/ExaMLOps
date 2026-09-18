"""`ORDER BY <time> ... LIMIT 1` on an append-only table must break the tie.

SQLite's `CURRENT_TIMESTAMP` has **one-second resolution**. On a table that keeps every write — a
new `id` per row rather than one row per key — two writes in the same second are not a rare race,
they are what a CI job, a regenerate-all script or a loop over models does routinely. `LIMIT 1`
then does not return *the latest* record; it returns whichever of the tied rows the query plan
reaches first, which in SQLite is the **oldest**, stably enough to look right.

The distinction this file rests on is between two kinds of table:

* **one row per key** (`traffic_rules`, `shadow_config`: `model TEXT PRIMARY KEY`) — there is
  nothing to tie, and demanding a tiebreaker there would be noise;
* **append-only** (`id INTEGER PRIMARY KEY AUTOINCREMENT`) — every write is a new row, so "the
  latest" is a real choice and `id` is the only thing that settles it.

The guard therefore reads the schema to decide which tables it applies to, rather than carrying a
hand-written list of the files that were fixed.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))

#: `ORDER BY <col> DESC ... LIMIT 1`, capturing the column and any tiebreaker that follows.
_ORDER_LIMIT_1 = re.compile(
    r"ORDER BY\s+(\w+)\s+DESC((?:\s*,\s*\w+(?:\s+(?:ASC|DESC))?)*)\s+LIMIT\s+1\b", re.I
)

_TIME_COLS = {"ts", "created_at", "updated_at", "started_at", "finished_at", "set_at"}


def _append_only_tables() -> set[str]:
    """Tables whose DDL gives every row its own id — so a key can hold many rows."""
    ddl = (ROOT / "platform/cli/src/examlops/platform_db.py").read_text()
    return {
        m.group(1)
        for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((?:[^;]*?)AUTOINCREMENT", ddl, re.S
        )
    }


def test_the_schema_scan_finds_both_kinds_of_table():
    """The guard is worthless if its own classifier is wrong, so check it against known cases."""
    append_only = _append_only_tables()
    assert {"model_cards", "ab_tests", "hpo_studies"} <= append_only
    assert not ({"traffic_rules", "shadow_config"} & append_only), (
        "these are one row per model (`model TEXT PRIMARY KEY`); flagging them would be noise"
    )


def _sql_literals_by_function(path: Path):
    """Yield (function name, line, sql string) for every string literal in the file."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:  # pragma: no cover - not a Python file we can read
        return
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                yield fn.name, fn.lineno, n.value


def test_no_append_only_table_picks_its_latest_row_by_time_alone():
    append_only = _append_only_tables()
    offenders: list[str] = []
    for src in sorted(ROOT.glob("platform/**/*.py")) + sorted(ROOT.glob("serving/**/*.py")):
        sp = str(src)
        if "/build/" in sp or "/tests/" in sp or src.name.startswith("test_"):
            continue
        for fn_name, lineno, sql in _sql_literals_by_function(src):
            m = _ORDER_LIMIT_1.search(sql)
            if not m or m.group(1).lower() not in _TIME_COLS:
                continue
            tables = {t.lower() for t in re.findall(r"FROM\s+(\w+)", sql, re.I)}
            if not (tables & append_only):
                continue  # one row per key: nothing to tie
            if re.search(r"\b(?:id|rowid)\b", m.group(2), re.I):
                continue  # already broken by the row's own order
            offenders.append(
                f"{src.relative_to(ROOT)}:{lineno} {fn_name}(): {m.group(0).strip()!r} "
                f"over {sorted(tables & append_only)}"
            )
    assert not offenders, (
        "these pick 'the latest' row of an append-only table by a one-second timestamp alone. "
        "Two writes in the same second tie, and SQLite returns the OLDEST of them — the dashboard "
        "served the first of three model cards written in one second as the current one. Add "
        "`, id DESC`:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("n", [3])
def test_the_tie_is_real_and_id_settles_it(tmp_path, monkeypatch, n):
    """Demonstrate the defect the guard exists for, so the rule is not folklore."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb

    pdb.init_db()
    with pdb.get_db() as conn:
        for i in range(1, n + 1):
            conn.execute(
                "INSERT INTO model_cards (ts, model, output_path, actor) "
                "VALUES ('2026-09-15 12:00:00', 'JPCP', ?, 'ci')",
                (f"card_v{i}.md",),
            )
        by_time = conn.execute(
            "SELECT output_path FROM model_cards WHERE model='JPCP' ORDER BY ts DESC LIMIT 1"
        ).fetchone()[0]
        by_chain = conn.execute(
            "SELECT output_path FROM model_cards WHERE model='JPCP' "
            "ORDER BY ts DESC, id DESC LIMIT 1"
        ).fetchone()[0]
    assert by_time == "card_v1.md", "SQLite returns the oldest of a tied group — stably"
    assert by_chain == f"card_v{n}.md"


#: `ORDER BY <time> DESC` with nothing after it to settle a tie.
_ORDER_NO_TIE = re.compile(
    r"ORDER BY\s+(?:ts|created_at|updated_at|started_at|finished_at|set_at)\s+DESC(?!\s*,\s*\w+)",
    re.I,
)


def _first_row_of_an_untied_read(tree: ast.AST, src: str):
    """Yield (line, expression) where a name bound from an untie-broken read is taken at [0]/[-1].

    A `LIMIT N` read is only safe while the caller *renders* the rows. `rows[0]` turns it back
    into "the latest record", with the tie unresolved — a `LIMIT 1` wearing a `LIMIT N`, which the
    LIMIT-1 guard above cannot see.
    """
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        tainted = {
            t.id
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _ORDER_NO_TIE.search(ast.get_source_segment(src, node.value) or "")
            for t in node.targets
            if isinstance(t, ast.Name)
        }
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in tainted
                and isinstance(node.slice, ast.Constant)
                and node.slice.value in (0, -1)
            ):
                yield node.lineno, f"{fn.name}(): {node.value.id}[{node.slice.value}]"


def test_the_first_row_detector_can_actually_see_the_shape(tmp_path):
    """A sweep that finds nothing is a claim about the detector, so prove it on a planted case."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def latest(conn):\n"
        "    rows = conn.execute('SELECT * FROM t ORDER BY ts DESC LIMIT 20').fetchall()\n"
        "    return rows[0]\n"
        "def safe(conn):\n"
        "    rows = conn.execute('SELECT * FROM t ORDER BY ts DESC, id DESC LIMIT 20').fetchall()\n"
        "    return rows[0]\n"
    )
    src = planted.read_text()
    found = list(_first_row_of_an_untied_read(ast.parse(src), src))
    assert [f for _, f in found] == ["latest(): rows[0]"], found


def test_nothing_takes_the_first_row_of_an_untie_broken_read():
    """A ratchet at zero: today no caller does this, and that is worth keeping true.

    This is the case the `LIMIT 1` guard cannot reach — `get_gate_reports` reads `LIMIT ?` and is
    handed to an agent that takes entry zero as the standing gate verdict.
    """
    offenders: list[str] = []
    for src_file in sorted(ROOT.glob("platform/**/*.py")) + sorted(ROOT.glob("serving/**/*.py")):
        sp = str(src_file)
        if "/build/" in sp or "/tests/" in sp or src_file.name.startswith("test_"):
            continue
        src = src_file.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for lineno, what in _first_row_of_an_untied_read(tree, src):
            offenders.append(f"{src_file.relative_to(ROOT)}:{lineno} {what}")
    assert not offenders, (
        "these take the first row of a read ordered by a one-second timestamp with no tiebreaker "
        "— the tie is unresolved and SQLite returns the oldest of it:\n  " + "\n  ".join(offenders)
    )


#: Consuming a window as a *sequence* rather than as a bag of values.
_ORDER_DEPENDENT = re.compile(
    r"\benumerate\(|\bzip\(|\[1:\]|\[:-1\]|\[::-1\]|reversed\(|"
    r"slope|trend|linregress|polyfit|np\.diff|ewm",
    re.I,
)
_WINDOWED_READ = re.compile(
    r"ORDER BY\s+(?:ts|created_at|updated_at|started_at|finished_at)\s+(?:DESC|ASC)[^\"']*LIMIT",
    re.I,
)
_WINDOW_TIEBROKEN = re.compile(
    r"ORDER BY\s+(?:ts|created_at|updated_at|started_at|finished_at)\s+DESC\s*,\s*(?:id|rowid)",
    re.I,
)


def _sequence_windows(tree: ast.AST, src: str):
    """Yield (line, name) for functions that read a time window AND treat it as a sequence."""
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        seg = ast.get_source_segment(src, fn) or ""
        if not _WINDOWED_READ.search(seg) or not _ORDER_DEPENDENT.search(seg):
            continue
        if _WINDOW_TIEBROKEN.search(seg):
            continue
        yield fn.lineno, fn.name


def test_the_sequence_window_detector_can_see_the_shape(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def untied(conn, m):\n"
        "    rows = conn.execute('SELECT v FROM t ORDER BY ts DESC LIMIT 50').fetchall()\n"
        "    vals = [r['v'] for r in reversed(rows)]\n"
        "    return vals[-1] - vals[0]\n"
        "def tied(conn, m):\n"
        "    rows = conn.execute('SELECT v FROM t ORDER BY ts DESC, rowid DESC LIMIT 50').fetchall()\n"
        "    vals = [r['v'] for r in reversed(rows)]\n"
        "    return vals[-1] - vals[0]\n"
    )
    src = planted.read_text()
    assert [n for _, n in _sequence_windows(ast.parse(src), src)] == ["untied"]


def test_a_window_read_as_a_sequence_is_tie_broken():
    """A mean does not care what order the rows arrive in. A trend does.

    `ORDER BY ts DESC LIMIT N` feeding an average is fine — that was checked, and it is why the
    other windowed reads are deliberately left alone. But the moment a window is read as a
    *sequence* — reversed into oldest-first, fitted for a slope, differenced, enumerated — the tie
    stops being invisible and starts choosing which samples sit at the ends of the series.
    `forecast_model_drift` is the one such read today, and it is tie-broken; this keeps that true
    and catches the next one.
    """
    offenders: list[str] = []
    for src_file in sorted(ROOT.glob("platform/**/*.py")) + sorted(ROOT.glob("serving/**/*.py")):
        sp = str(src_file)
        if "/build/" in sp or "/tests/" in sp or src_file.name.startswith("test_"):
            continue
        src = src_file.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for lineno, name in _sequence_windows(tree, src):
            offenders.append(f"{src_file.relative_to(ROOT)}:{lineno} {name}()")
    assert not offenders, (
        "these read a time window and consume it as an ordered sequence, but leave the `ts` tie "
        "to the query plan — so which samples land at the ends of the series is arbitrary:\n  "
        + "\n  ".join(offenders)
    )
