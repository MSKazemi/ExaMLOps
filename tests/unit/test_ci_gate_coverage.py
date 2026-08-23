"""Guard: a check that cannot stop anything is not a gate.

Two claims are made elsewhere in this repo, and until now nothing proved either of them:

1. ``.gitlab-ci.yml`` — the pipeline's job list reads as though every red test stops the
   deploy. It does not. ``deploy:lxp`` and ``release:gitlab`` declare ``needs:``, which turns
   them into DAG jobs: they start as soon as *the jobs they name* succeed, regardless of what
   else in the pipeline has failed. A blocking job missing from that list still turns the
   pipeline red — and still lets production be deployed and a release be tagged.
2. ``Makefile`` — ``preflight`` calls itself a "Full local mirror of every BLOCKING GitLab CI
   job". That sentence is only true for as long as someone remembers to extend it, and the
   whole reason preflight exists is that a check which is never run locally is discovered by a
   red pipeline days later.

Both tests fail when a *new* CI job is added, which is the point: adding a check forces a
decision about what it gates and how it is mirrored, rather than leaving it decorative.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".gitlab-ci.yml"
_MAKEFILE = _ROOT / "Makefile"

_NOT_JOBS = {"stages", "variables", "workflow", "include", "default", "image", "before_script"}

# The stages whose jobs exist to *check* the tree. Everything after them acts on it.
_CHECK_STAGES = {"sanity", "test"}

# How each blocking CI check is mirrored by `make preflight`, as a substring of the step's own
# banner. `None` means "deliberately not mirrored" — the recipe must then say so by name, so a
# reader of the preflight output learns what it did NOT cover.
_PREFLIGHT_MIRROR: dict[str, str | None] = {
    "sanity:python-syntax": "sanity: python syntax",
    "sanity:check-structure": "sanity: repo structure",
    "sanity:secret-scan": "sanity: secret scan",
    "test:examlops": "unit tests",
    "test:integration": "integration tests",
    "test:agent": "skipper agent tests",
    "test:frontend": "dashboard frontend",
    "test:control-plane": "control plane",
    "test:postgres": "postgres backend",
    "test:infra:compose": "ci-infra",
    "test:infra:slurm-lint": "ci-infra",
    "test:infra:alert-rules": "ci-infra",
    "test:infra:helm": "helm chart",
}


def _jobs() -> dict[str, dict]:
    doc = yaml.safe_load(_CI.read_text())
    return {
        name: body
        for name, body in doc.items()
        if name not in _NOT_JOBS and not name.startswith(".") and isinstance(body, dict)
    }


def _needs(jobs: dict[str, dict], name: str) -> list[str]:
    out = []
    for entry in jobs.get(name, {}).get("needs") or []:
        out.append(entry["job"] if isinstance(entry, dict) else entry)
    return out


def _reachable(jobs: dict[str, dict], start: str) -> set[str]:
    """Every job ``start`` transitively waits for."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        for dep in _needs(jobs, stack.pop()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def _blocking_checks(jobs: dict[str, dict]) -> set[str]:
    return {
        name
        for name, body in jobs.items()
        if body.get("stage") in _CHECK_STAGES and not body.get("allow_failure")
    }


@pytest.mark.parametrize("gated", ["deploy:lxp", "release:gitlab"])
def test_every_blocking_check_can_actually_stop_it(gated):
    jobs = _jobs()
    assert gated in jobs
    missing = sorted(_blocking_checks(jobs) - _reachable(jobs, gated))
    assert not missing, (
        f"{gated} does not wait for {missing}. Because it declares `needs:`, GitLab starts it "
        "as soon as the jobs it names succeed — a red job outside that list turns the pipeline "
        "red without stopping the deploy or the release. Add it to `needs:`, or mark it "
        "`allow_failure: true` to say out loud that it only advises."
    )


def _preflight_recipe() -> str:
    lines = _MAKEFILE.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("preflight:"))
    body = [lines[start]]
    for ln in lines[start + 1 :]:
        if ln and not ln.startswith(("\t", " ", "#")):
            break
        body.append(ln)
    return "\n".join(body)


def test_the_preflight_mirror_lists_every_blocking_check():
    """Adding a CI check must force a decision about running it locally."""
    unmapped = sorted(_blocking_checks(_jobs()) - set(_PREFLIGHT_MIRROR))
    assert not unmapped, (
        f"{unmapped} is a blocking CI check with no recorded local mirror. Add it to "
        "_PREFLIGHT_MIRROR — with a preflight step, or with None plus a line in the recipe "
        "naming it as not covered."
    )


def test_preflight_runs_or_names_every_blocking_check():
    recipe = _preflight_recipe()
    for job, marker in sorted(_PREFLIGHT_MIRROR.items()):
        if marker is None:
            assert job in recipe, (
                f"preflight does not mirror {job} and does not say so. Name it in the closing "
                "note, the way test:modelzoo is named."
            )
        else:
            assert marker in recipe, (
                f"preflight has no step matching {job!r} (expected the banner to contain "
                f"{marker!r}). Its docstring claims to mirror every blocking CI job."
            )
