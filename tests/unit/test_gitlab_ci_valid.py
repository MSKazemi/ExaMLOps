"""Guard: `.gitlab-ci.yml` must be structurally sound before it reaches GitLab.

This project's red-pipeline history is mostly *pipeline creation* errors and lint steps that
were never run locally — a job naming a stage that does not exist, a `needs:` pointing at a job
that was renamed, a `rules:` clause that silently never matches. GitLab reports those only after
a push, and on a repo where pushing is a deliberate, approved act that feedback loop is far too
long.

None of this replaces GitLab's own `/ci/lint` endpoint, which needs a token this checkout does
not carry. It covers the errors that have actually bitten, cheaply, in the local gate.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_CI = Path(__file__).resolve().parents[2] / ".gitlab-ci.yml"

# Top-level keys that configure the pipeline rather than declare a job.
_NOT_JOBS = {"stages", "variables", "workflow", "include", "default", "image", "before_script"}


def _pipeline() -> dict:
    return yaml.safe_load(_CI.read_text())


def _jobs(doc: dict) -> dict[str, dict]:
    return {
        name: body
        for name, body in doc.items()
        if name not in _NOT_JOBS and not name.startswith(".") and isinstance(body, dict)
    }


def test_the_file_parses():
    assert isinstance(_pipeline(), dict)


def test_every_job_declares_a_stage_that_exists():
    """An undefined stage fails pipeline *creation*, so nothing in the pipeline runs at all."""
    doc = _pipeline()
    stages = set(doc["stages"])
    bad = {n: b["stage"] for n, b in _jobs(doc).items() if b.get("stage") not in stages}
    assert not bad, (
        f"jobs naming a stage that is not in `stages`: {bad} (declared: {sorted(stages)})"
    )


def test_every_needs_points_at_a_real_job():
    """A `needs:` on a renamed or deleted job is the other pipeline-creation error."""
    doc = _pipeline()
    jobs = _jobs(doc)
    dangling: dict[str, list[str]] = {}
    for name, body in jobs.items():
        wanted = [n["job"] if isinstance(n, dict) else n for n in body.get("needs", [])]
        missing = [w for w in wanted if w not in jobs]
        if missing:
            dangling[name] = missing
    assert not dangling, f"`needs:` referencing jobs that do not exist: {dangling}"


def test_a_needed_job_runs_no_later_than_the_job_that_needs_it():
    """`needs` cannot look forward: GitLab rejects a dependency on a later stage."""
    doc = _pipeline()
    order = {s: i for i, s in enumerate(doc["stages"])}
    jobs = _jobs(doc)
    backwards: dict[str, list[str]] = {}
    for name, body in jobs.items():
        here = order.get(body.get("stage"), -1)
        late = [
            w
            for w in (n["job"] if isinstance(n, dict) else n for n in body.get("needs", []))
            if w in jobs and order.get(jobs[w].get("stage"), -1) > here
        ]
        if late:
            backwards[name] = late
    assert not backwards, f"jobs depending on a *later* stage: {backwards}"


def test_the_deploy_gate_cannot_fire_on_a_schedule():
    """A nightly pipeline exists to report on main, not to ship it.

    A scheduled pipeline on the default branch satisfies `$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH`,
    so without an explicit `never` the schedule would redeploy production every night. The rule
    also has to come *first* — GitLab takes the first match.
    """
    rules = _pipeline()["deploy:lxp"]["rules"]
    first = rules[0]
    assert first.get("when") == "never" and "schedule" in str(first.get("if", "")), (
        f"the first rule on deploy:lxp must exclude scheduled pipelines; got {first}"
    )


def test_the_release_job_only_runs_on_tags():
    """A release built from a branch would name a tag that does not exist."""
    job = _pipeline()["release:gitlab"]
    assert job["release"]["tag_name"] == "$CI_COMMIT_TAG"
    assert any("CI_COMMIT_TAG" in str(r.get("if", "")) for r in job["rules"])
    assert job["rules"][-1] == {"when": "never"}, "release must not fall through to a default"
