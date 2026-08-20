"""The proof gate must prove the thing under test — not whatever is on ``PATH``.

``make check`` is what every change in this repo is measured by. Its recipes have to reach
**this** repo's virtualenv explicitly. A bare ``pip``/``pytest``/``ruff`` in a recipe binds
to whatever the caller's shell happens to expose, which fails in two directions:

* on a PEP-668 host the gate dies with ``externally-managed-environment`` for a reason that
  has nothing to do with the change under test, and
* on a host whose system Python is writable it *passes*, having installed a different
  dependency set and run a different interpreter than the rest of the gate used.

The second is the dangerous one: a green gate that measured something else.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).parents[2] / "Makefile"

# Tools that must never be invoked by bare name inside a recipe.
_PINNED = ("pip", "pytest", "ruff", "mypy", "alembic", "python", "python3")
# Ways of naming them that ARE pinned to this repo's toolchain.
_OK_PREFIX = re.compile(r"\$\((VENV_BIN|VENV|UV|PYTHON)\)")


def _recipes(text: str) -> dict[str, list[str]]:
    """``{target: [recipe lines]}`` — good enough for this Makefile's plain syntax."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("\t"):
            if current:
                out.setdefault(current, []).append(line)
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
        current = m.group(1) if m else None
    return out


def _check_prerequisites(text: str) -> list[str]:
    m = re.search(r"^check:([^\n#]*)", text, re.M)
    assert m, "no `check:` target in the Makefile"
    return m.group(1).split()


def _bare_invocations(lines: list[str]) -> list[str]:
    """Recipe lines that call a pinned tool by bare name."""
    bad = []
    for line in lines:
        # Drop the parts that are already pinned, plus comments, so they cannot false-positive.
        stripped = _OK_PREFIX.sub("PINNED", line).split("#", 1)[0]
        for tool in _PINNED:
            # A bare tool name: not preceded by `/`, `-`, or a word character.
            if re.search(rf"(?<![\w/@.-]){tool}\b", stripped):
                bad.append(f"{tool}: {line.strip()}")
    return bad


def test_check_target_exists_and_names_the_dashboard():
    prereqs = _check_prerequisites(MAKEFILE.read_text())
    assert "dashboard-check" in prereqs, (
        "`make check` no longer runs the dashboard half — every 'gate green' claim in this "
        "repo assumes it does"
    )


@pytest.mark.parametrize("target", _check_prerequisites(MAKEFILE.read_text()))
def test_gate_recipes_pin_their_toolchain(target: str):
    text = MAKEFILE.read_text()
    recipes = _recipes(text)
    lines = recipes.get(target, [])
    if not lines:
        pytest.skip(f"`{target}` has no recipe of its own (delegates to prerequisites)")
    bad = _bare_invocations(lines)
    assert not bad, (
        f"`{target}` invokes a tool by bare name, so the gate measures whatever is on PATH "
        f"rather than this repo's venv. Use $(VENV_BIN)/<tool>:\n  " + "\n  ".join(bad)
    )


def test_dashboard_check_refuses_to_pass_when_it_cannot_run_both_halves():
    """Missing venv or missing npm must FAIL the gate, never quietly skip a half."""
    lines = "\n".join(_recipes(MAKEFILE.read_text()).get("dashboard-check", []))
    assert "exit 1" in lines, "dashboard-check has no failure path — a missing tool would pass"
    assert "command -v npm" in lines, (
        "dashboard-check does not check for npm; without it the frontend half is skipped "
        "silently and the gate still reports success"
    )


def test_lint_checks_formatting_not_just_rules():
    """`ruff format --check` is a HARD failure in CI; the local gate must run it too.

    `make lint` used to run only `ruff check`, so `make check` could report green on a tree
    CI rejects on formatting alone — the v0.26.1 → v0.27.1 red-pipeline saga, which recurred
    on 2026-08-20 when two test files landed unformatted with every local gate green.
    """
    lines = "\n".join(_recipes(MAKEFILE.read_text()).get("lint", []))
    assert "ruff format --check" in lines, (
        "`make lint` does not run `ruff format --check`, so `make check` cannot catch a "
        "formatting failure that CI treats as fatal"
    )
