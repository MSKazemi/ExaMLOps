"""ADR 0038 clause 2 — *rebuild* the recorded environment, rather than only compare it.

``exa reproduce run --execute`` used to check the bundle's lockfile hash and diff its recorded
package set against whatever interpreter happened to be running. That answers "is this machine
still the machine?", not "put the recorded environment back". This module answers the second
question: it materialises the bundle's package set into a **fresh, isolated virtualenv** with
``uv venv`` + ``uv pip install``, and the rebuilt interpreter is what the training subprocess
then runs.

Rules the implementation keeps (R4 — honesty over a green step):

* **Never mutate the caller's environment.** The venv is created under a directory the caller
  owns (a throw-away one by default); nothing is installed into the running interpreter, and
  the repository's shared ``.venv`` is never touched.
* **Never substitute a different environment and call it reproduced.** If a recorded package
  cannot be resolved, the rebuild fails and names the packages — the run does not continue in
  a partially-built venv. A *major.minor* Python difference is reported as a mismatch for the
  caller to fail on; a patch-level difference is reported in the detail and not hidden.
* **Say which interpreter ran.** :attr:`RebuildResult.python` is the absolute path of the
  rebuilt interpreter and is surfaced by the ``env`` step, the CLI and ``--json``.

The recorded set is a full ``pip freeze`` — an already-closed dependency set — so the install
runs ``--no-deps``: exactly the recorded distributions, at the recorded versions, and nothing
else. Workspace-local distributions (:data:`LOCAL_DISTRIBUTIONS`) are not on any index; they
are skipped here and reached through the checked-out worktree, which ``execute`` already puts
on ``PYTHONPATH``. That is reported, not silent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: Distributions that live in this repository's own uv workspace (plus the upstream model
#: library). They are not published to an index, so a rebuild cannot install them; the
#: checked-out worktree supplies them instead.
LOCAL_DISTRIBUTIONS = frozenset(
    {
        "examlops",
        "examlops-pipelines",
        "examlops-serving",
        "examlops-workspace",
        "seanergys-modelzoo",
    }
)

#: Bound on the per-package probe used only when uv's error text names nothing recognisable.
DEFAULT_PROBE_LIMIT = 100

Runner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]


@dataclass
class RebuildResult:
    """Outcome of materialising a recorded package set into a fresh venv."""

    ok: bool
    detail: str
    venv: str | None = None
    python: str | None = None
    installed: int = 0
    requested: int = 0
    skipped_local: list[str] = field(default_factory=list)
    unsatisfied: list[str] = field(default_factory=list)
    python_recorded: str | None = None
    python_actual: str | None = None
    #: True when the rebuilt interpreter's *major.minor* differs from the recorded one.
    python_mismatch: bool = False


def _run(args: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _minor(version: str | None) -> str | None:
    if not version:
        return None
    parts = str(version).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else None


def unsatisfied_from_output(text: str, recorded: dict[str, str]) -> list[str]:
    """Recorded distributions named as a requirement in uv's resolver output.

    uv wraps its diagnostics at the terminal width, so ``pkg==1.2.3`` can arrive split across
    lines; whitespace is collapsed before matching. Only names that are actually in the
    recorded set are reported, so an unrelated token in the message cannot invent a package.
    """
    flat = re.sub(r"\s+", " ", text or "")
    found = {
        _canonical(m.group(1))
        for m in re.finditer(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==", flat)
        if _canonical(m.group(1)) in recorded
    }
    return sorted(found)


def _probe_each(
    uv: str,
    python: Path,
    reqs: list[tuple[str, str]],
    extra: list[str],
    *,
    limit: int,
    timeout: int,
    runner: Runner,
) -> tuple[list[str], bool]:
    """Resolve the recorded requirements one at a time to name the ones that fail.

    Only reached when uv's bulk error text named nothing recognisable. ``--dry-run`` keeps this
    to resolution, not download. Returns ``(unsatisfied, complete)``; ``complete`` is False when
    the probe stopped at ``limit``.
    """
    bad: list[str] = []
    probed = reqs[:limit]
    for name, version in probed:
        done = runner(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "--dry-run",
                *extra,
                f"{name}=={version}",
            ],
            timeout,
        )
        if done.returncode != 0:
            bad.append(name)
    return bad, len(probed) == len(reqs)


def rebuild_environment(
    packages: dict[str, str],
    dest: str | Path,
    *,
    recorded_python: str | None = None,
    extra_args: Sequence[str] | None = None,
    timeout: int = 1800,
    probe_limit: int = DEFAULT_PROBE_LIMIT,
    runner: Runner | None = None,
    uv_bin: str | None = None,
) -> RebuildResult:
    """Materialise ``packages`` into a new venv at ``dest`` and return what actually happened.

    ``dest`` must not already exist as a populated environment the caller cares about — it is
    created by ``uv venv`` and is expected to be throw-away. ``extra_args`` is appended to the
    ``uv pip install`` invocation (the test suite uses ``--no-index --find-links`` to rebuild
    offline); it is deliberately not reachable from the CLI.
    """
    run = runner or _run
    uv = uv_bin or shutil.which("uv")
    dest = Path(dest)
    extra = list(extra_args or ())
    requested = dict(sorted(packages.items())) if packages else {}
    skipped = sorted(k for k in requested if _canonical(k) in LOCAL_DISTRIBUTIONS)
    wanted = {k: v for k, v in requested.items() if _canonical(k) not in LOCAL_DISTRIBUTIONS}

    if not requested:
        return RebuildResult(
            False, "bundle captured no package set — there is nothing to rebuild", requested=0
        )
    if not uv:
        return RebuildResult(
            False,
            "uv is not on PATH — cannot rebuild the recorded environment (refusing to "
            "reproduce in the caller's environment instead)",
            requested=len(requested),
            skipped_local=skipped,
        )
    if not wanted:
        return RebuildResult(
            False,
            f"every recorded distribution is workspace-local ({', '.join(skipped)}) — "
            "there is no third-party environment to rebuild",
            requested=len(requested),
            skipped_local=skipped,
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    minor = _minor(recorded_python)
    created = None
    if minor:
        created = run([uv, "venv", "--python", minor, "--no-python-downloads", str(dest)], timeout)
    if created is None or created.returncode != 0:
        created = run([uv, "venv", "--python", sys.executable, str(dest)], timeout)
    if created.returncode != 0:
        return RebuildResult(
            False,
            f"uv venv failed: {(created.stderr or created.stdout).strip()[:300]}",
            requested=len(requested),
            skipped_local=skipped,
            python_recorded=recorded_python,
        )

    python = (
        dest
        / ("Scripts" if os.name == "nt" else "bin")
        / ("python.exe" if os.name == "nt" else "python")
    )
    got = run([str(python), "-c", "import sys; print(sys.version.split()[0])"], 120)
    actual = got.stdout.strip() or None
    mismatch = bool(minor and _minor(actual) and _minor(actual) != minor)

    req_file = dest.parent / "requirements-rebuild.txt"
    req_file.write_text("".join(f"{k}=={v}\n" for k, v in wanted.items()), encoding="utf-8")
    installed = run(
        [uv, "pip", "install", "--python", str(python), "--no-deps", *extra, "-r", str(req_file)],
        timeout,
    )
    if installed.returncode != 0:
        text = (installed.stderr or "") + "\n" + (installed.stdout or "")
        bad = unsatisfied_from_output(text, wanted)
        note = ""
        if not bad:
            bad, complete = _probe_each(
                uv,
                python,
                sorted(wanted.items()),
                extra,
                limit=probe_limit,
                timeout=timeout,
                runner=run,
            )
            if not complete:
                note = f" (first {probe_limit} of {len(wanted)} recorded packages probed)"
        named = ", ".join(bad) if bad else "uv named none: " + text.strip()[:200]
        return RebuildResult(
            False,
            f"{len(bad)} recorded package(s) could not be resolved: {named}{note}",
            venv=str(dest),
            python=str(python),
            requested=len(requested),
            skipped_local=skipped,
            unsatisfied=bad,
            python_recorded=recorded_python,
            python_actual=actual,
            python_mismatch=mismatch,
        )

    listed = run([uv, "pip", "list", "--python", str(python), "--format", "json"], timeout)
    try:
        count = len(json.loads(listed.stdout or "[]"))
    except ValueError:
        count = 0
    bits = [f"rebuilt {len(wanted)} recorded package(s) into {dest}", f"python {actual or '?'}"]
    if skipped:
        bits.append(f"{len(skipped)} workspace-local from the checkout ({', '.join(skipped)})")
    if mismatch:
        bits.append(f"PYTHON MISMATCH: recorded {recorded_python}, rebuilt {actual}")
    elif recorded_python and actual and actual != recorded_python:
        bits.append(f"patch differs: recorded {recorded_python}")
    return RebuildResult(
        True,
        "; ".join(bits),
        venv=str(dest),
        python=str(python),
        installed=count,
        requested=len(requested),
        skipped_local=skipped,
        python_recorded=recorded_python,
        python_actual=actual,
        python_mismatch=mismatch,
    )


__all__ = [
    "DEFAULT_PROBE_LIMIT",
    "LOCAL_DISTRIBUTIONS",
    "RebuildResult",
    "rebuild_environment",
    "unsatisfied_from_output",
]
