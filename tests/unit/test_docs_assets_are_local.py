"""Every front-end asset the documentation loads comes from the documentation, and cannot stop.

Two used to come from third parties while a reader had the page open — mermaid from unpkg (a
*floating* major, so Subresource Integrity was impossible and the file could change under us) and
KaTeX from jsdelivr. Both are pinned in this repository now and added to the build by
`docs/overrides/docs_assets_hook.py`.

These assertions are about configuration, which is the half that can be checked without a browser:
the hook is registered, every asset it declares is loaded from where it puts it, the local mermaid
is loaded *first* (Material's own loader would otherwise win the race on a slow page), and a missing
package fails the build instead of quietly restoring the CDN.

What a reader's browser actually fetches — and whether the maths really has KaTeX's own fonts
rather than a fallback — is measured in `tests/integration/test_docs_site_is_self_contained.py`,
because no amount of config reading can see either.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MKDOCS = REPO_ROOT / "mkdocs.yml"
HOOK_URI = "docs/overrides/docs_assets_hook.py"
MERMAID = "assets/js/mermaid.min.js"

yaml = pytest.importorskip("yaml")
sys.path.insert(0, str(REPO_ROOT / "docs" / "overrides"))

import docs_assets_hook  # noqa: E402


def _config() -> dict:
    """mkdocs.yml without resolving its `!!python/name:` tags, which are not plain YAML."""
    text = re.sub(r"!!python/\S+", "", MKDOCS.read_text(encoding="utf-8"))
    return yaml.safe_load(text)


def _loaded() -> set[str]:
    config = _config()
    return {
        entry
        for key in ("extra_css", "extra_javascript")
        for entry in (config.get(key) or [])
        if isinstance(entry, str)
    }


def test_the_hook_is_registered():
    assert HOOK_URI in (_config().get("hooks") or []), (
        "without the hook nothing is added to the build, every asset 404s, and the site falls "
        "back to third-party CDNs in the reader's browser"
    )


def test_the_local_mermaid_is_loaded_before_everything_else():
    """Material's loader runs on `document$`; ours must have executed by then, and putting it
    anywhere but first is an invitation for a future entry to be inserted above it."""
    scripts = _config().get("extra_javascript") or []

    assert scripts and scripts[0] == MERMAID, f"expected {MERMAID} first, got {scripts[:2]}"


def test_nothing_the_site_loads_comes_from_a_third_party():
    """The whole point, stated once: no `http(s)://` entry in either list."""
    remote = sorted(entry for entry in _loaded() if entry.startswith(("http://", "https://")))

    assert not remote, f"loaded from a third party: {remote}"


def test_every_declared_asset_is_one_the_site_loads_or_a_file_it_needs():
    """A destination nobody references is dead weight in the build; a reference the hook does not
    produce is a 404. The fonts are the exception and the reason the whole package is served:
    `katex.min.css` names them itself, with relative urls."""
    declared = {destination for asset in docs_assets_hook.ASSETS for _, destination in asset.files}
    referenced = _loaded()

    unused = {d for d in declared if d not in referenced and "fonts" not in d}

    assert not unused, f"the hook serves what nothing loads: {sorted(unused)}"
    assert declared & referenced, "the hook and mkdocs.yml agree about nothing at all"


def test_every_asset_the_site_loads_locally_is_served_by_the_hook_or_lives_in_docs():
    """The other direction: a local path that neither the hook produces nor `docs/` contains."""
    declared = {destination for asset in docs_assets_hook.ASSETS for _, destination in asset.files}
    missing = [
        entry
        for entry in sorted(_loaded())
        if entry not in declared and not (REPO_ROOT / "docs" / entry).exists()
    ]

    assert not missing, f"referenced but neither generated nor present in docs/: {missing}"


def test_a_declared_directory_is_served_file_by_file_keeping_its_layout():
    """`katex.min.css` names its fonts with urls relative to itself, so the layout is the contract:
    `assets/katex/fonts/KaTeX_Math-Italic.woff2` has to exist because the stylesheet at
    `assets/katex/katex.min.css` asks for `fonts/KaTeX_Math-Italic.woff2`. Serving the directory as
    one entry, or flattening it, puts the maths back in a fallback face."""
    katex = next(asset for asset in docs_assets_hook.ASSETS if asset.name == "katex")
    if not katex.package.is_dir():
        pytest.skip("npm ci --prefix platform/ci/katex")

    destinations = [destination for _, destination in docs_assets_hook._pairs(katex)]
    fonts = [d for d in destinations if d.startswith("assets/katex/fonts/")]

    assert len(fonts) > 50, f"the font directory was not expanded: {destinations[:5]}"
    assert "assets/katex/fonts/KaTeX_Math-Italic.woff2" in fonts
    assert all(not d.endswith("/fonts") for d in destinations), "a directory was served as a file"


@pytest.mark.parametrize("asset", docs_assets_hook.ASSETS, ids=lambda a: a.name)
def test_a_missing_package_refuses_to_build(asset, tmp_path, monkeypatch):
    """The failure mode these guards exist for: build anyway, 404 the asset, and the published site
    returns to the CDN — silently for mermaid, and visibly wrong for the maths."""
    from mkdocs.exceptions import PluginError

    absent = [
        a if a is not asset else type(a)(**{**a.__dict__, "package": tmp_path / "absent"})
        for a in docs_assets_hook.ASSETS
    ]
    monkeypatch.setattr(docs_assets_hook, "ASSETS", tuple(absent))

    with pytest.raises(PluginError, match=re.escape(asset.install)):
        docs_assets_hook.on_files([], {})


def test_the_install_command_names_the_directory_the_package_lives_in():
    """The message a contributor acts on. A stale `--prefix` sends them to install into a
    directory the hook does not read, and the build keeps failing with advice that cannot work."""
    for asset in docs_assets_hook.ASSETS:
        tool_dir = asset.package.parent.parent.relative_to(REPO_ROOT).as_posix()

        assert tool_dir in asset.install, (
            f"{asset.name}: {asset.install!r} does not name {tool_dir}"
        )
        assert (REPO_ROOT / tool_dir / "package.json").is_file(), f"{tool_dir} has no package.json"
