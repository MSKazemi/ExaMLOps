"""Make a repository guard say *why* it could not run.

Seven guards in this directory answer questions about the repository itself — what the
public tree tracks, whether every environment variable is documented, whether each tag
yields release notes — by shelling out to ``git`` or ``make``. When the binary is absent
they do not fail an assertion; they raise ``FileNotFoundError`` from deep inside
``subprocess``, which reads as a broken test rather than as an unprotected tree.

That is not hypothetical. ``python:3.12-slim`` ships neither binary, so on GitLab pipeline
#3241 (2026-08-25) thirteen of these guards failed that way in ``test:examlops`` and the
same thirteen in ``test:postgres`` — including the guard that stops private assistant
material reaching the *published* tree. The protection had never run in CI at all.

``require_binary`` turns that into one sentence naming the binary and what is consequently
unchecked. It **fails**; it deliberately does not skip. A skip is how this became invisible
in the first place — the guard has to be loud enough that someone installs the binary.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def require_binary(name: str, guards: str) -> str:
    """Return the path to ``name``, or fail this test saying what is left unguarded.

    ``guards`` completes the sentence "this check is what ensures …", so the failure names
    the protection that is missing rather than only the tool.
    """
    found = shutil.which(name)
    if found is None:
        pytest.fail(
            f"cannot run this guard: {name!r} is not on PATH, so nothing here verified "
            f"that {guards}. This is an unguarded tree, not a passing one — install "
            f"{name!r} in the environment running the tests; a CI image is not required to "
            "have it (python:*-slim, for one, ships neither 'git' nor 'make').",
            pytrace=False,
        )
    return found


def tracked_and_new_files(*patterns: str) -> list[str]:
    """Repository-relative paths of the files a guard should scan: tracked plus untracked-eligible.

    `--others --exclude-standard` is what lets a guard fail on the change that introduces a
    problem rather than on some later one, by which point the undocumented knob or the leaked
    path is already released.

    **Paths that no longer exist on disk are dropped**, and that is the whole reason this is
    shared rather than repeated. `git ls-files --cached` lists what the index holds, which
    includes a file deleted in the working tree but whose deletion is not staged yet — a normal,
    hours-long state in a tree several people work in, and the state `git status` calls ` D`.
    Guards that went on to read each path then died with `FileNotFoundError` from inside
    `pathlib`, which reads as a broken guard rather than as "someone is removing a file": on
    2026-09-13 one unstaged deletion (a dashboard router) failed five tests across three guards,
    none of which had anything to say about the deletion.

    A file that is gone has no content to scan, so skipping it is not a weakening: the guard's
    subject is the content of the tree.
    """
    require_binary("git", "the tree's own files can be enumerated at all")
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", *patterns],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    names = [name for name in listing.split("\0") if name]
    assert names, "`git ls-files` returned nothing — the guard would pass by scanning no files"
    return [name for name in names if (REPO_ROOT / name).is_file()]


def scan_files(root: Path, pattern: str = "*.py", *, recursive: bool = True) -> list[Path]:
    """Files under ``root`` matching ``pattern`` — and proof that there were any.

    Most guards in this directory assert a **negative**: no module imports the monolith, no SQL
    inlines a `LIKE` pattern, no caller bypasses `/v1`, no alert threshold is unreachable. A
    negative claim over an empty scan is the strongest possible pass — zero offenders found, green —
    and it is indistinguishable from total compliance. `test_platform_db_coupling_ratchet.py` was
    caught in exactly that state: with its root pointed at a directory that did not exist, both of
    its tests passed.

    `test_guard_paths_are_not_stale.py` closes the half of this where the *root* has gone; it
    deliberately does not try to recognise whether a module asserts its own non-emptiness, because a
    detector of assertion style is itself the kind of check that quietly stops matching. This closes
    the other half without needing one: the assertion lives in the scan, so every caller gets it by
    construction. The **pattern** is what this catches — a root that still exists while `*.py`,
    `*.md` or `docker-compose*.yml` stops matching anything after a rename.

    `__pycache__` is excluded, and directories are not returned: both are noise in every caller.
    """
    matches = root.rglob(pattern) if recursive else root.glob(pattern)
    files = sorted(p for p in matches if p.is_file() and "__pycache__" not in p.parts)
    assert files, (
        f"scanned {root} for {pattern!r} and found nothing — this guard's subject is missing, not "
        "clean. Until the path and pattern are right it enforces nothing, and an empty scan is "
        "exactly what total compliance looks like."
    )
    return files
