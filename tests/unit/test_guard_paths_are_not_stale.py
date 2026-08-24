"""A guard that scans a path which no longer exists reports the cleanest possible result.

`test_platform_db_coupling_ratchet.py` was found this way. Its baseline is 0, so its entire
content is the negative claim "no module imports `platform_db` directly" — and with its scan root
pointed at a directory that does not exist, `rglob` yielded nothing, zero importers were found,
`0 <= 0` held, and both of its tests passed. Total compliance and a stale path produce the same
green.

This repo has already moved that tree once, into `platform/`, which is exactly how a root goes
stale: the guard keeps passing, so nobody looks at it again.

So: every hard-coded repo path a test module binds at import time must exist. This is deliberately
narrower and more mechanical than "every guard must assert it found something" — it checks a fact
(the path resolves) rather than trying to recognise an assertion style, which is the kind of
detector that quietly stops matching and becomes the very thing it is guarding against.

A guard whose path is right can still scan an empty directory, so the strongest guards also assert
their own non-emptiness (see `_python_files` in `test_no_bare_sqlite_connect.py`). This does not
replace that; it catches the failure that has actually happened here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_UNIT = _ROOT / "tests" / "unit"


def _module_level_repo_paths() -> list[tuple[Path, str, Path]]:
    """Every ``NAME = Path(__file__)...parents[n] / ...`` bound at module level in tests/unit.

    Evaluated rather than pattern-matched, so the answer is the path the test will really use.
    Only expressions built from `Path(__file__)` are evaluated, and only with `Path` and
    `__file__` in scope — nothing else in the module is executed.
    """
    found: list[tuple[Path, str, Path]] = []
    for f in sorted(_UNIT.rglob("test_*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken test file fails loudly elsewhere
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            src = ast.unparse(node.value)
            if "Path(__file__)" not in src or "parents[" not in src:
                continue
            try:
                value = eval(src, {"Path": Path, "__file__": str(f)})  # noqa: S307
            except Exception:  # noqa: BLE001 - a path we cannot evaluate is not a path we can judge
                continue
            if isinstance(value, Path):
                found.append((f, target.id, value))
    return found


def test_the_check_found_paths_to_check():
    """This guard is subject to its own finding — an empty scan would make it vacuous too."""
    paths = _module_level_repo_paths()
    assert len(paths) > 20, (
        f"only evaluated {len(paths)} module-level repo paths across {_UNIT}; the scan is broken, "
        "which would make the assertion below pass without checking anything"
    )


def test_no_guard_scans_a_path_that_does_not_exist():
    stale = [
        f"{f.relative_to(_ROOT)}: {name} -> {value}"
        for f, name, value in _module_level_repo_paths()
        if not value.exists()
    ]
    assert not stale, (
        "these test modules bind a repo path that no longer exists, so whatever they scan is "
        "empty and whatever they conclude from it is vacuous:\n  " + "\n  ".join(stale)
    )
