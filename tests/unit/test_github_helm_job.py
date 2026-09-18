"""Every helm-gated test file runs in GitHub's `helm` job (ADR 0129: GitHub is the release authority).

A test that skips when helm is missing reports success wherever helm is missing, and the `examlops`
unit job has no helm. So a helm-gated file that the `helm` job does not name asserts nothing in
the CI that gates releases. Three did not, until this guard: test_helm_agent_scaling,
test_helm_secret_scoping and test_dashboard_agent_credentials. test_gitlab_ci_valid.py holds the
GitLab mirror to the same rule; this is the GitHub half. The list comes from the tree, so a new
helm-gated file joins it by existing.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_the_helm_job_runs_every_helm_gated_file():
    needle = "skipif(HELM is " + "None"  # assembled so this file does not match itself
    gated = sorted(
        p.name
        for p in (ROOT / "tests" / "unit").glob("test_*.py")
        if needle in p.read_text(encoding="utf-8")
    )
    assert len(gated) >= 5, gated
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = ci["jobs"]["helm"]
    script = " ".join(str(step.get("run", "")) for step in job["steps"])
    assert "helm-validate" in script  # the job that has helm
    missing = [name for name in gated if name not in script]
    assert not missing, f"helm-gated test files the helm job does not run: {missing}"
