"""`.dockerignore` keeps private and local state out of the build context — without starving it.

The root `.dockerignore` governs the context of every image built from the repository root, which
is every image the release publishes with `context: .`. It must exclude what no image needs and
some must never see — the private git (`.git-private`), live SQLite datastores, `.env` files at
any depth, design records, host `node_modules` — and it must not exclude anything a Dockerfile
COPYs, or the image silently builds without it (or fails only in the release).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.unit.test_dockerfile_build_context import _copy_sources

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def _patterns() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _to_regex(pattern: str) -> re.Pattern[str]:
    """Docker's matching, as far as this file uses it: root-relative, `**` crosses directories."""
    out, i = "", 0
    pattern = pattern.strip("/")
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        elif pattern[i] == "[":
            end = pattern.index("]", i)
            out, i = out + pattern[i : end + 1], end + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + "$")


def _ignored(path: str) -> bool:
    """True when `path` — or a directory containing it — matches an exclusion."""
    parts = path.strip("/").split("/")
    prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    regexes = [_to_regex(p) for p in _patterns() if not p.startswith("!")]
    return any(rx.match(prefix) for rx in regexes for prefix in prefixes)


def _root_context_dockerfiles() -> list[Path]:
    images = yaml.safe_load(RELEASE.read_text())["jobs"]["images"]["strategy"]["matrix"]["include"]
    return [ROOT / i["dockerfile"] for i in images if i.get("context", ".") == "."]


def test_the_matcher_behaves_like_docker():
    assert _ignored(".git-private/objects/ab/cd")
    assert _ignored("platform.db") and _ignored("deep/dir/state.db")
    assert _ignored("platform/infra/docker-compose/.env")
    assert _ignored("platform/services/dashboard/frontend/node_modules/x/index.js")
    assert not _ignored("docs/guides/release-process.md")
    assert not _ignored("platform/cli/src/examlops/__init__.py")


def test_private_and_local_state_never_enters_the_context():
    for path in (
        ".git-private/HEAD",
        ".claude/memory/MEMORY.md",
        "design/adr/0129-github-is-the-release-authority.md",
        "platform.db",
        "platform.db-wal",
        ".env",
        "platform/infra/docker-compose/.env",
        "platform/services/dashboard/frontend/node_modules/react/index.js",
        "modelzoo/pyproject.toml",
    ):
        assert _ignored(path), f"{path} would be sent to the builder"


def test_every_copy_source_survives_the_ignore_file():
    dockerfiles = _root_context_dockerfiles()
    assert len(dockerfiles) >= 4, dockerfiles
    starved = [
        f"{df.relative_to(ROOT)}: COPY {src}"
        for df in dockerfiles
        for src in _copy_sources(df)
        if not src.startswith(("$", "--")) and _ignored(src)
    ]
    assert not starved, "excluded by .dockerignore but COPY'd by an image:\n  " + "\n  ".join(
        starved
    )
