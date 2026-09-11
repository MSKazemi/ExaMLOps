"""`.github/workflows/release.yml` — the structure a release depends on (ADR 0129).

A release workflow runs rarely and publishes irreversibly, so its mistakes surface at the worst
moment: a tag that published half its artifacts, a chart whose images were never pushed, a
private-repo tag that pushed to the public registry. Each test here names one such outcome.
Linting (actionlint, zizmor) and SHA pinning are enforced for every workflow elsewhere; this
file holds the release-specific invariants.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
SECURITY = ROOT / ".github" / "workflows" / "security.yml"
CHART = ROOT / "platform" / "infra" / "helm" / "examlops"

DOC = yaml.safe_load(WORKFLOW.read_text())
JOBS: dict = DOC["jobs"]
# PyYAML reads the bare key `on` as the boolean True.
TRIGGERS = DOC.get("on", DOC.get(True))
PUBLIC_REPO = "github.repository == 'MSKazemi/ExaMLOps'"


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _ancestors(name: str) -> set[str]:
    seen: set[str] = set()
    stack = _needs(JOBS[name])
    while stack:
        dep = stack.pop()
        if dep not in seen:
            seen.add(dep)
            stack.extend(_needs(JOBS[dep]))
    return seen


def _images() -> list[dict]:
    return JOBS["images"]["strategy"]["matrix"]["include"]


def test_runs_on_version_tags_and_on_demand_only():
    assert set(TRIGGERS) == {"push", "workflow_dispatch"}
    assert set(TRIGGERS["push"]) == {"tags"}, "a release must never run on a branch push"
    assert all(t.startswith("v") for t in TRIGGERS["push"]["tags"])


def test_nothing_publishes_from_the_private_repository():
    """The private repo tracks .github/ too; its tags must not reach GHCR or PyPI.

    Only `verify` carries the guard — every other job needs it (directly or transitively), and
    GitHub skips a job whose needed job was skipped unless its `if:` calls always()/failure().
    """
    assert JOBS["verify"]["if"] == PUBLIC_REPO
    for name, job in JOBS.items():
        if name == "verify":
            continue
        assert "verify" in _ancestors(name), f"{name} does not depend on the repository guard"
        condition = str(job.get("if", ""))
        assert not re.search(r"\b(always|failure|cancelled)\(\)", condition), (
            f"{name}: `if: {condition}` would run even when verify was skipped"
        )


def test_token_is_empty_by_default_and_each_job_asks_by_name():
    assert DOC["permissions"] == {}
    for name, job in JOBS.items():
        assert "permissions" in job, f"{name} must declare the permissions it needs"
        assert job["permissions"].get("contents") != "write" or name == "github-release", (
            f"{name}: only the job that creates the release may write repository contents"
        )


def test_pypi_goes_last_from_a_protected_environment():
    """A PyPI version can never be re-uploaded: publish only once everything else exists."""
    pypi = JOBS["pypi"]
    assert pypi["environment"]["name"] == "pypi"
    assert pypi["permissions"] == {"id-token": "write"}, "Trusted Publishing needs OIDC only"
    assert {"github-release", "chart", "images", "python-dist"} <= _ancestors("pypi")
    step_uses = [s.get("uses", "") for s in pypi["steps"]]
    assert any(u.startswith("pypa/gh-action-pypi-publish@") for u in step_uses)
    assert not any("password" in (s.get("with") or {}) for s in pypi["steps"]), (
        "no API token: the publish must go through Trusted Publishing"
    )


def test_chart_waits_for_its_images():
    """A chart pushed before its images is a chart nobody can install."""
    assert "images" in _needs(JOBS["chart"])


def test_every_image_builds_from_a_dockerfile_that_exists():
    for image in _images():
        context = ROOT / image.get("context", ".")
        dockerfile = ROOT / image["dockerfile"]
        assert context.is_dir(), f"{image['name']}: context {context} missing"
        assert dockerfile.is_file(), f"{image['name']}: {dockerfile} missing"
        assert dockerfile.resolve().is_relative_to(ROOT)


def test_image_names_are_unique_and_registry_safe():
    names = [i["name"] for i in _images()]
    assert len(names) == len(set(names))
    for name in names:
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), name


def test_the_chart_can_pull_every_image_it_deploys():
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    published = {f"examlops-{i['name']}" for i in _images()}
    deployed = {
        tier["image"]["repository"]
        for tier in values.values()
        if isinstance(tier, dict) and isinstance(tier.get("image"), dict)
    }
    assert deployed, "found no image repositories in the chart values"
    assert deployed <= published, (
        f"chart deploys images the release never pushes: {deployed - published}"
    )


def test_images_are_quarantined_until_scanned():
    """Push by digest, scan the digest, then tag — never tag first."""
    steps = JOBS["images"]["steps"]
    order = [s.get("id") or s.get("name", "") for s in steps]
    build = next(s for s in steps if s.get("id") == "build")
    assert "push-by-digest=true" in build["with"]["outputs"]
    scan = next(i for i, s in enumerate(steps) if "blocking" in s.get("name", ""))
    tag = next(i for i, s in enumerate(steps) if s.get("name") == "Tag the scanned digest")
    sign = next(i for i, s in enumerate(steps) if "cosign keyless" in s.get("name", ""))
    assert order.index("build") < scan < tag < sign
    gate = steps[scan]["with"]
    assert str(gate["exit-code"]) == "1" and gate["severity"] == "CRITICAL"


def test_images_carry_sbom_and_provenance():
    build = next(s for s in JOBS["images"]["steps"] if s.get("id") == "build")["with"]
    assert build["sbom"] is True
    assert build["provenance"] == "mode=max"


def test_release_notes_come_from_the_changelog_extractor():
    run = "\n".join(s.get("run", "") for s in JOBS["verify"]["steps"])
    assert "release_version.py tag" in run
    assert "release_version.py check" in run
    assert "release_version.py notes" in run


def test_the_release_is_created_only_if_absent():
    """The owner's release loop may have created it by hand; a re-run must not fail on it."""
    run = "\n".join(s.get("run", "") for s in JOBS["github-release"]["steps"])
    assert "gh release view" in run and "--clobber" in run


def test_every_required_check_is_an_aggregate_job_that_exists():
    """The release refuses a commit unless these passed; a renamed job would block forever."""
    required = DOC["env"]["REQUIRED_CHECKS"].split()
    assert required == ["ci-ok", "security-ok"]
    names: set[str] = set()
    for wf in (CI, SECURITY):
        jobs = yaml.safe_load(wf.read_text())["jobs"]
        names |= set(jobs) | {j.get("name") for j in jobs.values()}
    missing = [c for c in required if c not in names]
    assert not missing, f"no workflow defines a job named {missing}"


def test_run_scripts_never_expand_untrusted_input():
    """`${{ inputs.* }}` or `${{ github.event.* }}` inside a script is shell injection."""
    for name, job in JOBS.items():
        for step in job.get("steps", []):
            script = step.get("run", "")
            assert not re.search(r"\$\{\{\s*(inputs|github\.event)\.", script), (
                f"{name}/{step.get('name')}: pass untrusted input through env:, not ${{{{ }}}}"
            )


def test_the_compose_bundle_ships_every_file_it_needs():
    """The pull-only install is assembled from files at the tag; a renamed one breaks the tar."""
    run = "\n".join(s.get("run", "") for s in JOBS["github-release"]["steps"])
    listed = re.search(r"install/\{([^}]+)\}", run)
    assert listed, "the release no longer assembles the compose bundle"
    install = ROOT / "platform" / "infra" / "docker-compose" / "install"
    for name in listed.group(1).split(","):
        assert (install / name).is_file(), f"bundle file missing: {name}"
    assert "VERSION" in run, "install.sh init reads the release version from VERSION"


def test_release_assets_carry_a_signature_and_slsa_provenance():
    """One cosign signature over SHA256SUMS covers every asset; the provenance ships beside them.

    Also what OpenSSF Scorecard's Signed-Releases check looks for (*.sigstore.json, *.intoto.jsonl).
    """
    release = "\n".join(s.get("run", "") for s in JOBS["github-release"]["steps"])
    assert "cosign sign-blob" in release and "SHA256SUMS.sigstore.json" in release
    # SHA256SUMS is written before it is signed, so the signature is not among the summed files.
    assert release.index("sha256sum -- *") < release.index("cosign sign-blob")
    assert JOBS["github-release"]["permissions"].get("id-token") == "write"
    dist = "\n".join(s.get("run", "") for s in JOBS["python-dist"]["steps"])
    assert ".intoto.jsonl" in dist and "dsseEnvelope" in dist
    assert "provenance-python" in release


def test_the_blocking_scan_reads_the_policy_from_the_workflow_revision():
    """Triage made after a tag must be able to re-publish that tag without moving it."""
    steps = JOBS["images"]["steps"]
    policy = next(s for s in steps if s.get("name", "").startswith("Check out the scan policy"))
    assert policy["with"]["ref"] == "${{ github.workflow_sha }}"
    assert policy["with"]["sparse-checkout"] == ".github/trivy"
    gate = next(s for s in steps if "blocking" in s.get("name", ""))
    assert gate["with"]["trivyignores"] == f"{policy['with']['path']}/.github/trivy/ignore.yaml"
    report = next(s for s in steps if s.get("name", "").startswith("Full vulnerability report"))
    assert "trivyignores" not in report["with"], "the advisory report must show every finding"


def test_every_scan_exception_is_scoped_reasoned_and_expiring():
    """An exception without an end date is how an accepted risk becomes a permanent one."""
    import datetime as _dt

    policy = yaml.safe_load((ROOT / ".github" / "trivy" / "ignore.yaml").read_text())
    entries = [(kind, e) for kind, items in policy.items() for e in items]
    assert entries, "empty policy: delete the file's entries rather than keep a placeholder"
    today = _dt.date.today()
    for kind, e in entries:
        where = f"{kind}/{e.get('id')}"
        assert e.get("paths"), f"{where}: scope it to the file(s) it covers"
        assert len(e.get("statement", "")) >= 60, f"{where}: say why it cannot be exploited"
        expiry = e.get("expired_at")
        assert isinstance(expiry, _dt.date), f"{where}: needs expired_at (yyyy-mm-dd)"
        assert expiry > today, (
            f"{where}: expired on {expiry} — fix the finding or renew with the owner"
        )
        assert (expiry - today).days <= 90, f"{where}: an exception may run at most 90 days"
