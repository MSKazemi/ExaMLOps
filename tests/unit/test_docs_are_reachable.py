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
