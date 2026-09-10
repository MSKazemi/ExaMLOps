#!/usr/bin/env python3
"""One version, every artifact: check, set, and verify the release version (ADR 0129).

A release publishes a wheel, container images and a Helm chart from one tag. Each of those
reads its version from a different file, and until this script only one pair of them
(`Chart.yaml` appVersion against the root `pyproject.toml`) was held together by a test. The
root `pyproject.toml` `[project].version` is the single source; everything listed in
``VERSION_FILES`` is a copy that must agree with it.

    release_version.py check               exit 1 naming every copy that disagrees
    release_version.py set 0.52.0          rewrite the source and every copy
    release_version.py tag v0.52.0         exit 1 unless the tag is exactly v<version>
    release_version.py notes 0.52.0        print that version's CHANGELOG section(s)
    release_version.py current             print the version

``tag`` is the release workflow's first step: a tag pushed against a tree whose files say
another version would publish artifacts that disagree about what they are. ``notes`` is the
GitHub Release body; it exits 1 on an empty section, which is the cheapest proof the
CHANGELOG was written before tagging.

Standard library only: this runs on a bare runner before any dependency is installed.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path("pyproject.toml")
CHANGELOG = Path("CHANGELOG.md")

# SemVer 2.0 core with optional pre-release; build metadata is not a release version.
_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?")


@dataclass(frozen=True)
class VersionField:
    """One place a copy of the version lives: a file and the one line that carries it."""

    path: Path
    # Must contain exactly one group, the version. Anchored and MULTILINE, so it matches the
    # field itself and never a dependency pin or a comment that happens to hold a version.
    pattern: str
    label: str

    def read(self, root: Path) -> str | None:
        match = re.search(self.pattern, (root / self.path).read_text(), re.M)
        return match.group(1) if match else None

    def write(self, root: Path, version: str) -> None:
        target = root / self.path
        text = target.read_text()
        new, count = re.subn(
            self.pattern,
            lambda m: m.group(0).replace(m.group(1), version, 1),
            text,
            count=1,
            flags=re.M,
        )
        if count != 1:
            raise SystemExit(f"{self.path}: {self.label} not found — refusing a partial bump")
        target.write_text(new)


# `[project]` is the first table carrying a top-level `version =` in each of these files; the
# `^` anchor keeps `ruff==…`-style pins and indented tables from matching.
_PYPROJECT = r'^version = "([^"]+)"'

SOURCE_FIELD = VersionField(SOURCE, _PYPROJECT, "workspace version (the source)")
VERSION_FILES: tuple[VersionField, ...] = (
    VersionField(Path("platform/cli/pyproject.toml"), _PYPROJECT, "examlops wheel"),
    VersionField(Path("pipelines/pyproject.toml"), _PYPROJECT, "examlops-pipelines"),
    VersionField(Path("serving/pyproject.toml"), _PYPROJECT, "examlops-serving"),
    VersionField(
        Path("platform/infra/helm/examlops/Chart.yaml"), r"^version: (\S+)", "Helm chart version"
    ),
    VersionField(
        Path("platform/infra/helm/examlops/Chart.yaml"),
        r'^appVersion: "([^"]+)"',
        "Helm appVersion (default image tag)",
    ),
)


def current(root: Path = ROOT) -> str:
    version = SOURCE_FIELD.read(root)
    if not version:
        raise SystemExit(f"{SOURCE}: no [project] version found")
    return version


def mismatches(root: Path = ROOT) -> list[str]:
    """Every copy that disagrees with the source, as printable lines. Empty means consistent."""
    want = current(root)
    problems = []
    for field in VERSION_FILES:
        got = field.read(root)
        if got != want:
            problems.append(f"{field.path}: {field.label} is {got or 'MISSING'}, expected {want}")
    return problems


def set_version(version: str, root: Path = ROOT) -> None:
    if not _SEMVER.fullmatch(version):
        raise SystemExit(f"{version!r} is not a SemVer version (X.Y.Z[-pre])")
    # Validate every target before writing any, so a missing field cannot leave a half-bump.
    for field in (SOURCE_FIELD, *VERSION_FILES):
        if field.read(root) is None:
            raise SystemExit(f"{field.path}: {field.label} not found — nothing was changed")
    for field in (SOURCE_FIELD, *VERSION_FILES):
        field.write(root, version)


def check_tag(tag: str, root: Path = ROOT) -> str | None:
    """None when the tag names this tree's version, else the reason it does not."""
    want = f"v{current(root)}"
    if tag != want:
        return f"tag {tag!r} does not match the tree's version — expected {want!r}"
    return None


def release_notes(version: str, root: Path = ROOT) -> str:
    """The body of every `## [version]` / `## [vversion]` section, in file order.

    Semantics match the extractor the GitLab release job used (and that
    tests/unit/test_release_notes_are_extractable.py pinned): both heading styles, a version
    that heads more than one section contributes all of them, and a section ends at the next
    `## [` heading — never at end-of-file by accident of rule order.
    """
    heading = re.compile(rf"^## \[v?{re.escape(version)}\]")
    out: list[str] = []
    inside = False
    for line in (root / CHANGELOG).read_text().splitlines():
        if heading.match(line):
            inside = True
            continue
        if inside and line.startswith("## ["):
            inside = False
        if inside:
            out.append(line)
    return "\n".join(out).strip("\n") + ("\n" if out else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="exit 1 if any copy disagrees with the source")
    sub.add_parser("current", help="print the version")
    p_set = sub.add_parser("set", help="rewrite the source and every copy")
    p_set.add_argument("version")
    p_tag = sub.add_parser("tag", help="exit 1 unless TAG == v<version>")
    p_tag.add_argument("tag")
    p_notes = sub.add_parser("notes", help="print a version's CHANGELOG section")
    p_notes.add_argument("version", help="X.Y.Z; a leading v is accepted")
    args = parser.parse_args(argv)

    if args.cmd == "current":
        print(current())
    elif args.cmd == "check":
        problems = mismatches()
        if problems:
            print("Version copies disagree with pyproject.toml:", file=sys.stderr)
            for line in problems:
                print(f"  {line}", file=sys.stderr)
            print("Fix: python platform/ci/release_version.py set <version>", file=sys.stderr)
            return 1
        print(f"version {current()} — {len(VERSION_FILES)} copies agree")
    elif args.cmd == "set":
        set_version(args.version)
        print(f"version set to {args.version} in {1 + len(VERSION_FILES)} fields")
    elif args.cmd == "tag":
        reason = check_tag(args.tag)
        if reason:
            print(reason, file=sys.stderr)
            return 1
        print(f"{args.tag} matches the tree")
    elif args.cmd == "notes":
        version = args.version.removeprefix("v")
        body = release_notes(version)
        if not body.strip():
            print(
                f"CHANGELOG.md has no '## [{version}]' section — nothing to release.",
                file=sys.stderr,
            )
            return 1
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
