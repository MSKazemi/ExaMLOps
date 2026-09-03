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


# ── the manual rollback button ───────────────────────────────────────────────
#
# `rollback:lxp` is the one job whose whole purpose is to work when the pipeline is unwell,
# which makes its configuration easy to "tidy" into uselessness: adding a `needs:` list or
# dropping `allow_failure` both look like corrections and both disable it.


def _rollback() -> dict:
    job = _pipeline().get("rollback:lxp")
    assert job, "the manual rollback job is gone"
    return job


def test_the_rollback_button_does_not_wait_for_anything():
    """You roll back *because* jobs failed, so it must be clickable in a red pipeline."""
    assert _rollback().get("needs") == [], (
        "rollback:lxp declares `needs:`. GitLab will not let the button be clicked until "
        "those jobs succeed — so it becomes unavailable in exactly the situation it exists "
        "for. It must be `needs: []`."
    )


def test_the_rollback_button_never_blocks_a_pipeline():
    job = _rollback()
    assert job.get("when") == "manual"
    assert job.get("allow_failure") is True, (
        "an un-clicked manual job with allow_failure: false leaves the pipeline blocked, so "
        "every main pipeline would sit waiting for a rollback nobody wants"
    )


def test_the_rollback_cannot_race_a_deploy():
    """One node, one production. The health gate must not be deciding while this runs."""
    pipeline = _pipeline()
    group = pipeline["deploy:lxp"]["resource_group"]
    assert _rollback().get("resource_group") == group
    assert pipeline["smoke:lxp"].get("resource_group") == group


def test_the_rollback_is_health_checked():
    """A rollback that is not verified is a hope, not a recovery."""
    steps = "\n".join(_rollback().get("script") or [])
    assert "smoke_check.sh" in steps, (
        "rollback:lxp restores a release without probing it, so a rollback that fails to "
        "restore health reports success"
    )


# ── workflow rules ───────────────────────────────────────────────────────────
#
# `workflow.rules` decides whether a pipeline is created AT ALL, which makes it the one
# block in this file where a mistake is silent in the worst way: nothing runs, and nothing
# reports that nothing ran. Two properties are worth holding.


def _workflow_rules() -> list[dict]:
    rules = _pipeline()["workflow"]["rules"]
    assert rules, "workflow.rules is empty — no pipeline would ever be created"
    return rules


def _rule_index(predicate) -> int:
    for i, rule in enumerate(_workflow_rules()):
        if predicate(str(rule.get("if", ""))):
            return i
    return -1


def test_the_duplicate_suppressor_is_evaluated_before_the_catch_all():
    """Rule order IS the mechanism, not a style choice.

    GitLab takes the first matching rule. The `never` for a branch with an open MR must come
    before the plain-branch rule; behind it, the catch-all matches first and every push to a
    branch with an MR builds the full suite twice against identical code.
    """
    never = _rule_index(lambda c: "CI_OPEN_MERGE_REQUESTS" in c)
    catch_all = _rule_index(lambda c: c.strip() == "$CI_COMMIT_BRANCH")
    assert never >= 0, "the duplicate-pipeline suppressor is gone"
    assert catch_all >= 0, "the plain-branch rule is gone — topic branches would not build"
    assert never < catch_all, (
        f"the CI_OPEN_MERGE_REQUESTS `never` rule is at index {never}, after the catch-all "
        f"branch rule at {catch_all}. The catch-all matches first, so nothing is ever "
        "suppressed and every MR branch builds twice."
    )


def test_main_and_tags_can_still_create_a_pipeline():
    """The failure mode of a workflow-rules mistake is that NOTHING runs, silently."""
    rules = _workflow_rules()
    for needed in ("$CI_COMMIT_TAG", "$CI_DEFAULT_BRANCH"):
        matching = [r for r in rules if needed in str(r.get("if", ""))]
        assert matching, f"no workflow rule admits {needed} — releases or deploys would stop"
        assert any(r.get("when") != "never" for r in matching), (
            f"every workflow rule mentioning {needed} is a `never`, so main/tag pipelines "
            "would not be created at all — and no job would report it"
        )


def test_no_job_needs_another_in_its_own_stage():
    """Stricter than the rule above, and deliberately so.

    Same-stage ``needs:`` is legal — but only from GitLab 14.2. This pipeline runs on a
    self-managed Seanergys instance whose version is not something the file should have to
    assume, and the failure mode is a pipeline that will not be *created*: no job runs, and
    no job reports that nothing ran. Keeping every dependency pointing at a strictly earlier
    stage costs nothing and removes the assumption entirely.

    If you ever need same-stage ``needs``, delete this test on purpose after checking the
    instance version — do not work around it by renaming a stage.
    """
    doc = _pipeline()
    order = {s: i for i, s in enumerate(doc["stages"])}
    jobs = _jobs(doc)
    same: dict[str, list[str]] = {}
    for name, body in jobs.items():
        here = order.get(body.get("stage"), -1)
        peers = [
            w
            for w in (n["job"] if isinstance(n, dict) else n for n in body.get("needs", []))
            if w in jobs and order.get(jobs[w].get("stage"), -1) == here
        ]
        if peers:
            same[name] = peers
    assert not same, (
        "these jobs depend on another job in the SAME stage, which requires GitLab 14.2+: "
        f"{same}. Move the depended-on job to an earlier stage."
    )
