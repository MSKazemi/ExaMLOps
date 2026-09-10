"""Every GitHub workflow keeps the supply-chain properties the CI header promises.

`.github/workflows/ci.yml` states the rules in its header; this file is what makes them true for
every workflow in the directory, including ones added later. Each rule exists because its
absence is silent: an unpinned tag keeps working right up to the day it is rewritten (on
2026-03-19 76 of 77 `aquasecurity/trivy-action` tags were force-pushed to credential-stealing
code, GHSA-69fq-xp46-6x23), a persisted checkout token is readable by every later step, and a
job without a timeout hangs for six hours before anyone is told.

It also holds the two properties that keep branch protection honest: the single required check
`ci-ok` must depend on every job in ci.yml (a job missing from it is a job protection silently
does not require), and Dependabot must watch every manifest the repository ships.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.y*ml"))
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")
_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
_VERSION_COMMENT = re.compile(r"#\s*v?\d+(?:\.\d+)*")


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{path.name} is not a YAML mapping"
    return data


def _triggers(workflow: dict[str, Any]) -> Any:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def _jobs(path: Path) -> dict[str, dict[str, Any]]:
    return _load(path).get("jobs") or {}


def test_there_are_workflows_to_check() -> None:
    assert WORKFLOWS, "no workflow files found — the scan below would pass vacuously"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    """`owner/repo@<40-hex> # vX.Y.Z` — the SHA for integrity, the comment for Dependabot."""
    bad = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        match = _USES.match(line)
        if not match:
            continue
        ref, rest = match["ref"], match["rest"]
        if ref.startswith("./"):
            continue  # a local action is versioned with this repository
        if ref.startswith("docker://"):
            if "@sha256:" not in ref:
                bad.append(f"{path.name}:{lineno}: docker image not pinned by digest: {ref}")
            continue
        if not _PINNED.match(ref):
            bad.append(f"{path.name}:{lineno}: not pinned to a full commit SHA: {ref}")
        elif not _VERSION_COMMENT.search(rest):
            bad.append(f"{path.name}:{lineno}: SHA pin has no `# vX.Y.Z` comment: {ref}")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_checkout_drops_its_credentials(path: Path) -> None:
    bad = []
    for name, job in _jobs(path).items():
        for step in job.get("steps") or []:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                if (step.get("with") or {}).get("persist-credentials") is not False:
                    bad.append(
                        f"{path.name}: job `{name}` checks out without persist-credentials: false"
                    )
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_has_a_timeout(path: Path) -> None:
    missing = [
        name
        for name, job in _jobs(path).items()
        # a job that calls a reusable workflow cannot set one; the callee's jobs do
        if "uses" not in job and "timeout-minutes" not in job
    ]
    assert not missing, f"{path.name}: jobs without timeout-minutes: {missing}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_the_workflow_token_is_read_only_by_default(path: Path) -> None:
    """Write scopes belong to the one job that needs them, never to the whole workflow."""
    workflow = _load(path)
    assert "permissions" in workflow, (
        f"{path.name} declares no top-level `permissions`, so every job inherits the "
        "repository default, which may be read-write"
    )
    perms = workflow["permissions"]
    assert perms != "write-all", f"{path.name}: top-level permissions are write-all"
    if isinstance(perms, dict):
        writes = sorted(k for k, v in perms.items() if v == "write")
        assert not writes, (
            f"{path.name}: top-level permissions grant write on {writes}; "
            "move them to the job that needs them"
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_runs_untrusted_code_with_a_privileged_token(path: Path) -> None:
    """`pull_request_target` runs with a write token and secrets in the base repository's
    context, and a checkout of the PR head then executes a stranger's code with both."""
    triggers = _triggers(_load(path))
    names = [triggers] if isinstance(triggers, str) else list(triggers or [])
    assert "pull_request_target" not in names, f"{path.name} uses pull_request_target"


_FORCED_COLOR = ("FORCE_COLOR", "CLICOLOR_FORCE", "PY_COLORS")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_forces_colored_output(path: Path) -> None:
    """Forced color turns every captured CLI string into ANSI escapes.

    `FORCE_COLOR: "1"` in ci.yml's env (2026-09-10) failed 23 unit tests that assert on CLI text
    and the wheel smoke test's `exa --version` comparison, on the first run that carried it.
    """
    workflow = _load(path)
    scopes = [("workflow", workflow.get("env") or {})]
    for name, job in _jobs(path).items():
        scopes.append((f"job `{name}`", job.get("env") or {}))
        for step in job.get("steps") or []:
            scopes.append((f"job `{name}` step `{step.get('name', '?')}`", step.get("env") or {}))
    bad = [f"{where}: {var}" for where, env in scopes for var in _FORCED_COLOR if var in env]
    assert not bad, f"{path.name} forces colored output: {bad}"


def test_ci_ok_requires_every_job_in_ci() -> None:
    jobs = _jobs(WORKFLOW_DIR / "ci.yml")
    assert "ci-ok" in jobs, "ci.yml has no `ci-ok` aggregate job for branch protection"
    gate = jobs["ci-ok"]
    needs = gate.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    missing = sorted(set(jobs) - {"ci-ok"} - set(needs))
    unknown = sorted(set(needs) - set(jobs))
    assert not missing, f"ci-ok does not wait for {missing}, so protection never requires them"
    assert not unknown, f"ci-ok needs jobs that do not exist: {unknown}"
    assert "always()" in str(gate.get("if", "")), (
        "ci-ok must run with `if: always()`; otherwise a failed dependency SKIPS it, and a "
        "skipped required check is treated as passing by branch protection"
    )


def _dependabot() -> dict[str, Any]:
    return _load(DEPENDABOT)


def _covered(ecosystem: str) -> set[str]:
    dirs: set[str] = set()
    for update in _dependabot()["updates"]:
        if update["package-ecosystem"] == ecosystem:
            dirs.update(update.get("directories") or [update.get("directory")])
    return {d.rstrip("/") or "/" for d in dirs if d}


def _tracked_files() -> list[str]:
    """Tracked paths — the manifests the repository ships, not whatever is lying around.

    Falls back to a pruned walk where there is no git (an exported tarball)."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=60
        )
        return [p for p in out.stdout.decode().split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        skip = {".venv", "node_modules", ".git", ".git-private", "site", ".claude", "modelzoo"}
        files = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in skip]
            rel = Path(dirpath).relative_to(ROOT)
            files.extend(str(rel / f) for f in filenames)
        return files


def _manifest_dirs(pattern: str) -> set[str]:
    found = set()
    for rel in map(Path, _tracked_files()):
        if fnmatch.fnmatch(rel.name, pattern) and "node_modules" not in rel.parts:
            found.add("/" if str(rel.parent) == "." else f"/{rel.parent}")
    return found


def test_dependabot_watches_every_pinned_requirements_file() -> None:
    missing = sorted(_manifest_dirs("requirements*.txt") - _covered("pip"))
    assert not missing, f"requirements files Dependabot never updates: {missing}"


def test_dependabot_watches_every_npm_package() -> None:
    missing = sorted(_manifest_dirs("package.json") - _covered("npm"))
    assert not missing, f"package.json files Dependabot never updates: {missing}"


def test_dependabot_watches_the_uv_workspace_and_the_actions() -> None:
    assert "/" in _covered("uv"), "the root uv workspace (uv.lock) is not watched"
    assert "/" in _covered("github-actions"), "the pinned workflow actions are not watched"


def test_every_dependabot_update_waits_out_a_cooldown() -> None:
    """A brand-new release is the one most likely to be a compromised one."""
    missing = [
        u["package-ecosystem"]
        for u in _dependabot()["updates"]
        if not (u.get("cooldown") or {}).get("default-days")
    ]
    assert not missing, f"Dependabot updates without a cooldown: {missing}"


def test_every_dependabot_ignore_says_why() -> None:
    """An ignore with no reason outlives its reason — say why and what lifts it."""
    lines = DEPENDABOT.read_text().splitlines()
    bad = []
    for i, line in enumerate(lines):
        if re.match(r"^\s*-\s+dependency-name:", line):
            above = next((ln for ln in reversed(lines[:i]) if ln.strip()), "")
            if not above.strip().startswith("#"):
                bad.append(f"dependabot.yml:{i + 1}: {line.strip()}")
    assert not bad, "ignore rules without an explanatory comment:\n" + "\n".join(bad)
