"""Every Dockerfile must be buildable from the tree we publish.

The chart in ``platform/infra/helm/`` tells strangers to run our images, and the public
repository is where they will look for the recipe.  A ``COPY`` whose source is private is
therefore not a packaging detail: it makes the published artifact unbuildable by its own
audience, and nothing else notices, because ``helm lint``/``template``/``--dry-run`` never
resolve a registry and the maintainer's checkout has the private path sitting right there.

The gap is real today -- ``modelzoo/`` left the public tree on 2026-08-07 and the control
plane's Dockerfile still copies it -- so this guard pins the gap rather than pretending it
away: the known set may not grow, and when the last entry is fixed the test says so.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# COPY sources that are NOT in the published tree, with the reason and the fix owner.
# Shrink this set; never grow it.  An entry here is a bug that is tracked, not a licence.
KNOWN_GAPS = {
    # `usecases/seanergy/pack.toml` puts `../../modelzoo` on sys.path and names its framework
    # classes, so the baked-in default pack drags the private upstream library into the image.
    # Fixing it is a distribution decision (ship the public image with no default pack, mount
    # the pack at runtime, or publish modelzoo), not a Dockerfile tweak.
    ("platform/services/control_plane/Dockerfile", "modelzoo"),
}

_COPY = re.compile(r"^\s*COPY\s+(?P<args>.+)$", re.IGNORECASE)


def _public_tree() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    paths = set()
    for line in out.splitlines():
        paths.add(line)
        parts = line.split("/")
        for i in range(1, len(parts)):
            paths.add("/".join(parts[:i]))
    return paths


def _copy_sources(dockerfile: Path) -> list[str]:
    """Source operands of every COPY that reads from the build context."""
    sources: list[str] = []
    for raw in dockerfile.read_text().splitlines():
        m = _COPY.match(raw)
        if not m:
            continue
        args = m.group("args").split()
        # `COPY --from=<stage>` reads from another stage, not the build context.
        if any(a.startswith("--from=") for a in args):
            continue
        args = [a for a in args if not a.startswith("--")]
        sources.extend(args[:-1])  # last operand is the destination
    return sources


def _dockerfiles() -> list[Path]:
    return sorted(
        p
        for p in REPO.rglob("Dockerfile*")
        if ".git" not in p.parts
        and "node_modules" not in p.parts
        and ".venv" not in p.parts
        and p.is_file()
        and not p.name.endswith((".md", ".txt"))
    )


def _gaps() -> set[tuple[str, str]]:
    public = _public_tree()
    found: set[tuple[str, str]] = set()
    for df in _dockerfiles():
        rel_df = df.relative_to(REPO).as_posix()
        if rel_df not in public:  # a private Dockerfile builds a private image; not our problem
            continue
        for src in _copy_sources(df):
            src = src.rstrip("/")
            if src in (".", "./") or "*" in src or "?" in src:
                continue
            # A build-arg path names a context outside this repository (the bridge image is
            # built from a sibling checkout); this guard cannot speak for that tree.
            if "${" in src or "$" in src:
                continue
            # The build context is whatever the compose file says. Repo-root is the common
            # case; several images are built from their own directory instead, so a source
            # counts as present if it resolves under either.
            local = (df.parent.relative_to(REPO) / src).as_posix()
            if src not in public and local not in public:
                found.add((rel_df, src))
    return found


def test_dockerfiles_are_discovered():
    """A parser that silently matches nothing would make every other test vacuous."""
    dfs = _dockerfiles()
    assert len(dfs) >= 5, [d.name for d in dfs]
    assert any(_copy_sources(d) for d in dfs)


def test_no_new_private_build_context_gaps():
    new = _gaps() - KNOWN_GAPS
    assert not new, (
        "Dockerfile COPY reads a path that is not in the published tree, so nobody outside "
        f"this checkout can build the image: {sorted(new)}"
    )


def test_known_gaps_are_still_real():
    """When a gap is fixed, delete its KNOWN_GAPS entry -- do not leave the excuse behind."""
    fixed = KNOWN_GAPS - _gaps()
    assert not fixed, f"fixed, so remove from KNOWN_GAPS: {sorted(fixed)}"


@pytest.mark.parametrize("dockerfile,source", sorted(KNOWN_GAPS))
def test_known_gap_is_documented(dockerfile, source):
    """Each tracked gap names the file it breaks, so the list cannot rot into folklore."""
    assert (REPO / dockerfile).exists()
    assert source in (REPO / dockerfile).read_text()
