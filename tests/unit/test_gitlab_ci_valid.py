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


def test_every_job_running_the_repository_guards_can_actually_run_them():
    """A job that runs a repository guard must have the binary that guard shells out to.

    Seven guards in `tests/unit/` answer questions about the repository by shelling out —
    `test_public_tree_privacy` above all, which is what stops private assistant material and
    personal paths reaching the *published* tree. `python:3.12-slim` ships neither `git` nor
    `make`, so on pipeline #3241 (2026-08-25) those guards did not fail an assertion, they died
    on `FileNotFoundError` — thirteen in `test:examlops` and the same thirteen in
    `test:postgres`. They had never once run in CI.

    `_guard_deps.require_binary` makes that say so in a sentence instead of a traceback. This
    test is the other half: it keeps the binaries in the image, so the guards run at all.

    Provisioning counts wherever it happens — `test:infra:helm` does its `apk add` in `script`,
    not `before_script`, and that is fine. Jobs that `cd` into a cloned repository are exempt:
    `test:modelzoo` runs *modelzoo's* `tests/unit/`, not ours.
    """
    # Only binaries a CI image is known *not* to ship. `awk` is deliberately absent even though
    # test_release_notes_are_extractable now requires it: those five tests were among the
    # thirteen observed *executing* and failing on `git` in pipeline #3241, which they could not
    # have done had their `which("awk")` gate been unsatisfied — so Debian-slim provides it, and
    # demanding an install line for it would be a false positive. A guard with false positives
    # gets switched off.
    needs_binary = {
        "test_public_tree_privacy.py": {"git"},
        "test_env_vars_are_documented.py": {"git"},
        "test_release_notes_are_extractable.py": {"git"},
        "test_dockerfile_build_context.py": {"git"},
        "test_env_example_public.py": {"git"},
        "test_makefile_is_honest.py": {"make"},
    }
    offenders = []
    for name, job in _pipeline().items():
        if not isinstance(job, dict) or name.startswith("."):
            continue
        script = " ".join(str(s) for s in job.get("script", []))
        setup = " ".join(str(s) for s in job.get("before_script", []))
        if "cd " in setup or "cd " in script:
            continue  # operates on another checkout — not our guards
        if f"tests/unit/{chr(32)}" in script + " " or "tests/unit/ " in script + " ":
            required = {b for bins in needs_binary.values() for b in bins}
        else:
            required = {b for f, bins in needs_binary.items() if f in script for b in bins}
        missing = sorted(b for b in required if b not in setup + " " + script)
        if missing:
            offenders.append(f"{name}: never installs {', '.join(missing)}")
    assert not offenders, (
        "these jobs run repository guards without the binaries those guards shell out to, so "
        "the guards will crash instead of guarding:\n  " + "\n  ".join(offenders)
    )


def test_every_helm_gated_test_file_runs_in_the_one_job_that_has_helm():
    """A `skipif(helm is None)` test must be named in a job whose image ships helm.

    The sibling above catches a guard that *crashes* for want of a binary. This catches the
    quieter half of the same anti-pattern: a guard that **skips**, and so reports success.

    Measured on pipeline #3241 (2026-08-25). Eleven chart assertions across four files skipped
    in `test:examlops`, because `python:3.12-slim` has no helm. The only job with helm —
    `test:infra:helm`, on `alpine/helm` — named just one of those four files, and died at
    collection before reaching it. So no assertion about the Helm chart ran anywhere in CI,
    while the pipeline reported the chart green. That matters beyond tidiness: publishing the
    chart as a Helm repository is on the roadmap, and it would have shipped unverified.

    The list is derived from the tree, not typed here, so a new helm-gated file joins the
    guard by existing.
    """
    # Assembled at run time so this file does not match its own needle.
    needle = "skipif(HELM is " + "None"
    gated = sorted(
        p.name
        for p in _CI.parent.joinpath("tests", "unit").glob("test_*.py")
        if needle in p.read_text(encoding="utf-8")
    )
    assert gated, "no helm-gated test files found — has the skip marker been renamed?"

    with_helm = [
        name
        for name, job in _pipeline().items()
        if isinstance(job, dict)
        and not name.startswith(".")
        and "helm" in str(job.get("image", "")).lower()
    ]
    assert with_helm, "no CI job runs on an image that ships helm"

    covered = set()
    for name in with_helm:
        script = " ".join(str(s) for s in _pipeline()[name].get("script", []))
        covered |= {f for f in gated if f in script}

    missing = sorted(set(gated) - covered)
    assert not missing, (
        "these test files skip their assertions unless helm is installed, and no job that has "
        "helm runs them — so they assert nothing in CI:\n  " + "\n  ".join(missing) + "\n"
        f"add them to the pytest invocation in: {', '.join(with_helm)}"
    )
