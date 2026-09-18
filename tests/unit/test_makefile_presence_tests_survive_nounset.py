"""A presence test that dies when the thing is absent has tested nothing.

This Makefile runs its recipes under `SHELL := /bin/bash -euo pipefail`. The `-u` means reading an
unset variable **aborts the recipe**, so a line like::

    @[ -n "$$EXAMLOPS_IAM_LIVE_KEYCLOAK_URL" ] || { printf "Set it first..."; exit 1; }

never reaches its own message: bash exits with `unbound variable` at the test itself. The operator
gets a shell error instead of the sentence explaining which variables to set and where to get them.

Both `iam-live` and `lineage-live` shipped with exactly that on 2026-09-15 and were found by
*running* them — a dry run expands the recipe and proves nothing about what bash does with it.
The fix is `$${VAR:-}`, which is defined when the variable is not.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: `[ -n "$VAR" ]` or `[ -z "$VAR" ]` on a bare dereference — no `${VAR:-}` default.
PRESENCE_TEST = re.compile(r'\[\s*-[nz]\s+"\$\$(?!\{)([A-Za-z_][A-Za-z0-9_]*)"')


def _offenders(text: str) -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.startswith("\t"):  # only recipe lines run under the project's SHELL
            continue
        found.extend((number, m.group(1)) for m in PRESENCE_TEST.finditer(line))
    return found


def test_the_makefile_still_runs_recipes_under_nounset():
    """If `-u` is ever dropped this guard is pointless, and its docstring becomes wrong.

    The flags arrive combined (`-euo pipefail`), so `"-u" in line` is False for a shell that very
    much does set nounset — read the cluster, not the rendered string.
    """
    shell = [ln for ln in (ROOT / "Makefile").read_text().splitlines() if ln.startswith("SHELL")]
    assert shell, "no SHELL line in the Makefile"
    clusters = re.findall(r"(?<![\w-])-([a-z]+)", shell[0])
    assert any("u" in cluster for cluster in clusters), shell[0]


def test_no_presence_test_aborts_before_it_can_decide():
    offenders = _offenders((ROOT / "Makefile").read_text(encoding="utf-8"))
    assert not offenders, (
        "these recipe lines test whether a variable is set, but read it bare — under `bash -u` "
        "that aborts with `unbound variable` before the branch runs, so the helpful message never "
        f"prints. Use $${{VAR:-}}: {offenders}"
    )


def test_the_pattern_catches_the_mistake_it_was_written_for():
    """A detector that matched nothing would let the next one through silently."""
    assert _offenders('\t@[ -n "$$SOME_VAR" ] || { echo nope; exit 1; }') == [(1, "SOME_VAR")]
    assert _offenders('\t@[ -n "$${SOME_VAR:-}" ] || { echo nope; exit 1; }') == []
