# tests/unit/test_guard_file_enumeration.py
"""The enumerator every repository guard scans the tree with.

`tests/unit/_guard_deps.tracked_and_new_files` decides *which files exist* for the guards that
answer questions about the repository itself — what the public tree leaks, whether every
environment variable is documented. That makes it the one place where a mistake is invisible in
the worst direction: a guard that scans fewer files than it should still passes.

It has two jobs, and both have already been got wrong once:

- **it must find the tree.** An enumerator that returns nothing makes every guard built on it pass
  by checking nothing, which is why it asserts rather than returning an empty list (the same trap
  `_python_files` in `test_no_bare_sqlite_connect.py` documents);
- **it must not return a path that is gone.** `git ls-files --cached` lists the index, which holds
  a file deleted in the working tree whose deletion is not staged — ` D` in `git status`, and a
  normal state in a tree several people work in. Guards that then read each path died with
  `FileNotFoundError` from inside `pathlib`: on 2026-09-13 one unstaged deletion failed five tests
  across three guards, none of which had anything to say about that deletion, and for three days
  those failures masked two genuinely undocumented environment variables.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit._guard_deps import REPO_ROOT, tracked_and_new_files


def test_it_finds_the_tree():
    """If this is ever empty, every guard built on it passes by scanning nothing."""
    files = tracked_and_new_files("*.py")
    assert len(files) > 500, len(files)
    assert "tests/unit/_guard_deps.py" in files


def test_every_path_it_returns_can_be_read():
    """The property the guards depend on: what comes back has content to scan."""
    unreadable = [name for name in tracked_and_new_files() if not (REPO_ROOT / name).is_file()]
    assert not unreadable, unreadable


def test_a_tracked_file_deleted_in_the_working_tree_is_not_returned():
    """Driven through the real seam — the index says the file is there, the disk says otherwise.

    Done in a throwaway repository rather than by deleting something here: this suite runs in a
    tree other people are working in, and a guard's test may not stage or remove their files.
    """
    import os
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        run = lambda *args: subprocess.run(args, cwd=repo, check=True, capture_output=True)  # noqa: E731
        run("git", "init", "-q")
        (repo / "kept.py").write_text("x = 1\n")
        (repo / "removed.py").write_text("y = 2\n")
        run("git", "add", "kept.py", "removed.py")
        # Staged, not committed: `--cached` reads the index, so this is the state under test
        # without a commit — which this machine's identity hook would refuse anyway.
        (repo / "removed.py").unlink()  # gone from disk, still in the index — git's ` D`

        listed = subprocess.run(
            ["git", "ls-files", "--cached", "*.py"], cwd=repo, capture_output=True, text=True
        ).stdout.split()
        assert "removed.py" in listed, "git must still list it, or this test proves nothing"

        # The function reads REPO_ROOT at import, so point it at the throwaway tree.
        import tests.unit._guard_deps as guard_deps

        original = guard_deps.REPO_ROOT
        cwd = os.getcwd()
        try:
            guard_deps.REPO_ROOT = repo
            os.chdir(repo)
            got = guard_deps.tracked_and_new_files("*.py")
        finally:
            guard_deps.REPO_ROOT = original
            os.chdir(cwd)

    assert got == ["kept.py"], got


def test_it_says_so_when_the_tree_yields_nothing():
    """An empty tree must fail loudly, not return an empty list a guard would call clean."""
    import os
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        import tests.unit._guard_deps as guard_deps

        original = guard_deps.REPO_ROOT
        cwd = os.getcwd()
        try:
            guard_deps.REPO_ROOT = repo
            os.chdir(repo)
            with pytest.raises(AssertionError, match="returned nothing"):
                guard_deps.tracked_and_new_files("*.py")
        finally:
            guard_deps.REPO_ROOT = original
            os.chdir(cwd)


def test_the_guards_that_scan_the_tree_use_it():
    """Two guards had their own copy of the listing, and both carried the same defect. A third
    copy would carry it again, so the ones that read every path go through this function."""
    for name in ("test_public_tree_privacy.py", "test_env_vars_are_documented.py"):
        text = (REPO_ROOT / "tests" / "unit" / name).read_text(encoding="utf-8")
        assert "tracked_and_new_files" in text, name
        assert "ls-files" not in text, f"{name} enumerates the tree itself again"


# ── the other half: scanning a directory rather than the index ───────────────


def test_scan_files_finds_the_tree_and_skips_caches():
    from tests.unit._guard_deps import scan_files

    files = scan_files(REPO_ROOT / "platform" / "cli" / "src" / "examlops")
    assert len(files) > 200, len(files)
    assert not [p for p in files if "__pycache__" in p.parts]
    assert all(p.is_file() for p in files)


def test_scan_files_refuses_an_empty_result():
    """The property the negative-claim guards depend on: no offenders found in *nothing* scanned
    is not compliance. Both a missing directory and a pattern that stopped matching say so."""
    import tempfile
    from pathlib import Path

    from tests.unit._guard_deps import scan_files

    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp)
        (empty / "notes.txt").write_text("not python\n")
        with pytest.raises(AssertionError, match="found nothing"):
            scan_files(empty)  # the directory exists; the pattern matches nothing
        with pytest.raises(AssertionError, match="found nothing"):
            scan_files(empty / "gone")  # the directory does not exist


def test_scan_files_honours_a_non_recursive_scan():
    import tempfile
    from pathlib import Path

    from tests.unit._guard_deps import scan_files

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "top.md").write_text("a\n")
        (root / "deep").mkdir()
        (root / "deep" / "under.md").write_text("b\n")
        assert [p.name for p in scan_files(root, "*.md", recursive=False)] == ["top.md"]
        # sorted by full path, so `deep/under.md` comes before `top.md`
        assert sorted(p.name for p in scan_files(root, "*.md")) == ["top.md", "under.md"]
