"""Every drill we run is a drill we document, and every document it points at exists (plan P5).

The reliability material grew one iteration at a time: a drill, a table row, a guide section, a
runbook paragraph. That is how it should grow, and it is also how it rots — a drill added to
`make chaos-drills-kind` with no section in [game days](../../docs/guides/game-days.md) is a failure
mode nobody reading the operator page knows the platform has been held to, and a guide that names a
drill file which has been renamed sends its reader nowhere.

So the Makefile is the definition: **the drills the chaos targets run** must each have a row in the
testing guide's chaos table and a section in the game-days guide, and every `docs/…` path a drill
names in its own text must exist. Two chart tests run in the kind target's neighbourhood are not
drills and say so here rather than by being forgotten.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
TESTING = (ROOT / "docs" / "guides" / "testing.md").read_text(encoding="utf-8")
GAME_DAYS = (ROOT / "docs" / "guides" / "game-days.md").read_text(encoding="utf-8")
WORKFLOW = ROOT / ".github" / "workflows" / "chaos-drills.yml"

#: Tests that run in a chaos target but are not chaos drills: they verify that the chart renders and
#: deploys, and belong to the Helm documentation rather than to the game-day set.
NOT_A_DRILL = {
    "test_helm_gateway_kind_live.py": "renders the chart's gateway and checks it serves",
    "test_helm_workload_identity_kind_live.py": "renders the chart's SPIFFE wiring",
}


def _recipe(target: str) -> str:
    """Just that target's recipe: its own lines, not everything below it in the file.

    This used to slice from `chaos-drills:` to the end of the Makefile, which was the same thing
    only while nothing else lived down there. Adding the `*-live` targets on 2026-09-15 swept eight
    unrelated tests in and demanded game-day sections for a Prometheus discovery check. A guard
    whose scope is "the rest of the file" grows claims nobody made.
    """
    start = MAKEFILE.index(f"\n{target}:") + 1
    rest = MAKEFILE[start:]
    end = len(rest)
    for i, line in enumerate(rest.splitlines(keepends=True)):
        if i and line[:1] not in ("\t", "\n", " ", "#"):  # the next target starts here
            end = sum(len(x) for x in rest.splitlines(keepends=True)[:i])
            break
    return rest[:end]


def _drills() -> set[str]:
    """The drill files the two chaos targets actually run."""
    target = _recipe("chaos-drills") + _recipe("chaos-drills-kind")
    names = set(re.findall(r"tests/integration/(\w+)\.py", target))
    names |= {m for m in re.findall(r"for drill in ([\w\s\\\n]+?);", target)[0].split()
              if m.startswith("test_")}  # fmt: skip
    return {f"{name}.py" for name in names}


def test_the_targets_really_name_drills():
    """If this finds nothing, the rest of the file is vacuous."""
    drills = _drills()
    assert len(drills) >= 6, drills
    assert "test_datastore_outage_drill_live.py" in drills
    assert "test_control_plane_partition_kind_live.py" in drills


def test_every_drill_has_a_row_in_the_testing_guides_table():
    missing = [d for d in sorted(_drills()) if d not in TESTING and d not in NOT_A_DRILL]
    assert not missing, (
        f"{missing} run in a chaos target but appear nowhere in docs/guides/testing.md — the table "
        "is how anyone finds out the drill exists and what turns it on"
    )


def test_every_drill_has_a_section_in_the_game_days_guide():
    missing = [d for d in sorted(_drills()) if d not in GAME_DAYS and d not in NOT_A_DRILL]
    assert not missing, (
        f"{missing} run in a chaos target but are not described in docs/guides/game-days.md — an "
        "operator reading that page would not know the platform is held to that failure at all"
    )


def test_the_documents_the_drills_point_at_exist():
    """A drill's own text is where its reader goes next; a renamed guide must not strand them."""
    broken: list[str] = []
    for path in sorted((ROOT / "tests" / "integration").glob("*_live.py")):
        for named in re.findall(r"docs/[a-zA-Z0-9/_-]+\.md", path.read_text(encoding="utf-8")):
            if not (ROOT / named).exists():
                broken.append(f"{path.name} → {named}")
    assert not broken, broken


def test_the_exemptions_still_describe_something_real():
    for name in NOT_A_DRILL:
        assert (ROOT / "tests" / "integration" / name).exists(), name


# ── and the drills a laptop can run are the drills CI runs ───────────────────
#
# A reliability property rots in a way nothing else in the suite can see: a retry budget, a queue
# bound or a health check can stop working while every unit test stays green, because they all run
# against a healthy stack. Only a drill notices, and a drill nobody runs notices nothing — so the
# weekly workflow must invoke every chaos target the Makefile defines. The targets, not the drill
# files: the workflow calls `make`, which is what keeps one drill list instead of two.


def _chaos_targets() -> set[str]:
    return set(re.findall(r"^(chaos-drills[\w-]*):", MAKEFILE, re.MULTILINE))


def test_there_are_chaos_targets_to_check():
    """If this finds nothing, the rest of this section is vacuous."""
    targets = _chaos_targets()
    assert {"chaos-drills", "chaos-drills-kind"} <= targets, targets


def test_every_chaos_target_is_invoked_by_the_weekly_workflow():
    assert WORKFLOW.exists(), f"{WORKFLOW.name} is gone; the drills would run nowhere but a laptop"
    text = WORKFLOW.read_text(encoding="utf-8")
    missing = [t for t in sorted(_chaos_targets()) if f"make {t}" not in text]
    assert not missing, (
        f"{missing} exist in the Makefile but nothing in {WORKFLOW.name} runs them — a drill that "
        "only ever runs by hand is a drill that stops running"
    )


def test_the_workflow_runs_on_a_schedule_and_can_be_asked_for():
    """A `workflow_dispatch`-only file would need someone to remember; the schedule is the point."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))  # PyYAML reads bare `on` as True
    assert "schedule" in triggers, f"{WORKFLOW.name} has no schedule: {sorted(triggers)}"
    assert triggers["schedule"] and triggers["schedule"][0].get("cron"), triggers["schedule"]
    assert "workflow_dispatch" in triggers, "a drill run must also be askable for on demand"


def test_the_drills_are_a_report_and_not_a_required_check():
    """They take tens of minutes and measure timing on a shared runner. Wiring them into the one
    required check (`ci-ok`, in ci.yml) would make a slow runner look like a regression and block
    every merge; the weekly artifact is what gets read instead."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "chaos-drills" not in ci, (
        "ci.yml runs the drills; they belong in their own weekly report"
    )
