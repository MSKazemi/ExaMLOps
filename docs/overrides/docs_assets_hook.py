"""Serve the site's front-end assets from the site, not from a CDN at the reader's expense.

Two assets used to be fetched from third parties while a reader had the page open. Both are now
pinned in this repository and added to the build from here, and the rule is the same for each: if
the package is missing the build **stops**, because the alternative is a site that quietly goes back
to the CDN and nobody finds out.

**mermaid** — mkdocs-material's bundle asks unpkg for the diagram renderer:

    typeof mermaid == "undefined" ? _t("https://unpkg.com/mermaid@11/dist/mermaid.min.js") : …

a *floating major*, so the file could change under us at any time and Subresource Integrity is
impossible by construction; unpkg saw every reader's IP and page; and an outage or a breaking 11.x
release would have broken every diagram, with the reader the first to know. The `typeof` test is
also the supported way out: define it first and Material never reaches for the network. The copy
served is the one `platform/ci/check_mermaid.py` validates the diagrams with, so what a reader runs
and what CI parsed cannot drift apart.

**KaTeX** — the maths renderer, previously `cdn.jsdelivr.net/npm/katex@…`. Material's `privacy`
plugin cannot localise it: `katex.min.css` names its ~60 font files with **relative** urls, which
the plugin does not follow, so localising the stylesheet alone left every `fonts/KaTeX_*.woff2`
404ing and the maths rendering in a fallback face — formulae still on the page, which is exactly why
a check that counted requests called that configuration a success. Serving the package with its
directory layout intact is what makes those relative urls resolve.

Verified in a headless browser against real builds, not reasoned about: see
`tests/integration/test_docs_site_is_self_contained.py`. Material's `privacy` plugin is the right
tool for assets referenced from HTML and CSS (Google Fonts) and is enabled for them; it is the wrong
tool for a URL built inside a JavaScript bundle, which it rewrites to an **absolute** `site_url`
address — diagrams then render on the production origin and degrade to raw text on a local build, a
preview deploy or the github.io domain.

Nothing is written into `docs/`: files are added to the build in memory, so the source tree never
carries a generated blob.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mkdocs.exceptions import PluginError
from mkdocs.structure.files import File

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Asset:
    """One pinned package, and where its files are served from."""

    name: str
    package: Path  # the installed package directory, from `npm ci --prefix <tool_dir>`
    install: str  # the command that produces it, quoted back at whoever hits the failure
    #: (path relative to `package`, destination URI). A directory copies recursively, keeping its
    #: layout — KaTeX's stylesheet points at `fonts/…` relative to itself, so the layout *is* the
    #: contract.
    files: tuple[tuple[str, str], ...]


def _tool(*parts: str) -> Path:
    return REPO_ROOT.joinpath("platform", "ci", *parts)


ASSETS = (
    Asset(
        name="mermaid",
        package=_tool("mermaid", "node_modules", "mermaid"),
        install="npm ci --prefix platform/ci/mermaid",
        files=(("dist/mermaid.min.js", "assets/js/mermaid.min.js"),),
    ),
    Asset(
        name="katex",
        package=_tool("katex", "node_modules", "katex"),
        install="npm ci --prefix platform/ci/katex",
        files=(
            ("dist/katex.min.css", "assets/katex/katex.min.css"),
            ("dist/katex.min.js", "assets/katex/katex.min.js"),
            ("dist/contrib/auto-render.min.js", "assets/katex/contrib/auto-render.min.js"),
            # Every format the stylesheet names, not only woff2: a face it cannot find is a
            # silent fallback, and 1.2 MB is a cheaper answer than deciding which browsers count.
            ("dist/fonts", "assets/katex/fonts"),
        ),
    ),
)


def _pairs(asset: Asset) -> list[tuple[Path, str]]:
    """Every (source file, destination URI) this asset contributes."""
    resolved: list[tuple[Path, str]] = []
    for relative, destination in asset.files:
        source = asset.package / relative
        if source.is_dir():
            resolved.extend(
                (child, f"{destination}/{child.relative_to(source).as_posix()}")
                for child in sorted(source.rglob("*"))
                if child.is_file()
            )
        else:
            resolved.append((source, destination))
    return resolved


def on_files(files, config):
    """Add every pinned asset to the build, or refuse to build at all.

    Refusing is the point. Building anyway would 404 the asset and put the site straight back on a
    third party's CDN — for mermaid silently, because Material's fallback is exactly that; for
    KaTeX visibly wrong, because the maths would lose its typeface. Neither failure announces
    itself, so the build has to.
    """
    # Everything is checked before anything is added: a build that fails half-way through would
    # have to be reasoned about, and which asset is reported would depend on declaration order.
    for asset in ASSETS:
        if not asset.package.is_dir():
            raise PluginError(
                f"{asset.name} is not installed, so the documentation would fall back to loading "
                f"it from a CDN in every reader's browser. Run:\n\n    {asset.install}\n\n"
                f"(expected: {asset.package})"
            )
        for source, _ in _pairs(asset):
            if not source.is_file():
                raise PluginError(f"{asset.name} is installed but {source} is missing")

    for asset in ASSETS:
        for source, destination in _pairs(asset):
            # `abs_src_path` rather than `content`: mkdocs copies the file straight through
            # instead of holding several megabytes in memory for the whole build.
            files.append(File.generated(config, destination, abs_src_path=str(source)))
    return files
