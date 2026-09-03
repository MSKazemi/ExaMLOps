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

from tests.unit._guard_deps import require_binary

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"

# Prose false positives: "make it clear", "make a copy", "make the call".
_PROSE = {
    "a",
    "active",
    "an",
    "every",  # "would make every downstream model stale" — `make <verb-object>` is ordinary English
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
    require_binary("make", "`make help` lists every target the Makefile defines")
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
# Repository policy names this as an invariant that "must agree". When the scopes
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


def test_typecheck_reads_every_python_source_root():
    """`make typecheck` ran mypy over three roots and silently skipped the largest.

    `platform/cli/src/` is the `examlops` package — 243 source files, the CLI, the data layer,
    the gate — and it was in the lint scope, in the test scope, and in neither typecheck
    invocation. A gate whose name says "type check" and whose recipe reads two thirds of the
    tree reports green over code it never opened; that is how a pre-existing `arg-type` error
    in `autopilot_cmd` sat in the tree with the gate passing. Ratcheted rather than clean is
    fine here — unread is not.
    """
    lines = MAKEFILE.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("typecheck:"))
    body = [lines[start]]
    for ln in lines[start + 1 :]:
        if ln and not ln.startswith(("\t", " ", "#")):
            break
        body.append(ln)
    recipe = "\n".join(body)
    roots = [
        "pipelines/",
        "serving/",
        "platform/services/",
        "platform/cli/src/",
    ]
    missing = [r for r in roots if r not in recipe]
    assert not missing, f"make typecheck never reads {missing}"


def test_every_place_that_runs_mypy_also_checks_the_cli_package():
    """The mypy invocation is written in three places, and all three skipped the same package.

    `Makefile:typecheck`, `Makefile:ci-examlops` and `.gitlab-ci.yml`'s examlops job each ran
    `mypy pipelines/ serving/ platform/services/`. Widening only the local target would have
    left CI checking a narrower tree than the laptop — the same one-directional silence the lint
    scope guard above exists to prevent. `platform/cli/src/` is ratcheted, so it is reached
    through the `typecheck-cli` target rather than added to those lines; what this asserts is
    that nowhere runs the strict line *without* it.
    """
    places = {
        "Makefile": MAKEFILE,
        ".gitlab-ci.yml": ROOT / ".gitlab-ci.yml",
    }
    strict = re.compile(r"mypy pipelines/ serving/ platform/services/")
    for label, path in places.items():
        text = path.read_text()
        runs = len(strict.findall(text))
        assert runs, f"no strict mypy invocation found in {label} — the scan is broken"
        assert text.count("typecheck-cli") >= runs, (
            f"{label} runs mypy {runs}× but reaches the ratcheted platform/cli/src/ check "
            f"{text.count('typecheck-cli')}×, so one gate reads less of the tree than another"
        )


def test_no_recipe_captures_exit_status_in_a_way_the_shell_flags_defeat():
    """`SHELL := /bin/bash -euo pipefail` makes the `cmd; rc=$?` idiom unreachable.

    Under `-e` the shell exits on the failing command, so the line that reads `$?` never
    runs — and neither does anything after it. `make test-postgres` was written that way:
    a failure in the first of three suites skipped the other two, never computed the summed
    exit code, and never reached the `docker rm -f` on the last line, leaving a throwaway
    Postgres running (one had been up four hours when this was found). The failure is quiet
    because make still reports an error — it just silently runs a third of the work.

    The safe forms are `cmd || rc=$?` and a `trap` for cleanup.
    """
    text = MAKEFILE.read_text()
    assert "-e" in re.search(r"^SHELL\s*:?=(.*)$", text, re.M).group(1), (
        "this guard assumes an erroring shell; if SHELL no longer carries -e, revisit it"
    )
    # a capture of $? that is NOT guarded by `||` on the same command
    offenders = []
    for match in re.finditer(r"^\s*(\w+)=\$\$\?", text, re.M):
        line_no = text[: match.start()].count("\n") + 1
        previous = text.splitlines()[line_no - 2] if line_no > 1 else ""
        if "||" not in previous and not previous.lstrip().startswith("#"):
            offenders.append(f"line {line_no}: {match.group(0).strip()} after {previous.strip()!r}")
    assert not offenders, (
        "these capture $? on a line the erroring shell can never reach; use `cmd || rc=$?`:\n"
        + "\n".join(offenders)
    )
