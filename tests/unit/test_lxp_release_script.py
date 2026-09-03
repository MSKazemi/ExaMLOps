"""Guards for the node-side release script — the code that runs during an incident.

``platform/ci/lxp_release.sh`` is the only thing standing between a bad deploy and a restored
platform, and none of it was covered: it runs on a remote host, under SSH, from CI. So the
paths that matter most were the ones nobody could exercise before trusting them.

These tests run the real script against a temporary directory with a stubbed ``docker``, and
assert the properties that a rollback depends on. Three of them are regressions found while
writing this: the retention budget counting the wrong set, the ambient environment leaking
into a rollback, and ``list`` putting a human sentence on the stdout a job parses.
"""

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "platform" / "ci" / "lxp_release.sh"

pytestmark = pytest.mark.skipif(
    not shutil.which("bash") or not _SCRIPT.exists(), reason="needs bash and the release script"
)


@pytest.fixture
def node(tmp_path):
    """A fake deploy node: stubbed `docker`/`setfacl`, a legacy path, a release archive."""
    shim = tmp_path / "shim"
    shim.mkdir()
    for name in ("docker", "setfacl", "uv"):
        stub = shim / name
        # `docker` records its arguments so a test can assert *how* compose was invoked.
        stub.write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\nexit 0\n'
            if name == "docker"
            else "#!/usr/bin/env bash\nexit 0\n"
        )
        stub.chmod(0o755)

    src = tmp_path / "src" / "platform" / "infra" / "docker-compose"
    src.mkdir(parents=True)
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "src", arcname=".")

    legacy = tmp_path / "opt" / "examlops"
    (legacy / "platform" / "infra" / "docker-compose").mkdir(parents=True)

    class Node:
        def __init__(self):
            self.tmp = tmp_path
            self.legacy = legacy
            self.releases = tmp_path / "opt" / "examlops-releases"
            self.current = tmp_path / "opt" / "examlops-current"
            self.log = tmp_path / "docker.log"

        def run(self, *args, env=None, check=True):
            environ = {
                **os.environ,
                "PATH": f"{shim}:{os.environ['PATH']}",
                "DOCKER_LOG": str(self.log),
                **(env or {}),
            }
            return subprocess.run(
                ["bash", str(_SCRIPT), *args],
                capture_output=True,
                text=True,
                env=environ,
                check=check,
            )

        def deploy(self, sha, env=None):
            return self.run("deploy", str(legacy), sha, str(archive), env=env)

        def compose_calls(self):
            if not self.log.exists():
                return []
            return [ln for ln in self.log.read_text().splitlines() if ln.startswith("compose")]

        def stack_verb(self):
            """How the main stack was started: the compose call with no --profile flag.

            The -f overlay flags sit between `compose` and the verb, so this looks for the
            verb inside the line rather than at its start.
            """
            return [c for c in self.compose_calls() if "--profile" not in c]

    return Node()


def _sha(n: int) -> str:
    return f"{n:040d}"


# ── retention ────────────────────────────────────────────────────────────────


def test_retention_keeps_exactly_the_budget(node):
    """A number given to bound a volume must mean what it says."""
    for i in range(1, 9):
        node.deploy(_sha(i), env={"EXAMLOPS_KEEP_RELEASES": "3"})
    assert len(list(node.releases.iterdir())) == 3


def test_retention_never_deletes_the_active_or_the_rollback_target(node):
    """These two are what smoke:lxp and rollback:lxp restore; losing them is unrecoverable."""
    for i in (1, 2, 3):
        node.deploy(_sha(i))
    result = node.deploy(_sha(4), env={"EXAMLOPS_KEEP_RELEASES": "1"})
    remaining = {p.name for p in node.releases.iterdir()}
    assert _sha(4) in remaining, "deleted the release it had just activated"
    assert _sha(3) in remaining, "deleted the release a rollback would restore"
    # And it says so rather than reporting a budget it did not meet.
    assert "beyond the retention budget" in result.stdout


def test_a_nonsense_retention_value_prunes_nothing(node):
    """Failing open is right here: a typo must not delete releases."""
    for i in (1, 2, 3):
        node.deploy(_sha(i))
    result = node.deploy(_sha(4), env={"EXAMLOPS_KEEP_RELEASES": "not-a-number"})
    assert len(list(node.releases.iterdir())) == 4
    assert "is not a number" in result.stderr


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_marks_the_active_release(node):
    node.deploy(_sha(1))
    node.deploy(_sha(2))
    lines = node.run("list", str(node.legacy)).stdout.splitlines()
    assert lines[0].startswith("* ") and _sha(2) in lines[0]
    assert lines[1].startswith("  ") and _sha(1) in lines[1]


def test_list_puts_nothing_but_releases_on_stdout(node):
    """rollback:lxp parses this stdout to choose what to restore.

    It printed `no releases yet` there, which the job read as a release named `no` and tried
    to activate.
    """
    empty = node.tmp / "opt" / "nothing-deployed-here"
    result = node.run("list", str(empty))
    assert result.stdout.strip() == ""
    assert "no releases yet" in result.stderr


def test_list_does_not_write_to_the_node(node):
    """Asking a question must not seed state — `list` skips seed_state deliberately."""
    empty = node.tmp / "opt" / "untouched"
    node.run("list", str(empty))
    assert not (node.tmp / "opt" / "untouched-state").exists()


# ── registry pinning / rollback fidelity ─────────────────────────────────────

_REGISTRY = {
    "EXAMLOPS_IMAGE_PREFIX": "registry.example/examlops",
    "EXAMLOPS_IMAGE_TAG": "cafe1234",
}


def test_a_release_without_a_pin_builds_on_the_node(node):
    node.deploy(_sha(1))
    assert any("up --build -d" in c for c in node.stack_verb())


def test_a_pinned_release_pulls_and_never_builds_the_stack(node):
    node.deploy(_sha(1), env=_REGISTRY)
    calls = node.compose_calls()
    assert any(" pull --quiet" in c for c in calls)
    assert any("up -d --no-build" in c for c in calls)
    assert not any("up --build" in c for c in node.stack_verb())


def test_rollback_reproduces_the_release_not_the_callers_environment(node):
    """The bug this covers broke the rollback path specifically.

    `activate` read EXAMLOPS_IMAGE_PREFIX from the environment, so rolling back to a release
    created before the registry existed inherited the *current* pipeline's prefix and tried
    to pull images that were never pushed for it.
    """
    node.deploy(_sha(1))  # built on the node, no pin
    node.deploy(_sha(2), env=_REGISTRY)  # pinned
    node.log.unlink()

    # Roll back to the unpinned release from a deliberately poisoned environment.
    node.run("activate", str(node.legacy), str(node.releases / _sha(1)), env=_REGISTRY)
    calls = node.compose_calls()
    assert any("up --build -d" in c for c in node.stack_verb()), (
        "a pre-registry release was activated in pull mode — it would try to pull images "
        f"that were never pushed for it: {calls}"
    )
    assert not any("pull" in c for c in calls)


def test_rollback_to_a_pinned_release_restores_its_own_images(node):
    node.deploy(_sha(1))
    node.deploy(_sha(2), env=_REGISTRY)
    node.deploy(_sha(3))  # current release is unpinned
    node.log.unlink()

    result = node.run("activate", str(node.legacy), str(node.releases / _sha(2)))
    assert "cafe1234" in result.stdout, "did not restore the release's own image tag"
    assert any("up -d --no-build" in c for c in node.compose_calls())


def test_the_pin_is_stored_inside_the_release(node):
    node.deploy(_sha(1), env=_REGISTRY)
    pin = (node.releases / _sha(1) / ".examlops-image.env").read_text()
    assert "EXAMLOPS_IMAGE_TAG=cafe1234" in pin


# ── input validation ─────────────────────────────────────────────────────────


def test_a_release_path_outside_the_managed_roots_is_refused(node):
    node.deploy(_sha(1))
    escape = node.run("activate", str(node.legacy), "/etc", check=False)
    assert escape.returncode != 0
    assert "refusing release path" in escape.stderr


def test_an_unknown_action_is_refused(node):
    result = node.run("frobnicate", str(node.legacy), check=False)
    assert result.returncode == 2
    assert "unknown action" in result.stderr
