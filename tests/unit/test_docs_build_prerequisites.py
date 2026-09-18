"""Every place that builds the documentation installs what the build now requires.

`docs/overrides/docs_assets_hook.py` makes the build *depend* on two pinned npm packages: the site
serves mermaid and KaTeX itself, and a missing package stops the build rather than letting the
published page fetch them from a CDN in the reader's browser. That is the intended behaviour — and
it turns "which jobs build the docs?" from a triviality into a prerequisite that has to hold
everywhere.

It did not. The change was made to `.github/workflows/ci.yml` and `.gitlab-ci.yml`, the two
pipelines whose docs steps were being edited at the time — and missed `.github/workflows/pages.yml`,
which is a *separate workflow* that builds the same site and publishes it. CI's own docs job stayed
green, so nothing pointed at the job that had stopped deploying the site.

The lesson is general enough to guard rather than remember: when a build step gains a prerequisite,
the question is not "which callers do I know about" but "which callers are there". This asks the
tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where a documentation build can be invoked from. `Makefile` is covered by its own target
#: (`docs-build` depends on `docs-assets-deps`), and is checked here too so the three agree.
SEARCH = (
    REPO_ROOT / ".github" / "workflows",
    REPO_ROOT / ".gitlab-ci.yml",
    REPO_ROOT / "Makefile",
)

#: The packages the hook requires, by the directory `npm ci --prefix` must name.
REQUIRED = ("platform/ci/mermaid", "platform/ci/katex")

#: What actually installs one — and what the check looks for. Matching the *path* instead let a
#: `cache-dependency-path:` entry satisfy the guard while `npm ci` still ran after the build: the
#: package was named early and installed late, which is the failure with extra steps.
INSTALL_COMMAND = "npm ci --prefix {package}"

_BUILDS_DOCS = re.compile(r"mkdocs build")

#: The Make target that installs them; the Makefile names it rather than the packages.
INSTALL_TARGET = "docs-assets-deps"


def _files() -> list[Path]:
    found: list[Path] = []
    for target in SEARCH:
        found.extend(sorted(target.rglob("*.yml")) if target.is_dir() else [target])
    return [path for path in found if path.is_file()]


def _builders() -> list[Path]:
    return [path for path in _files() if _BUILDS_DOCS.search(path.read_text(encoding="utf-8"))]


def test_the_search_finds_something_to_check():
    """A guard that scans nothing passes. Three places build the docs today; if this drops to
    zero the pattern has stopped matching, not the problem gone away."""
    builders = _builders()

    assert len(builders) >= 3, f"only found {[p.name for p in builders]}"


def _installs_before_build(path: Path) -> list[str]:
    """Which required packages this file fails to install before it builds.

    Order is *execution* order, not the order the characters happen to appear in: a Makefile names
    its prerequisite by target, whose recipe is written further down the file, and a pipeline runs
    the steps of one job in sequence. Comparing byte offsets called the Makefile broken and would
    have taught the next reader to ignore this test.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".yml":
        # Commands only. Both halves of this matter and both were learned the hard way: the first
        # `mkdocs build` in pages.yml is in a file-header *comment* twenty lines above any step,
        # and a comment naming a package would otherwise satisfy the install check.
        steps = [line for line in text.splitlines() if not line.strip().startswith("#")]
        build_line = next(i for i, line in enumerate(steps) if _BUILDS_DOCS.search(line))
        before = "\n".join(steps[:build_line])
        return [
            package for package in REQUIRED if INSTALL_COMMAND.format(package=package) not in before
        ]

    # Make: the recipe that builds must invoke the install target before its build line, and that
    # target must handle both tools. It names them as loop values (`for tool in mermaid katex`)
    # rather than as paths, so the tool name is what to look for — checking for the full path
    # reported the Makefile as broken when it was correct, which is how a guard loses its reader.
    recipe = text.split("docs-build:", 1)[-1].split("\n\n", 1)[0].splitlines()
    build_line = next(i for i, line in enumerate(recipe) if _BUILDS_DOCS.search(line))
    if INSTALL_TARGET not in "\n".join(recipe[:build_line]):
        return list(REQUIRED)
    target = text.split(f"{INSTALL_TARGET}:", 1)[-1].split("\n\n", 1)[0]
    return [package for package in REQUIRED if Path(package).name not in target]


@pytest.mark.parametrize("path", _builders(), ids=lambda p: p.name)
def test_everything_that_builds_the_docs_installs_the_pinned_assets_first(path):
    """Installing them *after* the build is the same failure as not installing them: the hook has
    already stopped the build. So the order is asserted, not just the presence."""
    missing = _installs_before_build(path)

    assert not missing, (
        f"{path.name} builds the documentation without installing {missing} first. The build now "
        f"requires those packages and aborts without them — which is how the Pages deploy broke "
        f"while CI's own docs job stayed green."
    )
