"""Every path from platform code to a scheduler is a known, admitted one (ADR 0116, ADR 0108 d3).

ADR 0108 decision 3: workloads never address the substrate directly — any capability reachable by
a second path is ungoverned. For compute, the substrate is the execution seam's ``submit_job``.
This guard lists every call site in the product trees and fails on a new one, so a submission that
bypasses admission cannot be added without a reviewer seeing it here.

It scans source text (AST), not imports, so an aliased adapter does not hide a call.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREES = ("platform/cli/src", "pipelines", "serving", "platform/services")

#: file -> how that submission is admitted. Adding a row is a design decision, not a formality.
ALLOWED = {
    "platform/cli/src/examlops/scheduler_jobs.py": (
        "the platform job chokepoint: dispatch.submit_admitted"
    ),
    "platform/cli/src/examlops/llm_endpoints.py": (
        "HPC serving allocations: dispatch.submit_admitted; released on stop()"
    ),
    "pipelines/pipeline_generator.py": (
        "training job inside a run `exa pipeline run` already admitted (pipeline_run_gate); "
        "a flow started outside the CLI is not admitted - see the ADR 0116 status line"
    ),
}


def _calls(root: Path = ROOT) -> dict[str, int]:
    found: dict[str, int] = {}
    for tree in TREES:
        base = root / tree
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in rel or "node_modules" in rel:
                continue
            try:
                module = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(module):
                # Any *reference* to the verb, not only a direct call: `f = a.submit_job; f()`
                # and `getattr(a, "submit_job")(...)` reach the scheduler just the same.
                ref = isinstance(node, ast.Attribute) and node.attr == "submit_job"
                via_getattr = (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "submit_job"
                )
                if ref or via_getattr:
                    found[rel] = found.get(rel, 0) + 1
    return found


def test_no_unadmitted_path_to_the_scheduler():
    found = _calls()
    extra = sorted(set(found) - set(ALLOWED))
    assert not extra, (
        f"new direct scheduler submission(s) {extra}: route them through "
        "examlops.admission_seam.dispatch.submit_admitted (or scheduler_jobs.submit), or add "
        "them to ALLOWED with how they are admitted"
    )


#: How many references each allowed file holds. Pinned with ``==``: a second, unadmitted call
#: added to a file that is already allowed would otherwise pass unseen.
EXPECTED_COUNTS = {
    "platform/cli/src/examlops/scheduler_jobs.py": 1,
    "platform/cli/src/examlops/llm_endpoints.py": 1,
    "pipelines/pipeline_generator.py": 1,
}


def test_each_allowed_file_keeps_exactly_its_admitted_submissions():
    found = _calls()
    assert set(EXPECTED_COUNTS) == set(ALLOWED)
    assert {k: found.get(k, 0) for k in ALLOWED} == EXPECTED_COUNTS


def test_the_allow_list_has_no_dead_entries():
    """A stale entry would silently re-open the door for whatever is written there next."""
    found = _calls()
    dead = sorted(set(ALLOWED) - set(found))
    assert not dead, f"ALLOWED names files that no longer submit: {dead}"


def test_the_guard_can_fail(tmp_path):
    """A planted call (aliased, inside a method) is found; the scan is not vacuous."""
    rogue = tmp_path / "serving" / "rogue.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text("class S:\n    def go(self, sched):\n        return sched.submit_job('x')\n")
    assert _calls(tmp_path) == {"serving/rogue.py": 1}


def test_the_guard_sees_indirect_references(tmp_path):
    rogue = tmp_path / "pipelines" / "sneaky.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "def go(a):\n"
        "    f = a.submit_job\n"
        "    g = getattr(a, 'submit_job')\n"
        "    return f('x'), g('y')\n"
    )
    assert _calls(tmp_path) == {"pipelines/sneaky.py": 2}
