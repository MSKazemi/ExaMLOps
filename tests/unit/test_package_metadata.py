"""The `examlops` distribution's public face: what PyPI shows and what the wheel declares (ADR 0129).

A PyPI project page is written once per release and cannot be edited afterwards, so the
mistakes worth catching are the ones that would be frozen there: a README link PyPI cannot
resolve, a link into the docs site that 404s, a licence file that silently diverged from the
repository's, or classifiers that promise a Python the package refuses to install on.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "platform" / "cli"
PYPROJECT = tomllib.loads((PKG / "pyproject.toml").read_text())
PROJECT = PYPROJECT["project"]
README = (PKG / PROJECT["readme"]).read_text()
# The documentation site's canonical URL — one source, so the PyPI page cannot drift from it.
DOCS_SITE = re.search(r"^site_url:\s*(\S+)", (ROOT / "mkdocs.yml").read_text(), re.M).group(1)


def test_licence_copy_is_the_repository_licence():
    """PEP 639 needs the file inside the project dir; it must be the same file, byte for byte."""
    assert (PKG / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert PROJECT["license"] == "Apache-2.0"
    assert "Apache License" in (ROOT / "LICENSE").read_text()[:200]


def test_no_license_classifier_alongside_the_spdx_expression():
    """PEP 639 deprecates `License ::` classifiers; setuptools rejects the pair."""
    assert not [c for c in PROJECT["classifiers"] if c.startswith("License ::")]


def test_readme_has_no_relative_links():
    """PyPI renders the README without the repository around it: a relative link is a 404."""
    targets = re.findall(r"\]\(([^)]+)\)", README)
    relative = [t for t in targets if not re.match(r"(https?://|mailto:|#)", t)]
    assert not relative, f"relative links break on PyPI: {relative}"


def test_readme_links_into_the_docs_site_resolve_to_real_pages():
    """A docs link on a frozen PyPI page must point at a page the site actually builds."""
    links = set(re.findall(re.escape(DOCS_SITE) + r"([^)>\s]*)", README))
    assert links, "the README should link into the documentation site"
    for path in links:
        path = path.strip("/")
        if not path:
            continue
        candidates = [ROOT / "docs" / f"{path}.md", ROOT / "docs" / path / "index.md"]
        assert any(c.exists() for c in candidates), f"{DOCS_SITE}{path}/ has no docs page"


def test_python_classifiers_match_requires_python():
    floor = re.match(r">=(\d+)\.(\d+)", PROJECT["requires-python"])
    assert floor, PROJECT["requires-python"]
    minimum = (int(floor[1]), int(floor[2]))
    versions = [
        tuple(int(x) for x in c.rsplit(":: ", 1)[1].split("."))
        for c in PROJECT["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
    ]
    assert versions and min(versions) == minimum, (versions, minimum)


def test_project_urls_point_at_the_canonical_namespace():
    urls = PROJECT["urls"]
    assert urls["Homepage"] == urls["Documentation"] == DOCS_SITE, "docs URL differs from site_url"
    for key in ("Homepage", "Repository", "Issues", "Changelog"):
        assert key in urls, f"missing project URL {key}"
    for url in urls.values():
        assert url.startswith(("https://github.com/MSKazemi/", DOCS_SITE)), url


def test_the_console_script_is_exa():
    assert PROJECT["scripts"] == {"exa": "examlops.cli.main:app"}


def test_every_in_package_data_file_is_declared():
    """A non-Python file under src/ that package-data does not list is missing from the wheel."""
    declared = PYPROJECT["tool"]["setuptools"]["package-data"]["examlops"]
    src = PKG / "src" / "examlops"
    undeclared = []
    for f in src.rglob("*"):
        if f.is_dir() or f.suffix in {".py", ".pyc"} or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(src).as_posix()
        if not any(Path(rel).match(pattern) for pattern in declared):
            undeclared.append(rel)
    assert not undeclared, f"not shipped in the wheel (add to package-data): {undeclared}"


def test_readme_lists_every_extra():
    """The PyPI page is how a user learns an extra exists; a new extra must reach it."""
    documented = set(re.findall(r"`examlops\[([a-z0-9-]+)\]`", README))
    declared = set(PROJECT["optional-dependencies"])
    assert declared <= documented, (
        f"extras missing from platform/cli/README.md: {declared - documented}"
    )
    assert documented <= declared, f"README names extras that do not exist: {documented - declared}"
