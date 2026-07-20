#!/usr/bin/env python3
"""Platform ⟂ use-case boundary guard (ADR 0094).

Fails if the ExaMLOps **platform library** (``platform/cli/src/examlops``) or the **pipeline engine**
(``pipelines/``) imports the use-case model library directly. Content that runs *on top of* the
platform must live in a use-case pack (``usecases/<name>/``) and be reached through the loader seam
(``pipelines.usecase`` / ``examlops.usecase``), never by importing ``seanergys_modelzoo`` in core.

String *specs* (e.g. the loader's legacy-fallback ``"seanergys_modelzoo.…:X"``) are allowed — only
real ``import`` statements are banned. The pack, tests, and CI deploy scripts are out of scope.

Usage:  python platform/ci/check_usecase_boundary.py   (exit 1 on any violation)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Trees that must stay use-case-agnostic.
_SCOPED_ROOTS = [
    _REPO_ROOT / "platform" / "cli" / "src" / "examlops",
    _REPO_ROOT / "pipelines",
]

# Excluded: the pack itself, tests, caches, CI deploy scripts (deployment glue, not the library).
_EXCLUDE_PARTS = {"__pycache__", "tests", "test", ".venv", "usecases"}

# A real import of the use-case model library — banned in scope.
_BANNED_IMPORT = re.compile(r"^\s*(?:from|import)\s+seanergys_modelzoo\b")


def _iter_py(root: Path):
    for path in root.rglob("*.py"):
        if any(part in _EXCLUDE_PARTS for part in path.parts):
            continue
        yield path


def find_violations() -> list[str]:
    violations: list[str] = []
    for root in _SCOPED_ROOTS:
        if not root.is_dir():
            continue
        for path in _iter_py(root):
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if _BANNED_IMPORT.match(line):
                    rel = path.relative_to(_REPO_ROOT)
                    violations.append(f"{rel}:{lineno}: {line.strip()}")
    return violations


def main() -> int:
    violations = find_violations()
    if violations:
        print("Platform/use-case boundary violations (ADR 0094):", file=sys.stderr)
        print(
            "  the platform core / pipeline engine must not import 'seanergys_modelzoo' directly —\n"
            "  reach pack content through the loader seam (pipelines.usecase / examlops.usecase).\n",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("✓ platform ⟂ use-case boundary clean (no direct seanergys_modelzoo imports in core).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
