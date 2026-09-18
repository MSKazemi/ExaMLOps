"""The documentation serves its diagram renderer itself, and cannot quietly stop.

mkdocs-material's bundle loads mermaid from `https://unpkg.com/mermaid@11/dist/mermaid.min.js`
unless `mermaid` is already defined. Measured in a headless browser against a real build, a
published page made two requests to unpkg.com; with `docs/overrides/mermaid_hook.py` and the
`extra_javascript` entry it loads, it makes none, and the diagrams render identically.

Three properties are worth holding, and only the first is about configuration:

* the hook is wired, and the script it adds is loaded **first** — a later entry would let
  Material's own lazy loader win the race on a slow page;
* a missing `node_modules` **fails the build** rather than silently falling back to the CDN,
  which is the same "did not run is not a pass" rule the diagram parser follows; and
* the file served is the one the diagram check validates, so the version readers run and the
  version CI parses cannot drift apart.

The end-to-end proof (load a built page, assert zero requests to unpkg.com) needs a browser and
lives in `tests/integration/test_docs_site_has_no_cdn_mermaid.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MKDOCS = REPO_ROOT / "mkdocs.yml"
HOOK = REPO_ROOT / "docs" / "overrides" / "mermaid_hook.py"
ASSET = "assets/js/mermaid.min.js"

yaml = pytest.importorskip("yaml")


def _config() -> dict:
    """mkdocs.yml without resolving its `!!python/name:` tags, which are not plain YAML."""
    text = re.sub(r"!!python/\S+", "", MKDOCS.read_text(encoding="utf-8"))
    return yaml.safe_load(text)


def test_the_hook_is_registered():
    hooks = _config().get("hooks") or []

    assert "docs/overrides/mermaid_hook.py" in hooks, (
        "without the hook the asset is never added to the build, `extra_javascript` 404s, and "
        "Material goes back to fetching mermaid from unpkg in every reader's browser"
    )


def test_the_local_mermaid_is_loaded_before_everything_else():
    """Material's loader runs on `document$`; ours must have executed by then, and putting it
    anywhere but first is an invitation for a future entry to be inserted above it."""
    scripts = _config().get("extra_javascript") or []

    assert scripts and scripts[0] == ASSET, f"expected {ASSET} first, got {scripts[:2]}"


def test_the_hook_serves_the_same_package_the_diagram_check_parses():
    """One pinned copy. If these diverged, the site could render with a version CI never parsed —
    which is precisely the drift `test_docs_mermaid.py` can only *detect* after the fact."""
    source = HOOK.read_text(encoding="utf-8")

    assert '"platform"' in source and '"ci"' in source and '"mermaid"' in source
    assert "node_modules" in source and "mermaid.min.js" in source


def test_a_missing_package_refuses_to_build(tmp_path, monkeypatch):
    """The failure mode this guard exists for: build anyway, 404 the asset, and the published site
    silently returns to the CDN. `mkdocs build` must stop instead."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "docs" / "overrides"))
    import mermaid_hook
    from mkdocs.exceptions import PluginError

    monkeypatch.setattr(mermaid_hook, "_SOURCE", tmp_path / "absent" / "mermaid.min.js")

    with pytest.raises(PluginError, match="npm ci --prefix platform/ci/mermaid"):
        mermaid_hook.on_files([], {})


def test_the_destination_matches_what_mkdocs_loads():
    """Two places name the same path; a rename in one of them 404s the other."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "docs" / "overrides"))
    import mermaid_hook

    assert mermaid_hook.DEST_URI == ASSET
