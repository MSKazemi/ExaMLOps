"""Serve mermaid from this site, not from a CDN at the reader's expense.

mkdocs-material loads the diagram renderer itself, and its bundle asks unpkg for it:

    typeof mermaid == "undefined" ? _t("https://unpkg.com/mermaid@11/dist/mermaid.min.js") : …

Three things follow from that URL, and all three were measured in a headless browser against a
real build rather than assumed:

1. **A third party executes JavaScript on every page a reader opens.** The tag is a *floating
   major*, so the file can change under us at any time and Subresource Integrity is impossible by
   construction. Nothing in this repository pins what actually runs in the reader's browser.
2. **unpkg sees every reader** — their IP and the page they are on. This site is the public
   documentation of a European research project, which makes that a data-protection question and
   not only a supply-chain one.
3. **An outage or a breaking 11.x release silently breaks every diagram**, and the first to know
   is the reader, because a CDN fetch fails long after any build has passed.

The `typeof mermaid == "undefined"` test is also the supported way out: define it first and
Material never reaches for the network. So this hook adds mermaid to the build from the *same
pinned package* `platform/ci/check_mermaid.py` validates the diagrams with — one version, pinned
in one lockfile, Dependabot-watched — and `mkdocs.yml` loads it ahead of everything else.

Verified against the alternative: Material's `privacy` plugin downloads external assets at build
time and does catch this URL, but rewrites it to an **absolute** `site_url` address, so diagrams
render on the production origin and degrade to raw text everywhere else — a local `mkdocs serve`,
a preview deploy, the github.io domain. That is why this file exists instead of a line of config.
(For the site's *other* third-party assets — Google Fonts, and KaTeX from jsdelivr — the plugin
rewrites relatively and works; that is tracked separately.)

Nothing is written into `docs/`: the file is added to the build in memory, so the source tree
never carries a 3.5 MB generated blob.
"""

from __future__ import annotations

from pathlib import Path

from mkdocs.exceptions import PluginError
from mkdocs.structure.files import File

#: Where `npm ci --prefix platform/ci/mermaid` puts it, and the single pinned copy in this repo.
_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "platform"
    / "ci"
    / "mermaid"
    / "node_modules"
    / "mermaid"
    / "dist"
    / "mermaid.min.js"
)

#: Must match the first entry of `extra_javascript` in mkdocs.yml.
DEST_URI = "assets/js/mermaid.min.js"


def on_files(files, config):
    """Add the pinned mermaid to the build, or refuse to build at all.

    Refusing is the point. If this returned quietly when the package is missing, the site would
    still build, `extra_javascript` would 404, `window.mermaid` would be undefined again and
    Material would go back to unpkg — the exact thing this hook exists to prevent, restored
    silently, on the published site.
    """
    if not _SOURCE.is_file():
        raise PluginError(
            f"mermaid is not installed, so the docs would silently fall back to loading it from "
            f"unpkg.com in every reader's browser. Run:\n\n"
            f"    npm ci --prefix platform/ci/mermaid\n\n(expected: {_SOURCE})"
        )
    # `abs_src_path` rather than `content`: mkdocs then copies the 3.5 MB file straight through
    # instead of holding it in memory for the whole build.
    files.append(File.generated(config, DEST_URI, abs_src_path=str(_SOURCE)))
    return files
