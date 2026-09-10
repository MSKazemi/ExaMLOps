"""`.github/workflows/security.yml` — every scanner is a named gate or a named report (ADR 0129).

The failure this guards against is not a missing scanner but a dishonest one: a job everyone
believes blocks merges that `security-ok` never waits for, a report job that silently became a
gate and reddens every pull request for a reason nobody chose, a scanner image that moves under
a tag, or a secret-scan exception nobody can explain.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"
GITLEAKS_IGNORE = ROOT / ".github" / ".gitleaksignore"

DOC = yaml.safe_load(WORKFLOW.read_text())
JOBS: dict = DOC["jobs"]
TRIGGERS = DOC.get("on", DOC.get(True))
AGGREGATE = "security-ok"


def test_every_job_is_either_a_gate_or_a_named_report():
    """A gate is in security-ok's needs; anything else must say `(report)` in its name."""
    gates = set(JOBS[AGGREGATE]["needs"])
    for name, job in JOBS.items():
        if name == AGGREGATE:
            continue
        is_report = "(report)" in job.get("name", "")
        assert (name in gates) != is_report, (
            f"{name}: either list it in {AGGREGATE}.needs (a gate) or name it '(report)' — not both, not neither"
        )


def test_the_aggregate_fails_on_any_gate_that_did_not_succeed():
    agg = JOBS[AGGREGATE]
    assert agg["if"].replace(" ", "") == "${{always()}}", "must run even when a gate failed"
    script = agg["steps"][-1]["run"]
    assert 'select(.value.result != "success")' in script and "exit 1" in script


def test_scanner_images_are_pinned_by_digest():
    images = {k: v for k, v in DOC["env"].items() if k.endswith("_IMAGE")}
    assert images, "no scanner images declared"
    for key, ref in images.items():
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), f"{key} is not pinned by digest: {ref}"


def test_secret_scan_reads_the_whole_history():
    checkout = JOBS["secrets"]["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0, "a shallow clone hides every deleted secret"
    gitleaks = next(s for s in JOBS["secrets"]["steps"] if "gitleaks" in s.get("name", ""))
    assert "--exit-code 1" in gitleaks["run"]
    assert ".github/.gitleaksignore" in gitleaks["run"]


def test_every_gitleaks_exception_is_a_fingerprint_under_a_reason():
    lines = GITLEAKS_IGNORE.read_text().splitlines()
    fingerprints = [ln for ln in lines if ln and not ln.startswith("#")]
    assert fingerprints, "empty ignore file: delete it rather than keep a placeholder"
    assert len(fingerprints) == len(set(fingerprints)), "duplicate fingerprint"
    reason_for: str | None = None
    for ln in lines:
        if ln.startswith("# ") and " — " in ln:
            reason_for = ln[2:].split(" — ", 1)[0]
        elif ln and not ln.startswith("#"):
            commit, path, rule, line = ln.split(":")
            assert re.fullmatch(r"[0-9a-f]{40}", commit), f"not a commit sha: {ln}"
            assert line.isdigit() and rule, f"not commit:file:rule:line: {ln}"
            assert reason_for == path, f"{ln} is not under a '# {path} — <reason>' line"


def test_scanned_paths_exist():
    for path in DOC["env"]["PY_SOURCES"].split():
        assert (ROOT / path).is_dir(), f"bandit scans a directory that does not exist: {path}"
    script = next(s for s in JOBS["secrets"]["steps"] if s.get("name") == "exa secrets scan")["run"]
    targets = re.search(r"for target in ([^;]+);", script)
    assert targets
    for target in targets.group(1).split():
        assert (ROOT / target).is_dir(), target


def test_runs_on_changes_and_on_a_schedule():
    """Advisories appear without a commit here; only a schedule notices them."""
    assert {"push", "pull_request", "schedule"} <= set(TRIGGERS)
    assert TRIGGERS["schedule"], "weekly scan missing"


def test_token_is_empty_by_default():
    assert DOC["permissions"] == {}
    for name, job in JOBS.items():
        assert "permissions" in job, f"{name} must declare the permissions it needs"
        assert not any(
            v == "write" for k, v in job["permissions"].items() if k != "security-events"
        ), f"{name}: a scanner needs to write only its SARIF report"
