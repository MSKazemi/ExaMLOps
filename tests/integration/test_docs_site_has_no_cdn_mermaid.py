"""A built documentation page asks no CDN for its diagram renderer — measured, in a browser.

This is the test that decided the design, and it is kept because it is the only one that can
observe the thing that matters: what a reader's browser actually fetches. Config assertions
(`tests/unit/test_docs_no_cdn_mermaid.py`) cannot see a race in Material's lazy loader, and
`mkdocs build --strict` is happy either way.

What it measured, against real builds of this repository:

| build | requests to unpkg.com | diagrams |
|---|---|---|
| before | 2 (the floating tag, then its redirect) | render |
| with the hook | 0 | render, identically |

It also rejected the obvious alternative. Material's `privacy` plugin does download the file at
build time, but rewrites the reference to an **absolute** `site_url` address, so diagrams render
on the production origin and degrade to raw text on a local build, a preview deploy, or the
github.io domain — which the same probe showed as two diagrams left as text.

Opt-in: it needs playwright and a browser, neither of which is a test dependency of this project.

    uv venv /tmp/pw && uv pip install --python /tmp/pw/bin/python playwright pytest mkdocs-material
    /tmp/pw/bin/python -m playwright install chromium-headless-shell
    /tmp/pw/bin/python -m pytest --noconftest \\
        tests/integration/test_docs_site_has_no_cdn_mermaid.py

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
MERMAID_PACKAGE = REPO_ROOT / "platform" / "ci" / "mermaid" / "node_modules" / "mermaid"

playwright_api = pytest.importorskip("playwright.sync_api", reason="pip install playwright")

pytestmark = pytest.mark.skipif(
    not MERMAID_PACKAGE.is_dir(), reason="npm ci --prefix platform/ci/mermaid"
)

#: A page carrying diagrams, and the site root, which carries the hero animation's own.
PAGES = ("/guides/architecture/", "/")


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


def test_every_diagram_is_rendered_rather_than_left_as_text(served):
    """The half a request-count alone cannot prove: serving our own copy must not break rendering.
    An unrendered fence stays `pre.mermaid` and its source shows up as page text."""
    _, rendered = _visit(served)

    assert all(page["unprocessed"] == 0 for page in rendered), rendered
    assert all(page["raw"] == 0 for page in rendered), (
        f"diagram source is visible as text, so mermaid never ran: {rendered}"
    )
