"""Every published page loads without a hole in it — all of them, in a browser.

`mkdocs build --strict` checks the links *between* pages. Nothing checked what a page then asks the
browser for: an image that moved, a stylesheet a theme upgrade renamed, a script that throws, a
diagram the renderer refused, a formula left as TeX source. Each is invisible to the build and
plainly visible to a reader, and this repository has already been bitten once — two invalid mermaid
diagrams were published for months before anyone opened the page.

So this loads all 145 of them and reports, per page: any response of 400 or worse, any console
error, any `pre.mermaid` the renderer did not consume, any visible TeX source, and any empty
`href`.

**The first run found nothing, which is a claim about the detector until it is tested**, so every
check was first run against a build deliberately broken in the way it claims to catch:

| build | reported |
|---|---|
| KaTeX stylesheet served without its fonts (a real configuration this repo nearly shipped) | both `algorithms/` pages, 404s on `KaTeX_*.woff2` + console errors |
| an unparseable diagram injected into one page | that page, `unrendered_diagrams: 1` |
| a missing image injected into the home page | that page, `404 /assets/nope.png` |
| KaTeX's scripts removed | `raw_math: 4` and `raw_math: 8` on the two `algorithms/` pages |
| an `<a href="">` injected into one page | that page, `empty_links: 1` |

Five checks, five demonstrations. The two that were hardest to arrange are the two worth having:
`raw_math` fires only when the maths is visible as TeX, and a build with the *fonts* missing does
not trip it — the formulae are still typeset, just wrongly, which is why that failure needed the
separate FontFaceSet assertion in `test_docs_site_is_self_contained.py`.

Opt-in, and slow by the standards of the unit suite (a browser, and every page in the site). It is
not a gate you run on every change; it is the one that answers "is the published site whole".

    uv venv /tmp/pw && uv pip install --python /tmp/pw/bin/python playwright pytest mkdocs-material
    /tmp/pw/bin/python -m playwright install chromium-headless-shell
    /tmp/pw/bin/python -m pytest --noconftest tests/integration/test_docs_site_pages_are_whole.py
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

#: Enough of a page to know the renderers have run, without waiting on the hero animation's loop.
SETTLE_MS = 1200


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("site")
    build = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert build.returncode == 0, build.stderr[-2000:]
    return out


@pytest.fixture(scope="module")
def served(site):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def findings(site, served) -> dict[str, dict]:
    """Crawl every page once; each test below reads one kind of finding out of the result."""
    pages = sorted(p.parent.relative_to(site).as_posix() for p in site.rglob("index.html"))
    assert len(pages) > 100, f"only {len(pages)} pages — did the build produce a site at all?"

    seen: dict[str, dict] = {}
    current: dict[str, list] = {"http": [], "console": []}
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on(
            "response",
            lambda r: (
                current["http"].append(f"{r.status} {r.url.split(served)[-1]}")
                if r.status >= 400
                else None
            ),
        )
        page.on(
            "console",
            lambda m: current["console"].append(m.text[:120]) if m.type == "error" else None,
        )
        for relative in pages:
            current["http"], current["console"] = [], []
            page.goto(f"{served}/" + ("" if relative == "." else relative + "/"), wait_until="load")
            page.wait_for_timeout(SETTLE_MS)
            state = page.evaluate(
                "() => ({"
                " diagrams: document.querySelectorAll('pre.mermaid').length,"
                r" rawMath: (document.body.innerText.match(/\\frac|\\sum|\\cdot|\\mathrm/g)||[]).length,"
                " emptyLinks: document.querySelectorAll('a[href=\"\"]').length })"
            )
            found: dict = {}
            if current["http"]:
                found["http"] = sorted(set(current["http"]))[:5]
            if current["console"]:
                found["console"] = sorted(set(current["console"]))[:3]
            if state["diagrams"]:
                found["unrendered_diagrams"] = state["diagrams"]
            if state["rawMath"]:
                found["raw_math"] = state["rawMath"]
            if state["emptyLinks"]:
                found["empty_links"] = state["emptyLinks"]
            if found:
                seen["/" + ("" if relative == "." else relative)] = found
        browser.close()
    return seen


def _report(findings: dict[str, dict], key: str) -> str:
    return "\n  ".join(f"{page}: {found[key]}" for page, found in findings.items() if key in found)


def test_no_page_asks_for_something_that_is_not_there(findings):
    """A moved image or a renamed theme asset. `--strict` never sees these: they are referenced
    from HTML, CSS and JavaScript, not from the Markdown link graph it walks."""
    assert not _report(findings, "http"), "assets a page asked for and did not get:\n  " + _report(
        findings, "http"
    )


def test_no_page_logs_an_error(findings):
    assert not _report(findings, "console"), "JavaScript errors on published pages:\n  " + _report(
        findings, "console"
    )


def test_every_diagram_was_rendered(findings):
    """A fence the renderer refused stays `pre.mermaid`. `platform/ci/check_mermaid.py` catches an
    invalid diagram at the source; this catches one the *browser* could not draw for any other
    reason — the renderer missing, a version that dropped a syntax, a CSP."""
    assert not _report(findings, "unrendered_diagrams"), "diagrams left unrendered:\n  " + _report(
        findings, "unrendered_diagrams"
    )


def test_no_page_shows_its_maths_as_source(findings):
    """`\\frac` on the page means arithmatex or KaTeX did not run, and the reader sees TeX."""
    assert not _report(findings, "raw_math"), "TeX source visible to readers:\n  " + _report(
        findings, "raw_math"
    )


def test_no_page_carries_an_empty_link(findings):
    assert not _report(findings, "empty_links"), "links that go nowhere:\n  " + _report(
        findings, "empty_links"
    )
