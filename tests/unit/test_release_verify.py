"""`release-verify.yml` + `platform/ci/verify_release.sh` — verify what a release published.

release.yml proves each artifact while it builds it; this pair fetches everything back from where
it was published and verifies it the way a user would. The first published releases had two
defects that only that view shows — v0.54.0's provenance attested `dist/.gitignore`, and its
dashboard image could not import `examlops` — and the script, run against v0.54.0, fails on
exactly those two. These tests keep the wiring and the fail-closed behaviour from eroding; the
network half is exercised by the workflow itself.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = yaml.safe_load((WORKFLOWS / "release.yml").read_text())
VERIFY = yaml.safe_load((WORKFLOWS / "release-verify.yml").read_text())
SCRIPT = ROOT / "platform" / "ci" / "verify_release.sh"
PUBLIC_REPO = "github.repository == 'MSKazemi/ExaMLOps'"


def _triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def _steps() -> list[dict]:
    return VERIFY["jobs"]["verify"]["steps"]


def test_a_release_verifies_what_it_published_before_pypi():
    published = RELEASE["jobs"]["published"]
    assert published["uses"] == "./.github/workflows/release-verify.yml"
    assert published["with"]["tag"] == "v${{ needs.verify.outputs.version }}"
    assert "github-release" in published["needs"], "verify only once the release exists"
    assert "published" in RELEASE["jobs"]["pypi"]["needs"], "PyPI can never be undone; verify first"
    assert published["permissions"] == {"contents": "read", "attestations": "read"}


def test_it_runs_after_every_release_on_demand_and_weekly():
    on = _triggers(VERIFY)
    assert set(on) == {"workflow_call", "workflow_dispatch", "schedule"}
    assert on["workflow_call"]["inputs"]["tag"]["required"] is True
    assert on["schedule"], "a signature or tag can vanish after release day"


def test_it_is_read_only_and_runs_only_in_the_public_repository():
    assert VERIFY["permissions"] == {}
    job = VERIFY["jobs"]["verify"]
    assert job["permissions"] == {"contents": "read", "attestations": "read"}
    assert job["if"] == PUBLIC_REPO


def test_the_tag_is_validated_and_never_expanded_into_a_script():
    for step in _steps():
        assert "${{" not in step.get("run", ""), f"step {step.get('name')!r} expands an expression"
    resolve = next(s for s in _steps() if s.get("id") == "tag")["run"]
    assert resolve.index("=~ ^v[0-9]+") < resolve.index("GITHUB_OUTPUT"), "validate before use"
    assert any(s.get("run", "").startswith("platform/ci/verify_release.sh") for s in _steps())


def test_its_tools_are_pinned_and_checksum_verified():
    env = VERIFY["env"]
    assert env["COSIGN_VERSION"].startswith("v3."), "the script's offline flags are cosign v3's"
    assert env["HELM_VERSION"] == RELEASE["env"]["HELM_VERSION"], "verify with the Helm it ships"
    installer = next(
        s for s in _steps() if s.get("uses", "").startswith("sigstore/cosign-installer@")
    )
    assert installer["with"]["cosign-release"] == "${{ env.COSIGN_VERSION }}"
    install = next(s for s in _steps() if "oras" in s.get("name", "").lower())["run"]
    assert install.count("sha256sum -c") == 2, "oras and helm are each checked against their sums"


def test_the_script_keeps_the_checks_that_caught_real_defects():
    text = SCRIPT.read_text()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "the workflow runs it directly"
    for name in (
        "provenance attests exactly the wheel and the sdist",
        "the dashboard image imports examlops",
        "no asset escapes the signed checksums",
        "air gap: nothing inside can reach the internet",
        "air gap: a signature from any other identity is rejected",
    ):
        assert name in text, f"check {name!r} is gone"


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script with every external tool replaced by one that fails."""
    fakes = tmp_path / "bin"
    fakes.mkdir(exist_ok=True)
    for tool in ("gh", "cosign", "oras", "helm", "docker", "python3", "curl"):
        fake = fakes / tool
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
    env = {
        "PATH": f"{fakes}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "VERIFY_OFFLINE": "0",
        "VERIFY_WORKDIR": str(tmp_path / "work"),
    }
    return subprocess.run(
        [str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120, check=False
    )


def test_the_script_fails_closed_and_keeps_counting(tmp_path: Path):
    result = _run(tmp_path, "v9.9.9")
    assert result.returncode == 1, result.stdout + result.stderr
    fails = [line for line in result.stdout.splitlines() if line.startswith("FAIL")]
    assert fails[0] == "FAIL  the verification tools are present"
    assert len(fails) > 5, "a failed check must not stop the ones after it"
    assert "PASS" not in result.stdout, "nothing can pass without the tools"
    assert "v9.9.9:" in result.stdout.splitlines()[-1]
    assert "checks FAILED" in result.stdout


def test_the_script_refuses_anything_but_a_release_tag(tmp_path: Path):
    for bad in ("main", "v1.2", "v1.2.3; rm -rf /", "../v1.2.3"):
        result = _run(tmp_path, bad)
        assert result.returncode == 2, (bad, result.stdout)
    assert os.listdir(tmp_path / "bin"), "sanity: the fakes were in place"
