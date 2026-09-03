#!/usr/bin/env python3
"""Reconcile the ADR record against the code that is actually shipped.

An ADR is a claim about the system. `- **Status:** Accepted` says the claim is now
true, and the body names the artifacts that make it true: `exa` commands, `examlops.*`
modules, file paths. Nothing checked that those artifacts exist, so the record could
drift from the product in either direction — an Accepted ADR pointing at a module
nobody wrote, or fifty Proposed ADRs describing features that shipped a year ago.

This script reads every ADR, extracts the artifacts it names, and checks each one
against the live CLI tree and the working tree. It answers, repeatably, the only
question that matters about a design record: *is it still true?*

Usage:
    python3 platform/ci/adr_reconcile.py            # human summary
    python3 platform/ci/adr_reconcile.py --json     # machine-readable
    python3 platform/ci/adr_reconcile.py --check    # exit 1 if an Accepted ADR lies
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = ROOT / "design" / "adr"

# `build`/`dist` hold *stale copies* of the package, and the matcher takes the first file whose
# path matches — so a token like `examlops.slo.record_sample` could resolve against a build
# artifact months out of date. That cuts both ways and the second way is worse: a newly added
# symbol reads as **absent** (noise, which is how a guard gets switched off), and a symbol deleted
# from source but still present in the artifact reads as **present**, blinding the Accepted-ADR
# check that gates the build. 242 of 1235 scanned files came from there before this line.
SKIP_DIRS = {
    ".git",
    ".git-private",
    ".venv",
    "node_modules",
    "site",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "htmlcov",
}


def source_files() -> list[Path]:
    """Every source file in the working tree, once.

    An ADR writes the path a reader would recognise (`skipper/memory.py`,
    `finops/carbon.py`) rather than the one an importer needs, so the honest
    comparison is a suffix match against the real tree, not a guess at which of
    six package roots the author had in mind.
    """
    out: list[Path] = []
    stack = [ROOT]
    while stack:
        d = stack.pop()
        for entry in d.iterdir():
            if entry.name in SKIP_DIRS:
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.suffix in {".py", ".yml", ".yaml", ".toml", ".json"}:
                out.append(entry)
    return out


FILES = source_files()
FILE_STRS = [str(f.relative_to(ROOT)) for f in FILES]

# A backticked token has to look like an artifact before we hold anyone to it.
CLI_RE = re.compile(r"^exa(\s+[a-z][\w-]*)+")
MODULE_RE = re.compile(r"^examlops(\.[a-z_0-9]+)+$")
PATH_RE = re.compile(r"^[\w./-]+\.(py|yml|yaml|toml|json)$")


# Every command node the CLI reports, keyed by its full path — filled by ``cli_commands()``.
# The flag check reads it instead of importing the CLI into *this* interpreter, because
# that import is what used to decide the answer: run under a python without ``examlops``
# on the path, every flag check quietly became a no-op and the report said 44 where the
# venv said 43. One source of truth, same number from any interpreter.
_CLI_NODES: dict[str, dict] = {}


def cli_commands() -> set[str]:
    """Every command the installed CLI actually exposes, from the CLI itself."""
    exa = ROOT / ".venv" / "bin" / "exa"
    if not exa.exists():
        return set()
    out = subprocess.run(
        [str(exa), "--json", "docs"], capture_output=True, text=True, cwd=ROOT, timeout=300
    )
    if out.returncode != 0:
        return set()
    names: set[str] = set()
    _CLI_NODES.clear()

    def walk(node: dict) -> None:
        names.add(node["name"])
        _CLI_NODES[node["name"]] = node
        for child in node.get("subcommands") or []:
            walk(child)

    walk(json.loads(out.stdout))
    return names


# A token that elides part of itself is prose, not an artifact. ADR 0077 writes
# ``exa finops … providers`` while explaining that finops *has* provider listing, and ADR
# 0092 writes ``modelzoo/.../datasets/_backends.py`` to point at a directory without
# spelling the package out. Checking those literally reports a missing artifact for a
# sentence that never promised one, and false "absent" rows are how a guard earns a
# reputation for noise and gets switched off.
_ELIDED = ("…", "...")


def artifact_exists(token: str, cmds: set[str]) -> bool | None:
    """True / False if the token is checkable, None if it is not an artifact."""
    token = token.strip()
    if any(mark in token for mark in _ELIDED):
        return None
    if CLI_RE.match(token):
        # `exa models sign/verify/bom` and `exa workbench create|list` name a group
        # plus a menu of leaves; the group is the part every form agrees on.
        base = re.split(r"\s+(?:--|<|\[)", token)[0]
        base = re.split(r"[/|]", base)[0].strip()
        if not cmds:
            return None
        if base not in cmds:
            return False
        # An ADR that names a *flag* is claiming the flag, not just the command. Checking
        # only the command path counted `exa pipeline run --distributed` as present when no
        # such option exists — the record read as kept because the verb happened to be real.
        flags = [f.strip("[]<>") for f in re.findall(r"(?:^|\s|\[)(--[a-z][\w-]*)", token)]
        if flags:
            known = _command_options(base)
            if known is not None:
                # Preference order, strongest evidence first: a literal declaration, an
                # instance of a declared pattern (`--if-rmse-lt` against
                # `--if-<metric>-<op>`), then the command's own help text. The last is the
                # weakest — prose can outlive the flag it describes — so it is the fallback,
                # not the first thing consulted.
                missing = [
                    f
                    for f in flags
                    if f not in known
                    and not _matches_declared_pattern(base, f)
                    and not _documents_flag(base, f)
                ]
                if missing:
                    return False
        return True
    if MODULE_RE.match(token):
        # `examlops.hpc_placement.choose_cluster` is a module plus a symbol. Peel
        # the tail off until the head resolves to a file, then require the tail to
        # appear in it — otherwise a renamed function reads as a present module.
        parts = token.split(".")
        for cut in range(len(parts), 1, -1):
            rel = "/".join(parts[:cut])
            hit = _find_module(rel)
            if hit is None:
                continue
            tail = parts[cut:]
            if not tail:
                return True
            return all(re.search(rf"\b{re.escape(t)}\b", hit.read_text()) for t in tail)
        # `examlops.cli_plugins` is an entry-point *group name*, not a module — it is
        # real, and it is spelled exactly like a module. If the tree quotes the string,
        # the ADR is naming something that exists.
        return any(
            token in f.read_text(errors="ignore") for f in FILES if f.suffix in {".py", ".toml"}
        )
    if PATH_RE.match(token) and "/" in token:
        return any(s == token or s.endswith("/" + token) for s in FILE_STRS)
    return None


def _command_options(path: str) -> set[str] | None:
    """Every flag ``exa <path>`` accepts, or None when the question cannot be answered.

    Reads the tree ``cli_commands()`` already fetched from the installed ``exa`` rather
    than importing the CLI here, so the answer does not depend on which interpreter ran
    this file. A flag counts as accepted if the command **declares** it or its own help
    **documents** it: ``exa pipeline promote`` parses the whole ``--if-<metric>-<op>``
    family in its body and declares none of them, and an options-only check would call
    that real, documented flag a missing artifact.
    """
    node = _CLI_NODES.get(path)
    if node is None:
        return None
    declared: set[str] = set()
    for opt in node.get("options") or []:
        for form in str(opt.get("opts", "")).split(","):
            form = form.strip()
            if form.startswith("-"):
                declared.add(form)
    return declared | {"--help"}


def _matches_declared_pattern(path: str, flag: str) -> bool:
    """True if ``flag`` is an instance of a *pattern* the command declares.

    A command that parses its own flags declares them as a shape rather than a list —
    ``exa pipeline promote`` publishes ``--if-<metric>-<op> <value>`` because the metric is
    whatever the model logged to MLflow. Expanding the placeholders is what lets a concrete
    ``--if-rmse-lt`` be recognised from a real declaration instead of from prose in the help
    text, which is a weaker thing to trust.
    """
    node = _CLI_NODES.get(path) or {}
    for opt in node.get("options") or []:
        pattern = str(opt.get("opts", "")).split(",")[0].strip().split(" ")[0]
        if "<" not in pattern:
            continue
        # Build the regex from the pattern's literal parts so nothing in a flag name is
        # interpreted as regex syntax; each <placeholder> becomes one non-empty segment.
        parts = re.split(r"<[^>]*>", pattern)
        rx = "^" + "[^\\s-]+".join(re.escape(part) for part in parts) + "$"
        if re.match(rx, flag):
            return True
    return False


def _documents_flag(path: str, flag: str) -> bool:
    """True when ``exa <path> --help`` itself names the flag in prose."""
    node = _CLI_NODES.get(path) or {}
    return flag in f"{node.get('help', '') or ''}"


def _find_module(rel: str) -> Path | None:
    for cand in (f"{rel}.py", f"{rel}/__init__.py"):
        for f, s in zip(FILES, FILE_STRS):
            if s == cand or s.endswith("/" + cand):
                return f
    return None


# An ADR argues by listing what it did *not* choose. `exa agent watch` appears in
# ADR 0104 only as a rejected alternative, so its absence is the decision working, not
# a defect. Everything from the alternatives heading to the next heading is argument,
# not commitment — 110 of the 113 ADRs have such a section, so this is not an edge case.
ALT_HEADING = re.compile(r"^#{1,6}\s.*\b(alternatives|rejected|options considered)\b", re.I)
NEXT_HEADING = re.compile(r"^#{1,6}\s")


def commitments(text: str) -> str:
    """The part of an ADR that claims something about the built system."""
    kept, skipping = [], False
    for line in text.splitlines():
        if ALT_HEADING.match(line):
            skipping = True
            continue
        if skipping and NEXT_HEADING.match(line):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


# --- dating -----------------------------------------------------------------
#
# The second list this script prints — ADRs that are not Accepted but whose named
# artifacts all exist — is easy to misread as "44 features shipped, go flip the status".
# It is not. `exa serve` existing does not make ADR 0015 (KServe-native serving) true, and
# ADR 0112 ("carbon signals are typed", written 2026-08-19) names `finops/carbon.py`, which
# had existed since 2026-07-01. An artifact that predates the decision cannot be evidence
# that the decision was carried out.
#
# So date both sides. An artifact that first appeared *after* the ADR is consistent with
# the ADR having driven it; one that predates the ADR tells you nothing and the ADR needs a
# human read. This is a coarse signal on purpose — for a CLI command it is the first commit
# touching that word anywhere in the CLI source — and it is used only to *sort* the work,
# never to flip a status on its own.

_DATE_CACHE: dict[str, str] = {}


def _git(args: list[str]) -> str:
    for git_dir in (".git-private", ".git"):
        if not (ROOT / git_dir).exists():
            continue
        out = subprocess.run(
            ["git", f"--git-dir={ROOT / git_dir}", *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=120,
        )
        lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
        if lines:
            return lines[-1].strip()  # oldest
    return ""


def first_seen(token: str, cmds: set[str]) -> str:
    """Earliest date this artifact shows up in history, or '' if undatable."""
    if token in _DATE_CACHE:
        return _DATE_CACHE[token]
    if CLI_RE.match(token):
        leaf = re.split(r"[/|]", re.split(r"\s+(?:--|<|\[)", token)[0].strip())[0].split()[-1]
        # Quote the leaf: Typer registers a command as `@app.command("vector")`, so the
        # quoted form dates the *command*, while the bare word also matches every dict key
        # and docstring that happens to contain it. Measured difference on `lineage`:
        # 2026-05-29 bare (an unrelated mention) vs 2026-07-16 quoted (the command itself).
        date = _git(
            ["log", f'-S"{leaf}"', "--format=%ad", "--date=short", "--", "platform/cli/src"]
        )
    else:
        target = None
        if MODULE_RE.match(token):
            hit = _find_module(token.replace(".", "/"))
            target = str(hit.relative_to(ROOT)) if hit else None
            if target is None:
                for cut in range(len(token.split(".")), 1, -1):
                    hit = _find_module("/".join(token.split(".")[:cut]))
                    if hit:
                        target = str(hit.relative_to(ROOT))
                        break
        else:
            for cand in FILE_STRS:
                if cand == token or cand.endswith("/" + token):
                    target = cand
                    break
        date = _git(["log", "--format=%ad", "--date=short", "--", target]) if target else ""
    _DATE_CACHE[token] = date
    return date


def status_of(text: str) -> str:
    """The ADR's status line, with markdown emphasis stripped.

    Stripping matters: ``- **Status:** **Accepted**`` is how a human writes emphasis, and
    with the markers left in, ``head.startswith("accepted")`` is False — the ADR silently
    escapes the Accepted guard. A record-keeping guard that can be switched off by bolding
    a word is not a guard.
    """
    for line in text.splitlines()[:8]:
        m = re.match(r"\s*-?\s*\*\*Status:?\*\*:?\s*(.+)", line)
        if m:
            return re.sub(r"\*+|__", "", m.group(1)).strip()
    return ""


def swept_of(text: str) -> str:
    """The ADR's ``**Reconciliation:**`` note, if it carries one.

    A not-accepted ADR whose named artifacts all predate it is a **name match**, not evidence:
    ADR 0015 "matches" because ``exa serve`` has existed since 2026-05-21, which says nothing
    about KServe. Sweeping those once and recording the sweep in the ADR itself is what keeps
    "still to decide" a number that moves — without it the report counts the same 26 ADRs
    forever and the count stops meaning anything.
    """
    for line in text.splitlines()[:12]:
        m = re.match(r"\s*-?\s*\*\*Reconciliation:?\*\*:?\s*(.+)", line)
        if m:
            return re.sub(r"\*+|__", "", m.group(1)).strip()
    return ""


def reconcile(cmds: set[str]) -> list[dict]:
    rows = []
    for path in sorted(ADR_DIR.glob("*.md")):
        text = path.read_text()
        status = status_of(text)
        swept = swept_of(text)
        text = commitments(text)
        head = re.split(r"\s*[—(-]\s*", status)[0].strip().rstrip(".").lower() or "(none)"
        present: list[str] = []
        absent: list[str] = []
        for token in sorted(set(re.findall(r"`([^`\n]{2,80})`", text))):
            verdict = artifact_exists(token, cmds)
            if verdict is True:
                present.append(token)
            elif verdict is False:
                absent.append(token)
        rows.append(
            {
                "adr": path.name,
                "status": head,
                "accepted": head.startswith("accepted"),
                "swept": swept,
                "present": present,
                "absent": absent,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--check", action="store_true", help="exit 1 if an Accepted ADR names a missing artifact"
    )
    ap.add_argument(
        "--dates",
        action="store_true",
        help="split the not-accepted list by whether its artifacts predate the ADR",
    )
    args = ap.parse_args()

    cmds = cli_commands()
    rows = reconcile(cmds)
    if args.json:
        json.dump({"cli_commands": len(cmds), "adrs": rows}, sys.stdout, indent=1)
        print()
        return 0

    accepted = [r for r in rows if r["accepted"]]
    proposed = [r for r in rows if not r["accepted"]]
    lying = [r for r in accepted if r["absent"]]
    shipped_but_proposed = [r for r in proposed if r["present"] and not r["absent"]]

    print(f"ADRs: {len(rows)}  ({len(accepted)} accepted, {len(proposed)} not)")
    print(f"CLI commands consulted: {len(cmds)}")
    print()
    print(f"Accepted ADRs naming an artifact that does NOT exist: {len(lying)}")
    for r in lying:
        print(f"  {r['adr']}")
        for a in r["absent"]:
            print(f"      missing: {a}")
    print()
    # "Partially implemented" is a decision already taken with evidence, not a stale status.
    # Counting those as still-to-decide makes the remaining work look bigger than it is, which
    # is the failure mode this whole report exists to avoid.
    decided = [r for r in shipped_but_proposed if r["status"].startswith("partially implemented")]
    # A recorded sweep is also a decision already taken: the artifacts predate the ADR, so the
    # match proves nothing and the status stays. Leaving those in the queue is what made this
    # number immovable.
    swept = [r for r in shipped_but_proposed if r not in decided and r["swept"]]
    undecided = [r for r in shipped_but_proposed if r not in decided and r not in swept]

    print(f"Not-accepted ADRs whose named artifacts ALL exist: {len(shipped_but_proposed)}")
    print(f"  of which already decided as 'Partially implemented': {len(decided)}")
    print(f"  of which swept as name-match-only (artifacts predate the ADR): {len(swept)}")
    print(f"  still to decide one at a time: {len(undecided)}")
    for r in undecided:
        print(f"  {r['adr']:<58} {', '.join(r['present'][:3])}")

    if args.dates:
        print()
        print("Dated (an artifact older than its ADR is not evidence the ADR was carried out):")
        drove, predates = [], []
        for r in undecided:
            adr_date = _git(["log", "--format=%ad", "--date=short", "--", f"design/adr/{r['adr']}"])
            dates = {a: first_seen(a, cmds) for a in r["present"]}
            after = {a: d for a, d in dates.items() if d and adr_date and d > adr_date}
            (drove if after else predates).append((r["adr"], adr_date, dates, after))
        print()
        print(f"  Artifacts appeared AFTER the ADR — worth reading first ({len(drove)}):")
        for adr, adr_date, _dates, after in drove:
            newest = ", ".join(f"{a} ({d})" for a, d in sorted(after.items())[:2])
            print(f"    {adr:<58} adr {adr_date} → {newest}")
        print()
        print(f"  Every artifact PREDATES the ADR — name match only ({len(predates)}):")
        for adr, adr_date, dates, _ in predates:
            oldest = ", ".join(f"{a} ({d or '?'})" for a, d in sorted(dates.items())[:2])
            print(f"    {adr:<58} adr {adr_date} → {oldest}")

    if args.check and lying:
        print()
        print(f"FAIL: {len(lying)} accepted ADR(s) name artifacts that do not exist.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
