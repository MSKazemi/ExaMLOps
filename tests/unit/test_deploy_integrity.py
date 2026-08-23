"""Guards for the deploy integrity checker (`platform/ci/verify_deploy_integrity.py`).

The check exists because an additive rsync left the lxp node with a `platform_db.py`
whose re-export barrel named symbols its `serving.py` did not have, killing every `exa`
command at import — while the source machine's test suite stayed green. These tests pin
the three properties that make the checker worth running: it notices *content* drift (not
just missing files), it does not fail a deploy over files the target has and the source
does not, and it refuses to call an empty manifest a match.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "platform" / "ci" / "verify_deploy_integrity.py"


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
    )


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def test_identical_trees_pass(tmp_path):
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})
    dst = _tree(tmp_path / "dst", {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})
    manifest = _run("manifest", str(src))
    assert manifest.returncode == 0
    out = _run("verify", str(dst), "-", stdin=manifest.stdout)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "2 files identical" in out.stdout


def test_content_drift_is_caught_even_when_no_file_is_missing(tmp_path):
    """The lxp failure mode: nothing missing, one file simply older."""
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})
    dst = _tree(tmp_path / "dst", {"a.py": "x = 1\n", "pkg/b.py": "y = 999  # stale\n"})
    manifest = _run("manifest", str(src))
    out = _run("verify", str(dst), "-", stdin=manifest.stdout)
    assert out.returncode == 1
    assert "DIFFERENT  pkg/b.py" in out.stdout
    assert "MISSING" not in out.stdout


def test_missing_file_is_caught(tmp_path):
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})
    dst = _tree(tmp_path / "dst", {"a.py": "x = 1\n"})
    manifest = _run("manifest", str(src))
    out = _run("verify", str(dst), "-", stdin=manifest.stdout)
    assert out.returncode == 1
    assert "MISSING    pkg/b.py" in out.stdout


def test_extra_files_are_reported_but_do_not_fail(tmp_path):
    """The node holds 815 files that exist nowhere else; they must not block a deploy."""
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n"})
    dst = _tree(tmp_path / "dst", {"a.py": "x = 1\n", "node_only.py": "z = 3\n"})
    manifest = _run("manifest", str(src))
    out = _run("verify", str(dst), "-", stdin=manifest.stdout)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "extra      node_only.py" in out.stdout


def test_empty_manifest_is_not_a_match(tmp_path):
    """Otherwise a broken manifest step would silently certify any target."""
    dst = _tree(tmp_path / "dst", {"a.py": "x = 1\n"})
    out = _run("verify", str(dst), "-", stdin="")
    assert out.returncode == 1
    assert "refusing" in out.stderr


def test_missing_target_directory_fails(tmp_path):
    out = _run("verify", str(tmp_path / "nope"), "-", stdin="d  a.py\n")
    assert out.returncode == 1
    assert "does not exist" in out.stderr


def test_pycache_is_ignored(tmp_path):
    """Compiled artefacts differ harmlessly between machines."""
    src = _tree(tmp_path / "src", {"a.py": "x = 1\n"})
    dst = _tree(tmp_path / "dst", {"a.py": "x = 1\n", "__pycache__/a.py": "junk\n"})
    manifest = _run("manifest", str(src))
    out = _run("verify", str(dst), "-", stdin=manifest.stdout)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "__pycache__" not in out.stdout


# ── the `import` half ────────────────────────────────────────────────────────────────
# Content comparison needs a reference manifest. The import check needs nothing, which is
# what makes it usable on a node no one has a reference for — and it catches the one thing
# a manifest structurally cannot: a stale dependency in the target's venv, where every
# source file matches and the CLI still will not start.


def test_import_mode_passes_on_this_repo():
    """If the source tree does not import, the source is broken, not the target."""
    out = _run("import")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "imports" in out.stdout


def test_import_mode_reports_the_missing_symbol_not_just_a_failure(tmp_path):
    """The lxp shape: two modules, each valid alone, that disagree with each other.

    A content check against a stale manifest could pass this, and a syntax check certainly
    would. The operator needs the symbol name to know which file to re-sync.
    """
    pkg = tmp_path / "brokenpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "leaf.py").write_text("def still_here() -> int:\n    return 1\n")
    (pkg / "barrel.py").write_text("from brokenpkg.leaf import gone_symbol  # noqa: F401\n")

    out = subprocess.run(
        [sys.executable, str(SCRIPT), "import", "brokenpkg.barrel"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert out.returncode == 1
    assert "does not import" in out.stderr
    assert "gone_symbol" in out.stderr


def test_unknown_mode_is_a_usage_error_not_a_pass():
    out = _run("improt")
    assert out.returncode == 2
    assert "unknown mode" in out.stderr
