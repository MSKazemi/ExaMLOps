#!/usr/bin/env python3
"""Every mermaid diagram in the published documentation parses.

The site renders ```mermaid fences in the reader's browser (Material loads mermaid itself), so
nothing at build time reads them: `mkdocs build --strict` is happy with a diagram that mermaid
cannot parse, and the reader gets a red error box. Eleven diagrams went unchecked for months that
way and two of them were broken; four more have been added since, and nothing has looked at them.

This runs the real grammar — `mermaid.parse` from the same major version the site loads — over
every fence, so a broken diagram fails the build instead of a reader's page. A pattern of our own
would be the wrong tool twice over: it would miss real breakage, and it would reject valid
diagrams, which is how a check earns a reputation for noise and gets switched off.

Only `docs/` is checked. That is what is published; the diagrams in `design/` are non-normative
dated design records that no site renders.

    python platform/ci/check_mermaid.py            # the documentation tree
    python platform/ci/check_mermaid.py docs/guides/architecture.md

Exit codes: 0 every diagram parses · 1 at least one does not · 2 the checker itself cannot run
(no node, or `npm ci --prefix platform/ci/mermaid` never ran). A missing toolchain is never
reported as success — a check that silently passes when it did not run is worse than no check.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_DIR = Path(__file__).resolve().parent / "mermaid"
DOCS_DIR = REPO_ROOT / "docs"

#: A fence opens with at least three backticks followed by the language. Material's own docs use
#: four when a diagram contains a fence, so the count is captured and the close must match it.
_OPEN = re.compile(r"^(?P<ticks>`{3,})\s*mermaid\s*$")


def _display(path: Path) -> str:
    """Repository-relative where that means something — the failure line is then clickable — and
    the full path otherwise. A replay tree, or a file named explicitly elsewhere, is a legitimate
    target, and a path that cannot be made relative must not turn a message into a `ValueError`.
    """
    return (path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path).as_posix()


class ToolchainMissing(RuntimeError):
    """node, or the pinned checker's dependencies, are not installed here."""


@dataclass(frozen=True)
class Fence:
    """One diagram, and where a reader of the failure can find it."""

    file: str
    line: int  # 1-based, the line the fence opens on
    text: str

    def as_payload(self) -> dict[str, object]:
        return {"file": self.file, "line": self.line, "text": self.text}


def iter_fences(path: Path) -> list[Fence]:
    """Every mermaid fence in one Markdown file."""
    found: list[Fence] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    relative = _display(path)
    index = 0
    while index < len(lines):
        opening = _OPEN.match(lines[index])
        if not opening:
            index += 1
            continue
        close = re.compile(rf"^`{{{len(opening.group('ticks'))},}}\s*$")
        start = index
        index += 1
        body: list[str] = []
        while index < len(lines) and not close.match(lines[index]):
            body.append(lines[index])
            index += 1
        found.append(Fence(file=relative, line=start + 1, text="\n".join(body)))
        index += 1
    return found


def collect(targets: list[Path]) -> list[Fence]:
    """Every fence under the given files or directories, in a stable order."""
    files: list[Path] = []
    for target in targets:
        files.extend(sorted(target.rglob("*.md")) if target.is_dir() else [target])
    return [fence for file in files for fence in iter_fences(file)]


def parse_all(fences: list[Fence]) -> list[dict[str, object]]:
    """Ask mermaid to parse each fence; return the ones it refused.

    Raises :class:`ToolchainMissing` rather than returning "no failures" when the checker cannot
    run, so a caller cannot mistake an absent toolchain for a clean tree.
    """
    node = shutil.which("node")
    if node is None:
        raise ToolchainMissing("node is not installed")
    if not (TOOL_DIR / "node_modules" / "mermaid").is_dir():
        raise ToolchainMissing(f"run: npm ci --prefix {_display(TOOL_DIR)}")
    if not fences:
        return []
    completed = subprocess.run(
        [node, str(TOOL_DIR / "check_fences.mjs")],
        input=json.dumps([fence.as_payload() for fence in fences]),
        capture_output=True,
        text=True,
        cwd=TOOL_DIR,
        timeout=300,
    )
    if not completed.stdout.strip():
        raise ToolchainMissing(f"the checker produced no verdict: {completed.stderr.strip()[:400]}")
    return list(json.loads(completed.stdout)["failures"])


def main(argv: list[str]) -> int:
    targets = [Path(arg).resolve() for arg in argv] or [DOCS_DIR]
    fences = collect(targets)
    try:
        failures = parse_all(fences)
    except ToolchainMissing as missing:
        print(f"mermaid check DID NOT RUN: {missing}", file=sys.stderr)
        return 2
    for failure in failures:
        print(f"{failure['file']}:{failure['line']}: {failure['error']}", file=sys.stderr)
    if failures:
        print(
            f"\n{len(failures)} of {len(fences)} diagrams do not parse. The site renders these in "
            "the reader's browser, so each one is a red error box on the published page.",
            file=sys.stderr,
        )
        return 1
    print(f"{len(fences)} mermaid diagrams parse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
