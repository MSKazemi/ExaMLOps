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

SKIP_DIRS = {".git", ".git-private", ".venv", "node_modules", "site", "__pycache__", ".mypy_cache"}


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

    def walk(node: dict) -> None:
        names.add(node["name"])
        for child in node.get("subcommands") or []:
            walk(child)

    walk(json.loads(out.stdout))
    return names


def artifact_exists(token: str, cmds: set[str]) -> bool | None:
    """True / False if the token is checkable, None if it is not an artifact."""
    token = token.strip()
    if CLI_RE.match(token):
        # `exa models sign/verify/bom` and `exa workbench create|list` name a group
        # plus a menu of leaves; the group is the part every form agrees on.
        base = re.split(r"\s+(?:--|<|\[)", token)[0]
        base = re.split(r"[/|]", base)[0].strip()
        return base in cmds if cmds else None
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


def status_of(text: str) -> str:
    for line in text.splitlines()[:8]:
        m = re.match(r"\s*-?\s*\*\*Status:?\*\*:?\s*(.+)", line)
        if m:
            return m.group(1).strip()
    return ""


def reconcile(cmds: set[str]) -> list[dict]:
    rows = []
    for path in sorted(ADR_DIR.glob("*.md")):
        text = path.read_text()
        status = status_of(text)
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
    print(f"Not-accepted ADRs whose named artifacts ALL exist: {len(shipped_but_proposed)}")
    for r in shipped_but_proposed:
        print(f"  {r['adr']:<58} {', '.join(r['present'][:3])}")

    if args.check and lying:
        print()
        print(f"FAIL: {len(lying)} accepted ADR(s) name artifacts that do not exist.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
