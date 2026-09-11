"""`release-verify.yml` + `platform/ci/verify_release.sh` — verify what a release published.

release.yml proves each artifact while it builds it; this pair fetches everything back from where
it was published and verifies it the way a user would. The first published releases had three
defects that only that view shows, all in v0.54.0 — its provenance attested `dist/.gitignore`, its
dashboard image could not import `examlops`, and its Compose bundle's MLflow was OOM-killed on
every start — and the script, run against v0.54.0, fails on exactly those three. These tests keep
the wiring and the fail-closed behaviour from eroding; the network half is exercised by the
workflow itself.
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
        "compose: the published bundle starts, every health check passes",
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


# A `docker` that answers only the Compose calls the bundle check makes, from the environment:
# UP_FAIL makes `compose up -d` fail with that message; PS_STATES is what `compose ps` prints.
_FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
case "$*" in
  "compose pull -q") exit 0 ;;
  "compose up -d") if [ -n "$UP_FAIL" ]; then echo "$UP_FAIL" >&2; exit 1; fi; exit 0 ;;
  "compose ps"*) printf '%b' "$PS_STATES"; exit 0 ;;
  "compose down"*) exit 0 ;;
esac
exit 1
"""


def _run_compose(tmp_path: Path, states: str, up_fail: str = "") -> tuple[str, list[str], str]:
    """The bundle check against a fake bundle and a fake docker; returns stdout, the docker
    calls it made, and the .env it wrote."""
    work = tmp_path / "work"
    bundle = tmp_path / "src" / "examlops-compose-9.9.9"
    bundle.mkdir(parents=True)
    install = bundle / "install.sh"
    install.write_text(
        "#!/bin/sh\nprintf 'EXAMLOPS_VERSION=9.9.9\\nCOMPOSE_PROFILES=minio\\n' > .env\n"
    )
    install.chmod(0o755)
    (bundle / "env.template").write_text(
        "EXAMLOPS_PORT_DASHBOARD=18099\nEXAMLOPS_PORT_MLFLOW=15000\n"
    )
    work.mkdir()
    subprocess.run(
        [
            "tar",
            "-czf",
            str(work / "examlops-compose-9.9.9.tar.gz"),
            "-C",
            str(bundle.parent),
            bundle.name,
        ],
        check=True,
    )
    fakes = tmp_path / "bin"
    fakes.mkdir()
    for tool in ("gh", "cosign", "oras", "helm", "python3", "curl"):
        (fakes / tool).write_text("#!/bin/sh\nexit 1\n")
        (fakes / tool).chmod(0o755)
    (fakes / "docker").write_text(_FAKE_DOCKER)
    (fakes / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{fakes}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "VERIFY_OFFLINE": "0",
        "VERIFY_POLL_SECONDS": "0",
        "VERIFY_WORKDIR": str(work),
        "DOCKER_LOG": str(log),
        "PS_STATES": states,
        "UP_FAIL": up_fail,
    }
    result = subprocess.run(
        [str(SCRIPT), "v9.9.9"], capture_output=True, text=True, env=env, timeout=120, check=False
    )
    dotenv = (work / "compose" / bundle.name / ".env").read_text()
    return result.stdout, log.read_text().splitlines(), dotenv


def test_the_bundle_check_passes_a_healthy_stack_in_its_own_project_and_ports(tmp_path: Path):
    # A one-shot job that exited 0 and a service without a health check are both fine.
    out, calls, dotenv = _run_compose(
        tmp_path, "mlflow|running|healthy|0\\ns3-init|exited||0\\ngrafana|running||0\\n"
    )
    assert "PASS  compose: the published bundle starts, every health check passes" in out, out
    assert "compose down -v --remove-orphans" in calls, "the stack must be torn down"
    assert "EXAMLOPS_PROJECT_NAME=examlops-verify-" in dotenv
    assert "COMPOSE_PROFILES=minio,monitoring" in dotenv
    ports = [line for line in dotenv.splitlines() if line.startswith("EXAMLOPS_PORT_")]
    assert len(ports) == 2 and not any(p.endswith(("=18099", "=15000")) for p in ports), ports


def test_the_bundle_check_gives_up_on_a_crash_loop(tmp_path: Path):
    # What the v0.54.0 bundle did: MLflow OOM-killed on every start, its dependants never created.
    out, calls, _ = _run_compose(
        tmp_path,
        "mlflow|restarting||0\\ndashboard|created||0\\ns3-init|exited||0\\n",
        up_fail="dependency failed to start: container examlops-mlflow-1 is unhealthy",
    )
    assert "FAIL  compose: the published bundle starts" in out, out
    assert "mlflow|restarting" in out and "dependency failed to start" in out, out
    polls = [c for c in calls if c.startswith("compose ps")]
    assert len(polls) == 3, f"stop after 3 crash-looping polls, not the full timeout: {len(polls)}"
    assert "compose down -v --remove-orphans" in calls


def test_the_bundle_check_fails_a_one_shot_job_that_exited_with_an_error(tmp_path: Path):
    out, _, _ = _run_compose(tmp_path, "mlflow|running|healthy|0\\ns3-init|exited||1\\n")
    assert "FAIL  compose: the published bundle starts" in out, out
    assert "s3-init|exited||1" in out


def test_the_script_refuses_anything_but_a_release_tag(tmp_path: Path):
    for bad in ("main", "v1.2", "v1.2.3; rm -rf /", "../v1.2.3"):
        result = _run(tmp_path, bad)
        assert result.returncode == 2, (bad, result.stdout)
    assert os.listdir(tmp_path / "bin"), "sanity: the fakes were in place"
