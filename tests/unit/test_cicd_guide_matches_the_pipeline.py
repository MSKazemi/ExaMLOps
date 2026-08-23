"""The CI/CD guide is the only prose description of the pipeline; hold it to the pipeline.

`docs/guides/cicd.md` documents each job under its own `###` heading. Nothing compared that list
to `.gitlab-ci.yml`, and it had drifted three ways at once:

* `test:docs` — a **blocking** job, in both `deploy:lxp`'s and `release:gitlab`'s `needs:` — had no
  section at all, so the guide described a gate that reads as absent.
* `notify:failure` was likewise undocumented.
* Two headings named jobs that do not exist: `post-deploy:notify-model-changes` and
  `post-deploy:retrain-push-models`, where the real jobs carry an `lxp:` segment. Searching the
  pipeline for a documented name found nothing — the worse direction, because it reads as the job
  having been removed.

`tests/unit/test_ci_gate_coverage.py` guards the `needs:` lists and the preflight mirror. This
guards the prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".gitlab-ci.yml"
GUIDE = ROOT / "docs" / "guides" / "cicd.md"

# Keys that are pipeline configuration rather than jobs.
_NOT_JOBS = {"stages", "variables", "workflow", "default", "include", "image", "before_script"}


def _jobs() -> set[str]:
    doc = yaml.safe_load(CI.read_text())
    return {
        name
        for name, body in doc.items()
        if isinstance(body, dict) and not name.startswith(".") and name not in _NOT_JOBS
    }


def _documented() -> set[str]:
    """`###` headings in the guide that look like a job name (`stage:thing`)."""
    return set(re.findall(r"^### ([a-z][\w.-]*(?::[\w.-]+)+)\s*$", GUIDE.read_text(), re.M))


def test_there_are_jobs_and_headings_to_compare():
    """Otherwise both assertions below pass by inspecting nothing."""
    assert len(_jobs()) >= 15, sorted(_jobs())
    assert len(_documented()) >= 15, sorted(_documented())


def test_every_pipeline_job_has_a_section_in_the_guide():
    missing = sorted(_jobs() - _documented())
    assert not missing, (
        "these jobs run in .gitlab-ci.yml but the CI/CD guide has no `### <job>` section:\n  "
        + "\n  ".join(missing)
    )


def test_every_job_section_in_the_guide_names_a_real_job():
    phantom = sorted(_documented() - _jobs())
    assert not phantom, (
        "the CI/CD guide documents jobs that do not exist in .gitlab-ci.yml — searching the "
        "pipeline for these names finds nothing:\n  " + "\n  ".join(phantom)
    )
