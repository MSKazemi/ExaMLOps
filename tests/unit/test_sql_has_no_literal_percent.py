"""A literal ``%`` in shared SQL is a placeholder to psycopg, so the statement never runs.

Postgres is reached through psycopg, and psycopg parses the *statement* for placeholders whenever
parameters are passed. `LIKE 'train:%'` is therefore not a pattern, it is a broken placeholder:

    psycopg.errors.ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders, got '%''

Two real defects were found this way, both silent because their callers swallow bookkeeping errors:

- `lineage_run_for_mlflow_run` matched `job LIKE 'train:%'`, so **no cost or carbon lineage was ever
  recorded on a Postgres install** — `attach_run_cost` logged a warning and returned False;
- the agent's platform-ops error listing matched `action LIKE '%fail%'`, so it raised on Postgres.

Two more were latent: they pass no parameters today, which is the only reason they work, and adding
one would break them. The fix in every case is to bind the pattern (`LIKE ?` with `'train:%'`), which
is what this guard requires.

Doubling the `%` is *not* an alternative: it would then be a literal `%%` on SQLite, and the platform
runs on both.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.unit._guard_deps import scan_files

ROOT = Path(__file__).resolve().parents[2]
#: Where the platform's datastore SQL lives. The dashboard and agent reach the same store.
ROOTS = (
    ROOT / "platform" / "cli" / "src",
    ROOT / "platform" / "services",
    ROOT / "pipelines",
    ROOT / "serving",
)

#: SQL that can only ever run on SQLite, with the reason. `sqlite_master` is not a Postgres table:
#: these statements are the SQLite backup tier reading its own file, and psycopg never sees them.
SQLITE_ONLY = {
    "platform/cli/src/examlops/backup/sqlite_tier.py": "reads sqlite_master in the SQLite tier",
}

#: A quoted SQL literal containing a percent, on the right of a `LIKE`/`ILIKE`.
_LIKE_WITH_PERCENT = re.compile(r"""(?:I?LIKE)\s+(['"])[^'"]*%[^'"]*\1""", re.IGNORECASE)

#: A `LIKE '` left hanging at the end of an f-string fragment — the pattern arrives by
#: interpolation, so the `%` still reaches psycopg and the value is not bound either.
_LIKE_INTERPOLATED = re.compile(r"""(?:I?LIKE)\s+['"]$""", re.IGNORECASE)


def _sql_strings(path: Path) -> list[tuple[int, str]]:
    """Every SQL-carrying string in a module, as the interpreter sees it — not as a line reader does.

    This started as a line-by-line regex and had two blind spots, both found by probing it rather
    than by reading it:

    - **the formatter splits long SQL.** At this repo's 100-character limit, `… WHERE job LIKE "`
      and `"'train:%' AND id=?"` land on different lines, and a per-line scan sees neither. Python
      merges adjacent string literals at parse time, so the AST sees the whole statement;
    - **the pattern can arrive by interpolation.** `f"… LIKE '{pattern}'"` reaches psycopg with the
      same literal `%`, and is an unbound value besides.

    Docstrings are skipped by identity, which also removes the old prose heuristics: comments are
    not in the AST at all, and a docstring quoting the broken pattern — as this file's does — is
    prose that cannot reach a database.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - a broken module fails loudly elsewhere
        return []
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in docstrings:
                continue
            if _LIKE_WITH_PERCENT.search(node.value):
                found.append((node.lineno, " ".join(node.value.split())[:120]))
        elif isinstance(node, ast.JoinedStr):
            rendered = "".join(
                v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
            )
            before_first_hole = rendered.split("{}")[0]
            if _LIKE_WITH_PERCENT.search(rendered) or _LIKE_INTERPOLATED.search(before_first_hole):
                found.append((node.lineno, " ".join(rendered.split())[:120]))
    return found


def test_no_shared_sql_inlines_a_like_pattern():
    offenders: list[str] = []
    for root in ROOTS:
        for path in scan_files(root):
            rel = path.relative_to(ROOT).as_posix()
            if rel in SQLITE_ONLY or "/tests/" in rel:
                continue
            for n, text in _sql_strings(path):
                offenders.append(f"{rel}:{n}: {text}")
    assert not offenders, (
        "these inline a LIKE pattern into the statement, which psycopg reads as a placeholder — "
        "bind it instead (`LIKE ?` with the pattern as a parameter):\n" + "\n".join(offenders)
    )


def test_the_exemptions_still_describe_something_real():
    for rel in SQLITE_ONLY:
        path = ROOT / rel
        assert path.exists(), rel
        assert _sql_strings(path), f"{rel} no longer inlines a pattern; drop the exemption"
        assert "sqlite_master" in path.read_text(encoding="utf-8"), rel


def test_the_pattern_matches_what_it_is_for():
    """The regex is the guard; a guard that matches nothing is worse than none."""
    assert _LIKE_WITH_PERCENT.search("WHERE job LIKE 'train:%' ORDER BY id")
    assert _LIKE_WITH_PERCENT.search('action LIKE "%fail%"')
    assert not _LIKE_WITH_PERCENT.search("WHERE job LIKE ? ORDER BY id")
    assert not _LIKE_WITH_PERCENT.search("percent = 50  # 100% of the time")


#: Every way this defect can be written, and the two the line-based scanner could not see.
_SHAPES = {
    "one line": "conn.execute(\"SELECT a FROM t WHERE job LIKE 'train:%' AND id=?\", (x,))\n",
    "split by the formatter": (
        'conn.execute(\n    "SELECT a FROM t WHERE job LIKE "\n    "\'train:%\' AND id=?",\n'
        "    (x,),\n)\n"
    ),
    "interpolated": 'p = "train:%"\nconn.execute(f"SELECT a WHERE job LIKE \'{p}\'", (x,))\n',
    "ILIKE": "conn.execute(\"SELECT a FROM t WHERE job ILIKE 'train:%'\", ())\n",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_every_way_of_writing_it_is_caught(shape, tmp_path):
    """The guard is only as good as the spellings it sees.

    Two of these were invisible until the scanner was probed with them: Python merges adjacent
    string literals, so the formatter splitting a long statement hid it from a line reader, and an
    interpolated pattern never looked like a literal at all.
    """
    module = tmp_path / f"case_{shape.replace(' ', '_')}.py"
    module.write_text(_SHAPES[shape])
    assert _sql_strings(module), f"a {shape} LIKE pattern is invisible to this guard"


def test_prose_that_explains_the_defect_is_not_flagged(tmp_path):
    """Otherwise the guard cannot be documented, including in its own docstring."""
    module = tmp_path / "prose.py"
    module.write_text(
        '"""A module docstring explaining that job LIKE \'train:%\' is the broken form."""\n'
        "\n"
        "def f():\n"
        '    """Do not write job LIKE \'train:%\' here either."""\n'
        "    return 1\n"
    )
    assert not _sql_strings(module)
