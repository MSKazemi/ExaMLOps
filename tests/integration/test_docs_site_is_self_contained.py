"""A built documentation page contacts no third party — measured, in a browser.

These are the tests that decided the design, and they are kept because they observe the only thing
that matters: what a reader's browser actually fetches. Config assertions
(`tests/unit/test_docs_no_cdn_mermaid.py`, `tests/unit/test_docs_privacy_plugin.py`) cannot see a
race in Material's lazy loader or a stylesheet whose own fonts never arrived, and
`mkdocs build --strict` is happy either way.

What they measured, against real builds of this repository:

| build | third-party requests per page | rendering |
|---|---|---|
| before | 2 to unpkg.com (mermaid), 6 to Google Fonts, 3 to jsdelivr (KaTeX) | fine |
| now | none but `api.github.com` (Material's repo widget) | identical |

They also rejected two plausible-looking answers. Material's `privacy` plugin does download
mermaid, but rewrites that reference **absolutely** against `site_url` — because the URL is built
inside a JavaScript bundle — so diagrams render on the production origin and degrade to raw text on
a local build, a preview deploy or the github.io domain; the probe showed two diagrams as text. And
letting it localise KaTeX left every `fonts/KaTeX_*.woff2` 404ing, because that stylesheet names
them with relative urls the plugin does not follow: the maths still appeared, in the wrong face,
which no request count would have revealed. KaTeX is therefore served whole, with its directory
layout intact so those relative urls resolve.

That last failure is also why `test_the_maths_has_its_own_typeface` asks the **FontFaceSet** rather
than computed style. `getComputedStyle(...).fontFamily` returns the family the CSS *declares*, which
reads `KaTeX_Math` whether or not the file behind it ever loaded — it was identical in the working
and the broken build, and would have been an assertion that cannot fail. `document.fonts.check()`
answers the real question and separates them: true with 5 KaTeX faces loaded, false with 0.

Opt-in: it needs playwright and a browser, neither of which is a test dependency of this project.

    uv venv /tmp/pw && uv pip install --python /tmp/pw/bin/python playwright pytest mkdocs-material
    /tmp/pw/bin/python -m playwright install chromium-headless-shell
    /tmp/pw/bin/python -m pytest --noconftest \\
        tests/integration/test_docs_site_is_self_contained.py

`--noconftest` because a throwaway environment holds a browser, not this project: `tests/conftest.py`
imports the platform itself. In the project's own environment the file is collected normally and
skips, since playwright is deliberately not a dependency here.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    REPO_ROOT / "platform" / "ci" / "mermaid" / "node_modules" / "mermaid",
    REPO_ROOT / "platform" / "ci" / "katex" / "node_modules" / "katex",
)

playwright_api = pytest.importorskip("playwright.sync_api", reason="pip install playwright")

pytestmark = pytest.mark.skipif(
    not all(package.is_dir() for package in PACKAGES),
    reason="npm ci --prefix platform/ci/mermaid && npm ci --prefix platform/ci/katex",
)

#: A page carrying diagrams, the site root (the hero animation's own), and a page of mathematics.
PAGES = ("/guides/architecture/", "/", "/algorithms/carbon-aware-placement/")


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("site")
    build = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, build.stderr[-2000:]
    return out


@pytest.fixture(scope="module")
def served(site):
    """The built site on a loopback port. A `file://` origin would not exercise the same fetches."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _visit(served: str) -> tuple[list[str], list[dict]]:
    """Load each page, returning every off-origin request and what rendered."""
    external: list[str] = []
    rendered: list[dict] = []
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on(
            "request",
            lambda request: external.append(request.url) if served not in request.url else None,
        )
        for path in PAGES:
            page.goto(f"{served}{path}", wait_until="load")
            page.wait_for_timeout(4000)
            rendered.append(
                page.evaluate(
                    "() => ({unprocessed: document.querySelectorAll('pre.mermaid').length,"
                    " raw: (document.body.innerText.match(/sequenceDiagram|flowchart /g)||[]).length})"
                )
            )
        browser.close()
    return external, rendered


def test_no_page_fetches_mermaid_from_a_cdn(served):
    external, _ = _visit(served)

    reached = sorted({url.split("/")[2] for url in external if "mermaid" in url})

    assert not reached, f"the diagram renderer was fetched from {reached}"


def test_no_page_asks_google_for_the_typeface(served):
    """Every page used to make six of these, carrying the reader's IP and the page they were on to
    a third party. For the public documentation of a European research project that is a
    data-protection question before it is a supply-chain one."""
    external, _ = _visit(served)

    google = sorted({url.split("/")[2] for url in external if "fonts.g" in url})

    assert not google, f"the typeface was fetched from {google}"


def test_the_only_third_party_left_is_the_one_the_config_explains(served):
    """A drifting list of "known exceptions" is how a privacy guarantee rots. The exceptions are
    named here so that adding one means editing a test that says why:

    * `api.github.com` — Material's own repository widget, a deliberate keep; and
    * `cdn.jsdelivr.net` — KaTeX, which cannot be localised until its fonts travel with it
      (BL-078), and which `mkdocs.yml` explains beside the URL.
    """
    external, _ = _visit(served)

    hosts = sorted({url.split("/")[2] for url in external})

    assert set(hosts) <= {"api.github.com"}, hosts


def test_the_maths_has_its_own_typeface(served):
    """KaTeX's stylesheet names ~60 font files with relative urls. Serving the stylesheet without
    them leaves the formulae on the page in a fallback face — visible to a reader, invisible to a
    request count, and the exact state a previous configuration shipped into a build.

    Asked of the FontFaceSet, because computed style reports the declared family either way.
    """
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        failed: list[str] = []
        page.on("requestfailed", lambda request: failed.append(request.url))
        page.on(
            "response",
            lambda response: failed.append(response.url) if response.status >= 400 else None,
        )
        page.goto(f"{served}/algorithms/carbon-aware-placement/", wait_until="load")
        page.wait_for_timeout(4000)
        fonts = page.evaluate(
            "async () => { await document.fonts.ready; return {"
            " usable: document.fonts.check('16px KaTeX_Math'),"
            " loaded: [...document.fonts].filter(f => f.status === 'loaded')"
            "          .filter(f => f.family.startsWith('KaTeX')).length,"
            " formulae: document.querySelectorAll('.katex').length } }"
        )
        browser.close()

    assert fonts["formulae"] > 0, "the page under test has no mathematics on it any more"
    assert fonts["usable"] and fonts["loaded"] > 0, f"the maths is in a fallback face: {fonts}"
    assert not failed, f"assets the page asked for and did not get: {sorted(set(failed))[:5]}"


def test_every_diagram_is_rendered_rather_than_left_as_text(served):
    """The half a request-count alone cannot prove: serving our own copy must not break rendering.
    An unrendered fence stays `pre.mermaid` and its source shows up as page text."""
    _, rendered = _visit(served)

    assert all(page["unprocessed"] == 0 for page in rendered), rendered
    assert all(page["raw"] == 0 for page in rendered), (
        f"diagram source is visible as text, so mermaid never ran: {rendered}"
    )
