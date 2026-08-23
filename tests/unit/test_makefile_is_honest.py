"""The Makefile is a published interface, so hold it to the same rule as the CLI's help.

Three claims, each of which had drifted or could:

1. Every target is ``.PHONY``. The file already declared 78 of 87 — the intent was clearly
   full coverage, and twelve had drifted out as targets were added. None collides with a
   real path *today*, which is exactly why it goes unnoticed: the day someone adds a
   ``smoke-check`` script or a ``skipper`` symlink at the repo root, ``make`` answers
   "nothing to be done" and the target silently stops running.
2. ``make help`` lists every target. It is the discovery surface; a target missing from it
   effectively does not exist for a new operator.
3. Every ``make <target>`` named in the docs is a target that exists. ``docs/guides/cicd.md``
   sent readers to ``make sample-data`` inside ``modelzoo/``, whose Makefile is empty.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"

# Prose false positives: "make it clear", "make a copy", "make the call".
_PROSE = {
    "a",
    "active",
    "an",
    "exact",
    "here",
    "it",
    "its",
    "make",
    "python3",
    "sure",
    "that",
    "the",
    "them",
    "this",
    "us",
    "exa",
}


def _targets() -> set[str]:
    return set(re.findall(r"^([a-zA-Z][\w.-]*):", MAKEFILE.read_text(), re.M))


def _phony() -> set[str]:
    # `.PHONY:` continues across backslash-newline; a scan that misses that reports 5
    # declarations instead of 78 and every target as missing.
    joined = re.sub(r"\\\n\s*", " ", MAKEFILE.read_text())
    names: set[str] = set()
    for match in re.finditer(r"^\.PHONY:(.*)$", joined, re.M):
        names.update(match.group(1).split())
    return names


def test_every_target_is_phony():
    targets = _targets()
    assert len(targets) > 50, "target scan found almost nothing — the regex is broken"
    missing = sorted(targets - _phony())
    assert not missing, (
        "these targets are not .PHONY, so make will skip them if a path of the same name "
        f"ever appears: {missing}"
    )


def test_make_help_lists_every_target():
    out = subprocess.run(["make", "help"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout)  # help is colourised
    listed = {m.group(1) for m in re.finditer(r"^  ([a-z][\w.-]*)\s", plain, re.M)}
    undocumented = sorted(_targets() - listed)
    assert not undocumented, f"targets absent from `make help`: {undocumented}"


def test_every_documented_make_target_exists():
    targets = _targets()
    missing: dict[str, list[str]] = {}
    for doc in [*(ROOT / "docs").rglob("*.md"), ROOT / "README.md"]:
        if not doc.exists():
            continue
        for name in re.findall(r"\bmake ([a-z][\w.-]*)", doc.read_text(errors="ignore")):
            if name.endswith("-"):
                continue  # a glob stem, e.g. `make stack-*`
            if name in _PROSE or name in targets:
                continue
            missing.setdefault(name, []).append(str(doc.relative_to(ROOT)))
    assert not missing, f"docs name make targets that do not exist: {missing}"


# The lint scope is written out in full in five places — `make lint`, `make lint-fix`,
# `make ci-examlops`, `make preflight` (twice), `.gitlab-ci.yml` and `.github/workflows/ci.yml`.
# CLAUDE.md names this as an invariant that "must agree" and nothing enforced it. When they
# disagree the failure is silent and one-directional: the local gate passes over a narrower
# tree than CI checks, so the first sign is a red pipeline after a push.

_SCOPE_LINE = re.compile(r"ruff (?:check|format)(?: --check| --fix)? ((?:[\w./-]+/ ?)+)")


def _scopes(path: Path) -> set[frozenset[str]]:
    found = set()
    for match in _SCOPE_LINE.finditer(path.read_text()):
        dirs = frozenset(match.group(1).split())
        if "platform/cli/src/" in dirs:  # the examlops scope, not modelzoo's or the adapter's
            found.add(dirs)
    return found


def test_the_lint_scope_is_the_same_everywhere_it_is_written():
    places = {
        "Makefile": MAKEFILE,
        ".gitlab-ci.yml": ROOT / ".gitlab-ci.yml",
        ".github/workflows/ci.yml": ROOT / ".github" / "workflows" / "ci.yml",
    }
    seen: dict[frozenset[str], list[str]] = {}
    for label, path in places.items():
        assert path.exists(), f"{label} is missing"
        scopes = _scopes(path)
        assert scopes, f"no examlops ruff scope found in {label} — the scan is broken"
        for scope in scopes:
            seen.setdefault(scope, []).append(label)
    assert len(seen) == 1, (
        "the lint scope disagrees between gates, so the local gate and CI check different "
        f"trees: { {tuple(sorted(k)): v for k, v in seen.items()} }"
    )


def test_the_generated_cli_reference_is_not_stale():
    """`docs/reference/cli-generated.md` is committed but nothing regenerated it.

    Only `make docs-cli` writes it, no CI job runs that, and no test compared it — so it
    drifts silently. When this was first checked it was missing the whole `exa agent memory`
    group (four commands, ADR 0034's right-to-erasure surface), which had shipped days
    earlier. A generated file that nobody regenerates is worse than no file: a reader
    trusts it precisely because it looks machine-produced.
    """
    generated = ROOT / "docs" / "reference" / "cli-generated.md"
    assert generated.exists(), "the generated CLI reference is missing"
    out = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "exa"), "docs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.returncode == 0, out.stderr
    if out.stdout.strip() != generated.read_text().strip():
        raise AssertionError(
            "docs/reference/cli-generated.md no longer matches the live command tree. "
            "Regenerate it with `make docs-cli` and commit the result."
        )
