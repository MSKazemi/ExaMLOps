"""A policy condition that cannot be evaluated does not match, so the policy layer fails OPEN when
its evaluator is missing: `deny ... when: "model == 'JPCP'"` silently allowed everything in any
process installed without the optional `finops` extra (the control-plane and dashboard images and
the control-plane CI job were, which is how three control-plane tests went red on main).

The evaluator is therefore a hard dependency. This pins that in the package metadata, where an
`import`-based test cannot see it — the developer venv always has the package.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "platform" / "cli" / "pyproject.toml"


def _names(requirements: list[str]) -> set[str]:
    return {re.split(r"[<>=!~\[; ]", r, maxsplit=1)[0].lower() for r in requirements}


def test_the_condition_evaluator_is_a_base_dependency():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert "simpleeval" in _names(project["dependencies"]), (
        "examlops.policy evaluates `when:` with simpleeval and fails open without it — it must be "
        "in [project].dependencies, not only an extra"
    )


def test_the_finops_extra_still_resolves():
    """Existing `examlops[finops]` installs must keep working."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert "simpleeval" in _names(project["optional-dependencies"]["finops"])
