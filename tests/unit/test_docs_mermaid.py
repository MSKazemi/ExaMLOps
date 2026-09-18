"""The published documentation's diagrams parse, and the checker that says so can still refuse.

The site renders ```mermaid fences in the reader's browser, so nothing at build time reads them:
`mkdocs build --strict` passes a diagram mermaid cannot parse and the reader gets a red error box.
Eleven went unchecked for months and two were broken.

Two layers, and the split is deliberate:

* **Always**, with no toolchain at all — the fences are found, and each names a diagram type
  mermaid knows. This is not a parser and does not pretend to be one; it catches the empty fence
  and the misspelled header, and it keeps *something* in the suite for a contributor with no node.
* **With the checker installed** — the real grammar, `mermaid.parse` from the version the site
  loads, over every published fence. `npm ci --prefix platform/ci/mermaid` (CI's docs job does it).

Two tests keep the rest honest, because a guard that cannot fail proves nothing and one that
fails on valid input gets switched off: diagrams known to be invalid must be refused, and one
whose shape merely *looks* invalid — a semicolon inside a quoted label — must not be.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "platform" / "ci"))

import check_mermaid  # noqa: E402
from check_mermaid import (  # noqa: E402
    DOCS_DIR,
    TOOL_DIR,
    Fence,
    ToolchainMissing,
    collect,
    parse_all,
)

#: The keyword a mermaid diagram opens with. Kept here rather than derived from the library so the
#: no-toolchain layer needs nothing installed; a type mermaid gains later fails here first, which
#: is a prompt to add it, not a false report about the diagram.
DIAGRAM_TYPES = {
    "architecture-beta",
    "block-beta",
    "C4Context",
    "classDiagram",
    "erDiagram",
    "flowchart",
    "gantt",
    "gitGraph",
    "graph",
    "journey",
    "mindmap",
    "packet-beta",
    "pie",
    "quadrantChart",
    "requirementDiagram",
    "sankey-beta",
    "sequenceDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "timeline",
    "xychart-beta",
    "zenuml",
}

_INSTALLED = shutil.which("node") is not None and (TOOL_DIR / "node_modules" / "mermaid").is_dir()
needs_checker = pytest.mark.skipif(not _INSTALLED, reason="npm ci --prefix platform/ci/mermaid")


@pytest.fixture(scope="module")
def fences() -> list[Fence]:
    found = collect([DOCS_DIR])
    assert found, "no mermaid fences found at all — the extractor, not the documentation, is broken"
    return found


# ── what holds with no toolchain ─────────────────────────────────────────────


def _declared_type(fence: Fence) -> str:
    first = next((line.strip() for line in fence.text.splitlines() if line.strip()), "")
    return re.split(r"[\s;]", first, maxsplit=1)[0]


def test_every_fence_declares_a_diagram_type_mermaid_knows(fences):
    """A misspelled header (`flowcart`) is the cheapest way to get an error box."""
    unknown = [
        f"{fence.file}:{fence.line}: {_declared_type(fence)!r}"
        for fence in fences
        if _declared_type(fence) not in DIAGRAM_TYPES
    ]
    assert not unknown, "diagram type not recognised:\n  " + "\n  ".join(unknown)


def test_no_fence_is_empty(fences):
    empty = [f"{f.file}:{f.line}" for f in fences if not f.text.strip()]
    assert not empty, f"empty mermaid fences: {empty}"


def test_the_extractor_reads_a_fence_the_way_the_site_does(tmp_path):
    """Content between the markers, at the line the fence opens on — and a longer run of backticks
    closes only on its own length, which is how a diagram containing a fence is written."""
    page = tmp_path / "page.md"
    page.write_text(
        "intro\n\n```mermaid\nflowchart LR\n  A --> B\n```\n\nmiddle\n\n"
        "````mermaid\nflowchart TD\n```\n  C --> D\n````\n",
        encoding="utf-8",
    )

    first, second = collect([page])

    assert (first.line, first.text) == (3, "flowchart LR\n  A --> B")
    assert second.line == 10 and second.text.endswith("  C --> D")


# ── a check that did not run is not a pass ───────────────────────────────────


def test_an_absent_node_is_refused_rather_than_reported_as_clean(monkeypatch):
    """The whole value of this guard rests on this. If a missing toolchain returned "no failures",
    every tree would look clean the moment node disappeared from the image — the failure mode that
    makes a CI check worse than none, because now there is a green tick saying it was checked."""
    monkeypatch.setattr(check_mermaid.shutil, "which", lambda _: None)

    with pytest.raises(ToolchainMissing):
        parse_all([Fence("x.md", 1, "flowchart LR\n  A --> B")])


def test_an_uninstalled_checker_is_refused_too(monkeypatch, tmp_path):
    """node alone is not enough — `npm ci` must have run. The message says which command."""
    monkeypatch.setattr(check_mermaid, "TOOL_DIR", tmp_path)

    with pytest.raises(ToolchainMissing, match="npm ci"):
        parse_all([Fence("x.md", 1, "flowchart LR\n  A --> B")])


def test_the_script_exits_two_when_it_could_not_run(monkeypatch, capsys):
    """Not 0, and not 1 either: a broken diagram and an absent checker are different facts, and
    `make docs-build` treats them differently (2 is a loud notice locally, a failure in CI)."""
    monkeypatch.setattr(check_mermaid.shutil, "which", lambda _: None)

    assert check_mermaid.main([]) == 2
    assert "DID NOT RUN" in capsys.readouterr().err


# ── what the real grammar says ───────────────────────────────────────────────


@needs_checker
def test_every_published_diagram_parses(fences):
    failures = parse_all(fences)

    assert not failures, "diagrams that will not render:\n  " + "\n  ".join(
        f"{f['file']}:{f['line']}: {f['error']}" for f in failures
    )


@needs_checker
def test_the_checker_refuses_a_diagram_that_does_not_parse():
    """The guard's own teeth. Each of these is a real failure mode: a misspelled diagram type, an
    unterminated subgraph, and a semicolon inside a label — which mermaid reads as a statement
    separator, so the label ends there and the rest becomes a syntax error. That last one is not
    hypothetical: it is what this check caught the first time it ran."""
    broken = [
        Fence("bad-type.md", 1, "flowcart LR\n  A --> B"),
        Fence("unclosed.md", 1, "flowchart LR\n  subgraph S\n    A --> B"),
        Fence(
            "semicolon.md", 1, "sequenceDiagram\n  Note over A: bounded (MAX); drops\n  A->>B: x"
        ),
    ]

    refused = {failure["file"] for failure in parse_all(broken)}

    assert refused == {"bad-type.md", "unclosed.md", "semicolon.md"}


#: One valid diagram per type the documentation actually uses. A checker that refuses everything
#: proves as little as one that accepts everything, and a false rejection is what gets a check
#: switched off — so the accepting half is tested as deliberately as the refusing half.
SAMPLES = {
    "flowchart": "flowchart LR\n  A[Start] --> B{Choice}\n  B -->|yes| C[Done]",
    "graph": "graph TD\n  A --> B",
    "sequenceDiagram": "sequenceDiagram\n  Alice->>Bob: hi\n  Bob-->>Alice: hello",
    "erDiagram": "erDiagram\n  CUSTOMER ||--o{ ORDER : places",
    "mindmap": "mindmap\n  root((platform))\n    control\n    data",
}


@needs_checker
def test_a_valid_diagram_of_every_type_the_documentation_uses_is_accepted(fences):
    used = {_declared_type(fence) for fence in fences}
    assert used <= set(SAMPLES), f"a diagram type this test does not sample: {used - set(SAMPLES)}"

    assert parse_all([Fence(f"{name}.md", 1, text) for name, text in SAMPLES.items()]) == []


@needs_checker
def test_a_semicolon_inside_a_quoted_label_is_not_a_failure():
    """The precise shape of the false positive a cruder check would produce. Mermaid splits
    statements on `;` in an *unquoted* label — the defect this guard found — but a quoted label
    holds it as text, and `docs/guides/architecture.md` has had one for months, rendering fine.
    A guard that failed this would be reporting a diagram that works."""
    quoted = Fence("quoted.md", 1, 'flowchart LR\n  SB["Real bus in prod; mock in dev"] --> B')

    assert parse_all([quoted]) == []


# ── the version the check speaks must be the version the site renders ────────


def test_the_pinned_mermaid_matches_the_major_the_site_loads():
    """Material loads mermaid itself, from a URL inside its own bundle. Checking against a
    different major would make this guard authoritative about a grammar no reader ever runs — so
    when Material moves to the next major, this fails and says to move the pin with it."""
    material = Path(__import__("material").__file__).parent
    bundles = list((material / "templates" / "assets" / "javascripts").glob("bundle.*.min.js"))
    assert bundles, "mkdocs-material's bundle is not where this test expects it"

    loaded = set()
    for bundle in bundles:
        loaded.update(re.findall(r"mermaid@(\d+)", bundle.read_text(encoding="utf-8")))
    assert loaded, "no mermaid URL found in Material's bundle — has the integration changed?"

    pinned = re.search(r'"mermaid":\s*"(\d+)', (TOOL_DIR / "package.json").read_text())
    assert pinned and loaded == {pinned.group(1)}, (
        f"the site loads mermaid@{sorted(loaded)} but the check is pinned to "
        f"{pinned.group(1) if pinned else '?'}.x — move platform/ci/mermaid/package.json "
        "(and its lockfile) to that major."
    )
