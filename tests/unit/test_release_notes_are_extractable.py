"""Every tag's release notes must be extractable by the job that publishes them.

`release:gitlab` builds the GitLab Release description by awk-ing this version's section out
of `CHANGELOG.md`, and exits 1 when the result is empty -- deliberately, as the cheapest check
that the CHANGELOG was updated before tagging. That makes the extractor a release blocker: if
it cannot find a section that is *present*, a tag pipeline goes red after every test job passed.

It could not find two. The CHANGELOG carries both `## [0.46.0]` and `## [v0.48.0]` heading
styles, and the job strips the leading `v` from the tag before matching -- so the two most
recent releases' notes were unreachable, and the next tag in that style would have failed.
Separately, `0.37.0` heads two sections. Both are read today only because awk evaluates the
heading rule before the `exit` rule -- change the order, or let another version's heading fall
between them, and 317 lines (the whole enterprise-readiness Phase 0 wave) vanish from that
release with no error. `exit` is now `inside = 0`, and the behaviour is asserted rather than
left to rule ordering.

This test runs the *real* awk program, parsed out of `.gitlab-ci.yml`, so it cannot drift from
what CI does.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.unit._guard_deps import require_binary

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".gitlab-ci.yml"
CHANGELOG = ROOT / "CHANGELOG.md"

# Three tags predate the release job and have no CHANGELOG section at all. They are history and
# cannot be written now without inventing content; naming them here is what stops a *new* tag
# from joining them silently.
UNDOCUMENTED_RELEASES = {"v0.29.0", "v0.30.0", "v0.36.0"}


def _awk_program() -> str:
    """The awk source the release job runs, taken from .gitlab-ci.yml itself."""
    text = CI.read_text()
    match = re.search(r"awk -v v=\"\$VERSION\" '(.*?)' CHANGELOG\.md", text, re.S)
    assert match, ".gitlab-ci.yml no longer contains the release-notes awk program"
    return match.group(1)


def _tags() -> list[str]:
    require_binary("git", "every tag yields non-empty release notes")
    out = subprocess.run(
        ["git", "tag"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return sorted(t for t in out.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t))


def _extract(version: str) -> str:
    # These five tests used to carry `skipif(which("awk") is None)`. That is the release gate's
    # own extraction step: if awk is missing they proved nothing, and a skip reports success —
    # the same anti-pattern as the thirteen guards that crashed for want of `git`, in its
    # quieter and therefore worse form. Fail with a sentence instead.
    require_binary("awk", "release:gitlab can extract non-empty notes for every tag")
    return subprocess.run(
        ["awk", "-v", f"v={version}", _awk_program(), str(CHANGELOG)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_there_are_tags_to_check():
    """Otherwise the sweep below passes by inspecting nothing."""
    assert len(_tags()) >= 10, _tags()


def test_every_tag_extracts_non_empty_release_notes():
    empty = [t for t in _tags() if not _extract(t[1:]).strip()]
    unexpected = sorted(set(empty) - UNDOCUMENTED_RELEASES)
    assert not unexpected, (
        "release:gitlab would exit 1 on these tags — it found no CHANGELOG section:\n  "
        + "\n  ".join(unexpected)
        + "\nAdd a `## [<version>]` section, or the tag has nothing to describe."
    )


def test_the_undocumented_list_does_not_outlive_its_reason():
    """A stale exemption is an exemption that hides a live defect."""
    still_empty = {t for t in UNDOCUMENTED_RELEASES if not _extract(t[1:]).strip()}
    assert still_empty == UNDOCUMENTED_RELEASES, (
        "these tags now have a CHANGELOG section — remove them from UNDOCUMENTED_RELEASES: "
        + ", ".join(sorted(UNDOCUMENTED_RELEASES - still_empty))
    )


def test_a_version_heading_in_either_style_is_found():
    """The CHANGELOG uses both `## [0.46.0]` and `## [v0.48.0]`; neither may be invisible."""
    assert _extract("0.46.0").strip(), "bare-style heading `## [0.46.0]` not matched"
    assert _extract("0.48.0").strip(), "v-prefixed heading `## [v0.48.0]` not matched"


def test_a_version_heading_more_than_once_contributes_every_section():
    """`0.37.0` heads two sections; both must reach the release notes."""
    heads = len(re.findall(r"^## \[v?0\.37\.0\]", CHANGELOG.read_text(), re.M))
    assert heads == 2, f"expected 0.37.0 to head 2 sections, found {heads} — update this test"
    body = _extract("0.37.0")
    assert "Enterprise-readiness Phase 0 completion wave" in body, (
        "the second 0.37.0 section is missing from the extracted notes"
    )
