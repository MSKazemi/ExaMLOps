"""No third party is contacted from a reader's browser, and each exception says why.

Measured in a headless browser against a real build: every documentation page fetched the site's
typeface from `fonts.googleapis.com` and `fonts.gstatic.com`, carrying the reader's IP address and
the page they were reading to a third party. Material's `privacy` plugin downloads those assets at
build time and serves them from this site instead; after enabling it, no page requests either host.

The plugin is not a blanket answer, which is why its exclusions are guarded rather than merely
written down. It rewrites references it finds in HTML and CSS, so:

* a URL built inside a JavaScript bundle is rewritten **absolutely** against `site_url` — which is
  why mermaid is served by `docs/overrides/mermaid_hook.py` and excluded here; and
* a stylesheet that names its own assets with **relative** urls is localised without them — KaTeX's
  `fonts/KaTeX_*.woff2` all 404'd in that configuration and the maths lost its typeface.

Both exclusions are therefore deliberate, and both are temporary in different ways: the first ends
if Material stops bundling that URL, the second when the package is served whole (BL-078). A guard
that only checked "the plugin is on" would let either silently become a lie.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MKDOCS = REPO_ROOT / "mkdocs.yml"

yaml = pytest.importorskip("yaml")


def _config() -> dict:
    text = re.sub(r"!!python/\S+", "", MKDOCS.read_text(encoding="utf-8"))
    return yaml.safe_load(text)


def _privacy() -> dict:
    for plugin in _config().get("plugins") or []:
        if plugin == "privacy":
            return {}
        if isinstance(plugin, dict) and "privacy" in plugin:
            return plugin["privacy"] or {}
    pytest.fail("the privacy plugin is not enabled — external assets reach the reader's browser")


def _plugin_names() -> list[str]:
    return [
        plugin if isinstance(plugin, str) else next(iter(plugin))
        for plugin in _config().get("plugins") or []
    ]


def test_the_privacy_plugin_is_enabled():
    assert "privacy" in _plugin_names(), (
        "without it every page fetches the typeface from Google, handing a third party the "
        "reader's IP address and the page they are reading"
    )


#: A YAML list entry naming a third party — an exclusion from the privacy plugin, or an asset
#: still loaded from a CDN. Both are holes in "no reader's browser talks to anyone else".
_THIRD_PARTY = re.compile(r"^\s+- (unpkg|cdn\.|fonts\.|https?:)")


def test_every_third_party_url_is_explained_in_the_file():
    """A hole that outlives its reason is just a hole, and the reason belongs beside the line
    rather than in a commit message nobody reads again.

    The comment introduces a *run* of URLs, not every line of one: three KaTeX files share one
    explanation because they are one decision, and demanding a comment per line would only teach
    the next person to paste the same sentence three times.

    It also asks the comment to *say* something. A mutation that emptied the explanation to a bare
    `#` passed an earlier version of this test — which would have made the guard satisfiable by
    the one edit it exists to prevent.
    """
    lines = MKDOCS.read_text(encoding="utf-8").splitlines()
    unexplained = []
    for index, line in enumerate(lines):
        if not _THIRD_PARTY.match(line):
            continue
        previous = next((ln for ln in reversed(lines[:index]) if ln.strip()), "")
        if _THIRD_PARTY.match(previous):
            continue  # a continuation of a run whose first line was checked
        # The whole contiguous comment block above, so a reason may span several lines.
        block, cursor = [], index - 1
        while cursor >= 0 and (not lines[cursor].strip() or lines[cursor].strip().startswith("#")):
            if lines[cursor].strip().startswith("#"):
                block.append(lines[cursor].strip().lstrip("#").strip())
            cursor -= 1
        if len(" ".join(block)) >= 20:
            continue
        unexplained.append(f"mkdocs.yml:{index + 1}: {line.strip()}")
    assert not unexplained, "third-party URLs with no reason beside them:\n  " + "\n  ".join(
        unexplained
    )


def test_mermaid_is_excluded_because_the_site_serves_its_own():
    """Localising it would ship a second, unused copy of a 3.5 MB file — and an absolute URL."""
    assert any("unpkg.com/mermaid" in pattern for pattern in _privacy().get("assets_exclude", []))


def test_katex_is_excluded_until_its_fonts_travel_with_it():
    """The plugin localises the stylesheet but not the `fonts/` it names relatively. Removing this
    line without serving the whole package puts the maths back in a fallback face."""
    assert any("katex" in pattern for pattern in _privacy().get("assets_exclude", []))


def test_no_external_asset_is_referenced_that_is_not_accounted_for():
    """The plugin covers what the *pages* reference; these two lists are configuration, and a new
    CDN URL added here would be localised silently — except for the excluded ones, which would
    not. So every external URL left in them must be one the exclusions name on purpose."""
    config = _config()
    excluded = " ".join(_privacy().get("assets_exclude", []))
    external = [
        url
        for key in ("extra_css", "extra_javascript")
        for url in (config.get(key) or [])
        if isinstance(url, str) and url.startswith(("http://", "https://"))
    ]

    unaccounted = [
        url for url in external if not any(part in url for part in ("katex",) if part in excluded)
    ]

    assert not unaccounted, (
        "external URLs in mkdocs.yml that the privacy plugin will localise or that nothing "
        f"explains: {unaccounted}"
    )
