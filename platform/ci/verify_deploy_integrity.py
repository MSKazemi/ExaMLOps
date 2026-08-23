#!/usr/bin/env python3
"""Prove a deployed tree matches the source it was deployed from.

Why this exists
---------------
An *additive* rsync (no ``--delete``) can leave a target where every file is
individually plausible and the combination does not import. That is exactly what
happened to the lxp node on 2026-08-23: it had the Aug-19 ``platform_db.py`` — whose
line 1737 is a re-export *barrel*, one ``from examlops.data.serving import (…20 names…)``
— alongside the Jul-18 ``serving.py`` that predates half those names. A barrel import
fails whole, ``examlops.cli.main`` pulls it in transitively, and so *every* ``exa``
command died at import. Nothing reported it: the unit suite is green on the source
machine, and the source machine is not the machine that is broken.

No test on the source side can catch this, because the defect is a property of the
*target*. So the check has to run against the target, and it needs **two** halves that
fail in different ways:

``manifest`` / ``verify``
    Compare content. Catches the quiet case — a target that imports fine but is running
    code from three weeks ago. That is the third drifted file on the node,
    ``data_assets.py``, which differed without breaking any import.

``import``
    Import ``examlops.cli.main`` in a fresh interpreter. Needs no manifest, no source
    tree and no network, so it is the half that can run *anywhere*, including on a node
    nobody has a reference for. It also catches what a content check structurally cannot:
    a stale installed **dependency** in the target's venv, where every source file matches
    the manifest and the CLI still will not start.

Neither subsumes the other, so the deploy procedure runs both.

Usage
-----
Write a manifest from the source tree, then verify a target against it::

    python3 platform/ci/verify_deploy_integrity.py manifest platform/cli/src/examlops > /tmp/m.txt
    python3 platform/ci/verify_deploy_integrity.py verify   /path/to/target/examlops /tmp/m.txt

Or do both ends in one command over ssh (this is the form the deploy procedure uses)::

    python3 platform/ci/verify_deploy_integrity.py manifest platform/cli/src/examlops \\
      | ssh lxp-cpu01 'python3 - verify /nfs/share01/examlops/platform/cli/src/examlops -' \\
      < /dev/stdin

And, on the target, that the thing actually starts::

    ssh lxp-cpu01 'cd /nfs/share01/examlops && .venv/bin/python \\
      platform/ci/verify_deploy_integrity.py import'

Exit codes: 0 = identical / imports, 1 = drift or an unimportable CLI, 2 = usage.
Non-zero is meant to fail a deploy.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

#: Only source files are compared. Caches and compiled artefacts differ harmlessly
#: between machines and would drown the signal.
SUFFIXES = (".py",)
SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}

#: Importing this walks the whole Typer command tree, so it exercises every re-export
#: barrel the CLI depends on — which is precisely where the lxp breakage lived.
IMPORT_TARGET = "examlops.cli.main"


def _digest(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 — drift detection, not a security boundary
    h.update(path.read_bytes())
    return h.hexdigest()


def build_manifest(root: Path) -> dict[str, str]:
    """Map every source file under *root* to its digest, keyed by relative path."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in SUFFIXES:
            continue
        if SKIP_DIRS & set(p.relative_to(root).parts):
            continue
        out[str(p.relative_to(root))] = _digest(p)
    return out


def parse_manifest(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rel = line.partition("  ")
        if rel:
            out[rel] = digest
    return out


def render_manifest(m: dict[str, str]) -> str:
    return "".join(f"{d}  {rel}\n" for rel, d in sorted(m.items()))


def compare(
    expected: dict[str, str], actual: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Return (missing, differing, extra) — extra is reported but never fatal."""
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    differing = sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k])
    return missing, differing, extra


def import_check(target: str = IMPORT_TARGET) -> str | None:
    """Import *target* in a fresh interpreter; return None on success, else the error.

    A subprocess, not a plain ``import``: this script can be run from inside the very tree
    being checked, and a module already resident in *this* process would mask the breakage.
    """
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", f"import {target}"], capture_output=True, text=True
    )
    if proc.returncode == 0:
        return None
    return (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    mode = argv[1]

    if mode == "manifest":
        if len(argv) != 3:
            print("usage: verify_deploy_integrity.py manifest <dir>", file=sys.stderr)
            return 2
        sys.stdout.write(render_manifest(build_manifest(Path(argv[2]))))
        return 0

    if mode == "verify":
        if len(argv) != 4:
            print("usage: verify_deploy_integrity.py verify <dir> <manifest|->", file=sys.stderr)
            return 2
        target = Path(argv[2])
        if not target.is_dir():
            print(f"FAIL  target directory does not exist: {target}", file=sys.stderr)
            return 1
        text = sys.stdin.read() if argv[3] == "-" else Path(argv[3]).read_text()
        expected = parse_manifest(text)
        if not expected:
            print("FAIL  manifest is empty — refusing to call that a match", file=sys.stderr)
            return 1
        missing, differing, extra = compare(expected, build_manifest(target))
        for rel in missing:
            print(f"MISSING    {rel}")
        for rel in differing:
            print(f"DIFFERENT  {rel}")
        for rel in extra:
            print(f"extra      {rel}")
        n = len(missing) + len(differing)
        if n:
            print(
                f"\nFAIL  {n} file(s) drifted out of {len(expected)} checked "
                f"({len(missing)} missing, {len(differing)} different, {len(extra)} extra — "
                f"extra files are reported, not counted).",
                file=sys.stderr,
            )
            return 1
        print(f"OK  {len(expected)} files identical ({len(extra)} extra, not counted).")
        return 0

    if mode == "import":
        if len(argv) not in (2, 3):
            print("usage: verify_deploy_integrity.py import [module]", file=sys.stderr)
            return 2
        target = argv[2] if len(argv) == 3 else IMPORT_TARGET
        error = import_check(target)
        if error:
            print(f"FAIL  {target!r} does not import — the CLI cannot start:", file=sys.stderr)
            for line in error.splitlines():
                print(f"      {line}", file=sys.stderr)
            return 1
        print(f"OK  {target!r} imports — the CLI can start.")
        return 0

    print(f"unknown mode: {mode!r} (expected 'manifest', 'verify' or 'import')", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
