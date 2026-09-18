"""A documentation page that is in no navigation is a page nobody finds.

`mkdocs build --strict` fails on a broken *link*; it says nothing about a page that no link
and no nav entry ever points at. Such a page still builds, still deploys, and is reachable
only by site search or by knowing its URL — which for a feature guide means the feature
looks undocumented. 37 pages were in that state when this guard was written, 17 of them
covering shipped features (`enterprise-installation`, `backup-restore`, `hpc-fleet`,
`rbac-multi-tenancy`, `synthetic-data`, the entire dashboard console set).

The reverse direction matters too: a nav entry pointing at a file that does not exist is a
dead menu item, and mkdocs only warns about it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MKDOCS = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs"

# mkdocs.yml carries python/tag YAML constructors that a plain safe_load rejects, and the nav
# is a flat-enough block that the paths can be read directly.
_PAGE = re.compile(r"([A-Za-z0-9_./-]+\.md)")


def _nav_pages() -> set[str]:
    text = MKDOCS.read_text()
    assert "\nnav:\n" in text, "mkdocs.yml has no nav: block"
    return set(_PAGE.findall(text.split("\nnav:\n", 1)[1]))


def _doc_pages() -> set[str]:
    return {str(p.relative_to(DOCS)) for p in DOCS.rglob("*.md")}


def test_there_are_pages_to_check():
    """Otherwise both assertions below pass by inspecting nothing."""
    pages = _doc_pages()
    assert len(pages) >= 50, f"only found {len(pages)} pages under {DOCS} — is the path right?"


def test_every_documentation_page_is_in_the_navigation():
    orphans = sorted(_doc_pages() - _nav_pages())
    assert not orphans, (
        f"{len(orphans)} page(s) are in no navigation — reachable only by search or a direct "
        "URL. Add each to the nav: block in mkdocs.yml under the section it belongs to:\n  "
        + "\n  ".join(orphans)
    )


def test_every_navigation_entry_points_at_a_page_that_exists():
    dangling = sorted(e for e in _nav_pages() if not (DOCS / e).exists())
    assert not dangling, (
        "mkdocs.yml nav names page(s) that do not exist under docs/ — a dead menu item:\n  "
        + "\n  ".join(dangling)
    )


# ── and a page that names a file must name one that exists ───────────────────


def test_every_repository_path_named_in_the_docs_exists():
    """A guide that points at `tests/unit/test_x.py` or `platform/…/y.py` must point at a real one.

    `mkdocs --strict` validates links between *pages*; a path to a **file in the repository** is
    just text to it. So a guide can keep naming the test that backs its claim long after the test
    was renamed, and the reader who goes looking finds nothing — which is worse than not naming it,
    because the citation is what made the claim credible.

    Only paths under directories the repository actually has are checked, and only where the page
    is pointing at a file rather than illustrating a shape: a path containing a wildcard, an
    ellipsis or a `<placeholder>` is prose.
    """
    all_roots = (
        "tests/",
        "platform/",
        "serving/",
        "pipelines/",
        "usecases/",
        "design/",
        ".github/",
    )
    roots = tuple(r for r in all_roots if (ROOT / r).is_dir())
    assert roots, (
        f"none of {all_roots} exist under {ROOT} — this guard would pass by scanning nothing"
    )
    pattern = re.compile(r"`((?:" + "|".join(re.escape(r) for r in roots) + r")[A-Za-z0-9_./-]+)`")
    broken: list[str] = []
    pages = sorted((ROOT / "docs").rglob("*.md"))
    assert pages, "no documentation pages found — this guard would pass by scanning nothing"
    for page in pages:
        for named in pattern.findall(page.read_text(encoding="utf-8")):
            if any(ch in named for ch in "*…<>") or named.endswith("/"):
                continue  # a shape, not a path
            if Path(named).name.startswith(".env"):
                continue  # an operator-created config file — gitignored in every checkout
            if not (ROOT / named).exists():
                broken.append(f"{page.relative_to(ROOT)} → {named}")
    assert not broken, "documentation names repository paths that do not exist:\n  " + "\n  ".join(
        broken
    )
