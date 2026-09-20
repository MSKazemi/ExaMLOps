#!/usr/bin/env python3
"""Generate the documentation site's capability atlas from the live `exa` command tree.

The "Every capability" page of the docs site lists every `exa` leaf command, grouped into the
same twelve lifecycle panels `exa --help` shows. Hand-maintaining that list is how it would
drift: 401 commands move too often for anyone to notice one missing. So the list is generated:

  * the command tree comes from `exa --json docs` — the same introspection `exa docs` publishes;
  * panel membership comes from `_ROOT_PANELS` in `examlops/cli/main.py`, read with `ast` rather
    than imported, so this script has no import side effects and no dependency on the CLI's deps;
  * each command links to its section of `docs/reference/cli-commands-guide.md` when that guide
    has a heading for it (exact anchor, computed with the same slugifier MkDocs uses), otherwise
    to the nearest ancestor's heading.

Run:   python3 platform/ci/gen_capability_atlas.py            # rewrite the JSON
       python3 platform/ci/gen_capability_atlas.py --check    # exit 1 if the JSON is stale
`tests/unit/test_docs_capability_atlas.py` runs --check, so a new command that is not in the
atlas fails the build instead of quietly missing from the site.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "platform" / "cli" / "src" / "examlops" / "cli" / "main.py"
GUIDE = ROOT / "docs" / "reference" / "cli-commands-guide.md"
OUT = ROOT / "docs" / "assets" / "explore" / "data" / "capabilities.json"
PAGE = ROOT / "docs" / "explore" / "capabilities.md"

# One plain sentence per panel: what a reader can do there. Keyed by the panel title in
# `_ROOT_PANELS`; a panel missing from this map fails the run, so a new panel cannot ship blank.
PANEL_BLURBS = {
    "Getting Started": "Check what is running, diagnose your setup and learn the CLI.",
    "Training & Pipelines": "Run, schedule, promote and reproduce training pipelines.",
    "Data & Features": "Version datasets, validate them and serve features without skew.",
    "Models & Registry": "Browse, compare, sign and trace every registered model version.",
    "Serving & Inference": "Serve models, split traffic, call gateways and query RAG.",
    "GenAI & LLMOps": "Manage prompts, trace GenAI calls and enforce guardrails.",
    "Agents & Automation": "Ask Skipper, run the autopilot loop and expose tools over MCP.",
    "Monitoring & Quality": "Watch drift, evaluate models, track SLOs and fairness.",
    "HPC, Fleet & FinOps": "Discover clusters, place jobs and account for cost and carbon.",
    "Governance & Security": "Approve changes, audit actions, manage secrets and policy.",
    "Projects & Workspaces": "Group models, people, storage and connections into projects.",
    "Platform & Integrations": "Operate the stack, back it up and connect external systems.",
}

# The guide page a top-level group is best explained by (in addition to its CLI-guide section).
# Paths are checked for existence at generation time.
GROUP_GUIDES = {
    "status": "guides/quickstart.md",
    "doctor": "guides/quickstart.md",
    "pipeline": "components/prefect.md",
    "retrain": "guides/control-plane.md",
    "scaffold": "guides/add-a-new-model.md",
    "finetune": "guides/fine-tuning.md",
    "reproduce": "guides/reproducibility-bundles.md",
    "data": "guides/data-versioning.md",
    "feature": "guides/feature-store.md",
    "features": "guides/feature-store.md",
    "assets": "guides/asset-pipelines.md",
    "cards": "guides/cards.md",
    "models": "components/mlflow.md",
    "modelzoo": "components/modelzoo.md",
    "embedding": "guides/embedding-lifecycle.md",
    "serve": "components/ray-serve.md",
    "gateway": "guides/model-gateway.md",
    "vector": "guides/vector-store.md",
    "rag": "guides/rag.md",
    "genai": "guides/genai-observability.md",
    "prompt": "guides/prompt-management.md",
    "guardrails": "guides/guardrails.md",
    "ask": "guides/agent.md",
    "chat": "guides/agent.md",
    "agent": "guides/agent.md",
    "agentops": "guides/agentops.md",
    "autopilot": "guides/programmable-mlops.md",
    "drift": "guides/drift-advanced.md",
    "eval": "guides/evaluation.md",
    "slo": "guides/slos.md",
    "fairness": "guides/fairness.md",
    "hpc": "guides/hpc-fleet.md",
    "hardware": "guides/heterogeneous-hardware.md",
    "federated": "guides/federated-training.md",
    "finops": "guides/finops-providers.md",
    "audit": "guides/audit-trail.md",
    "secrets": "guides/secrets.md",
    "compliance": "guides/eu-ai-act-compliance.md",
    "governance": "guides/governance-nist-rmf.md",
    "policy": "guides/policy-as-code.md",
    "providers": "guides/programmable-mlops.md",
    "project": "guides/projects-workspaces.md",
    "connection": "guides/projects-workspaces.md",
    "workbench": "guides/projects-workspaces.md",
    "backup": "guides/backup-restore.md",
    "dataplane-bus": "guides/dataplane-bus.md",
    "approvals": "guides/control-plane.md",
}


def root_panels() -> list[tuple[str, list[str]]]:
    tree = ast.parse(MAIN.read_text())
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            target = t.id if isinstance(t, ast.Name) else None
        if target == "_ROOT_PANELS" and node.value is not None:
            return [(title, list(names)) for title, names in ast.literal_eval(node.value)]
    raise SystemExit(f"_ROOT_PANELS not found in {MAIN}")


def slugify(text: str, sep: str = "-") -> str:
    """The slug python-markdown's toc extension gives a heading (MkDocs' default).

    Re-implemented rather than imported: CI's unit-test job does not install `markdown`
    (only the docs job does), and the guard test must run there. Same algorithm as
    `markdown.extensions.toc.slugify(value, separator)` with unicode=False.
    """
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(rf"[{sep}\s]+", sep, value)


def panel_anchors() -> dict[str, str]:
    """`Training & Pipelines` -> anchor of that area's `##` heading in the CLI command guide."""
    out: dict[str, str] = {}
    for line in GUIDE.read_text().splitlines():
        m = re.match(r"^##\s+([^#`].*)$", line)
        if m:
            out.setdefault(m.group(1).strip(), slugify(m.group(1).strip()))
    return out


def guide_anchors() -> dict[str, str]:
    """`exa serve reload` -> anchor of its heading in the CLI command guide."""
    anchors: dict[str, str] = {}
    for line in GUIDE.read_text().splitlines():
        m = re.match(r"^#{2,5}\s+`(exa[^`]*)`(.*)$", line)
        if m:
            cmd = m.group(1).strip()
            rendered = (cmd + m.group(2)).strip()
            anchors.setdefault(cmd, slugify(rendered))
    return anchors


def first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    text = re.sub(r"\[/?[a-z ]+\]", "", text)  # rich markup
    text = re.sub(r"``([^`]+)``", r"\1", text)  # reStructuredText literals in docstrings
    return text


def leaves(node: dict, acc: list[dict]) -> None:
    for child in node.get("subcommands", []):
        if child.get("subcommands"):
            leaves(child, acc)
        else:
            acc.append(child)


def build() -> dict:
    # Run the Typer app in a child interpreter (so a broken import fails loudly, not silently);
    # fall back to an installed `exa` entry point.
    runner = (
        "import sys; from examlops.cli.main import app; sys.argv = ['exa', '--json', 'docs']; app()"
    )
    raw = subprocess.run([sys.executable, "-c", runner], capture_output=True, text=True, cwd=ROOT)
    if raw.returncode != 0 or not raw.stdout.strip():
        raw = subprocess.run(["exa", "--json", "docs"], capture_output=True, text=True, cwd=ROOT)
    if raw.returncode != 0 or not raw.stdout.strip():
        raise SystemExit(f"`exa --json docs` failed:\n{raw.stderr[-2000:]}")
    tree = json.loads(raw.stdout)

    panels = root_panels()
    missing_blurbs = [t for t, _ in panels if t not in PANEL_BLURBS]
    if missing_blurbs:
        raise SystemExit(f"PANEL_BLURBS has no sentence for: {missing_blurbs}")
    for group, page in GROUP_GUIDES.items():
        if not (ROOT / "docs" / page).exists():
            raise SystemExit(f"GROUP_GUIDES[{group!r}] -> docs/{page} does not exist")

    anchors = guide_anchors()
    areas = panel_anchors()
    by_top = {c["name"].split()[1]: c for c in tree["subcommands"]}
    placed: set[str] = set()
    out_panels = []
    for title, names in panels:
        commands = []
        for top in names:
            node = by_top.get(top)
            if node is None:
                continue
            placed.add(top)
            acc: list[dict] = []
            if node.get("subcommands"):
                leaves(node, acc)
            else:
                acc.append(node)
            for leaf in acc:
                path = leaf["name"]
                parts = path.split()
                anchor = None
                for i in range(len(parts), 1, -1):
                    anchor = anchors.get(" ".join(parts[:i]))
                    if anchor:
                        break
                # A command the guide has no heading for yet still links to its area's section.
                anchor = anchor or areas.get(title)
                commands.append(
                    {
                        "cmd": path,
                        "group": top,
                        "help": first_sentence(leaf.get("help", "")),
                        "anchor": anchor,
                        "guide": GROUP_GUIDES.get(top),
                    }
                )
        out_panels.append(
            {
                "title": title,
                "slug": slugify(title),
                "blurb": PANEL_BLURBS[title],
                "groups": [n for n in names if n in by_top],
                "commands": commands,
            }
        )

    unplaced = sorted(set(by_top) - placed)
    if unplaced:
        out_panels.append(
            {
                "title": "Other",
                "slug": "other",
                "blurb": "Commands not yet filed under a lifecycle panel.",
                "groups": unplaced,
                "commands": [],
            }
        )
    return {
        "source": "exa --json docs + _ROOT_PANELS (platform/cli/src/examlops/cli/main.py)",
        "total_commands": sum(len(p["commands"]) for p in out_panels),
        "total_groups": len(by_top),
        "panels": out_panels,
    }


def render_page(data: dict) -> str:
    """The static half of the page: indexed by search, readable without JavaScript."""
    rows = []
    for p in data["panels"]:
        if not p["commands"]:
            continue
        groups = ", ".join(f"`{g}`" for g in p["groups"])
        rows.append(f"| {p['title']} | {p['blurb']} | {groups} | {len(p['commands'])} |")
    table = "\n".join(rows)
    return f"""---
title: Every capability
description: All {data["total_commands"]} exa commands, grouped into the twelve lifecycle areas of exa --help, searchable.
hide:
  - navigation
---

<!-- GENERATED by platform/ci/gen_capability_atlas.py from the live `exa` command tree.
     Do not edit by hand: run `python3 platform/ci/gen_capability_atlas.py` instead.
     tests/unit/test_docs_capability_atlas.py fails when this page or its JSON is stale. -->

# Every capability

Everything ExaMLOps can do is reachable from the `exa` CLI: **{data["total_commands"]} commands**
in {data["total_groups"]} command groups. They are grouped here the same way `exa --help` groups
them, into twelve areas of the model lifecycle. The bar is sized by the number of commands in
each area; select an area to filter, or search by what you want to do.

The same capabilities are available from the [dashboard](../dashboard/usage-guide.md), from
[Skipper](../guides/agent.md) in plain English, and to other agents over
[MCP](../guides/agent.md) — all through the same code paths.

<div class="xm-atlas" markdown>

| Area | What you can do | Command groups | Commands |
|---|---|---|---|
{table}

</div>

Each command links to its section of the [command guide](../reference/cli-commands-guide.md),
which gives its purpose, a use case and a runnable example, and to the guide for its area.
For exact flags, run `exa <command> -h` or read the [full command tree](../reference/cli-generated.md).
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the JSON is stale")
    args = ap.parse_args()
    data = build()
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    page = render_page(data)
    if args.check:
        stale = [
            f
            for f, want in ((OUT, text), (PAGE, page))
            if (f.read_text() if f.exists() else "") != want
        ]
        if stale:
            names = ", ".join(str(f.relative_to(ROOT)) for f in stale)
            print(f"{names} stale — run: python3 {Path(__file__).relative_to(ROOT)}")
            return 1
        print(f"capability atlas up to date ({data['total_commands']} commands)")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    PAGE.write_text(page)
    print(
        f"wrote {OUT.relative_to(ROOT)} and {PAGE.relative_to(ROOT)}: "
        f"{data['total_commands']} commands in {len(data['panels'])} panels"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
