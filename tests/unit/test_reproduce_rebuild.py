"""ADR 0038 clause 2, the two gaps the Status paragraph named: *rebuild* the environment, and
*verify* the recorded container image digest.

`exa reproduce run --execute` used to compare a lockfile hash and diff a package set against
whatever interpreter it happened to be running on, and it printed the recorded image digest
without ever asking the local runtime about it. Both are now real checks:

* ``--rebuild-env`` materialises the bundle's package set into a **fresh venv** (``uv venv`` +
  ``uv pip install``) and the training subprocess runs on *that* interpreter. The tests below
  build a real venv — offline, from a wheelhouse of two hand-built wheels, so nothing here
  needs the network or a package index and no opt-in gate is required.
* the image digest resolves to ``verified`` / ``mismatch`` / ``absent`` / ``unverifiable``, and
  the first two fail the ``env`` step while the third is reported rather than passed off as a
  success.

The last test is the regression guard: with ``--rebuild-env`` off and no digest recorded, the
``env`` step's status *and* its detail string are exactly what they were before this change.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))

PROBE_NAME, PROBE_VERSION, PROBE_MODULE = "exarepro-probe", "0.0.1", "exarepro_probe"

# Runs inside the rebuilt venv: it can only import the probe package if the rebuild really
# happened, and it refuses to be the caller's interpreter.
TRAIN = (
    "import json, os, sys\n"
    "import exarepro_probe\n"
    "want = os.environ['EXAMLOPS_REPRO_PYTHON']\n"
    "assert sys.executable == want, f'ran on {sys.executable}, expected {want}'\n"
    "assert exarepro_probe.VALUE == 'rebuilt'\n"
    "open(os.environ['EXAMLOPS_REPRO_WITNESS'], 'w').write(sys.executable)\n"
    "print('EXAMLOPS_REPRO_METRICS=' + json.dumps({'rmse': 5.0}))\n"
)


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


def _wheel(directory: Path, name: str, version: str, module: str, body: str) -> Path:
    """A minimal, valid pure-python wheel — no build backend, no index, no network."""
    directory.mkdir(parents=True, exist_ok=True)
    whl = directory / f"{module}-{version}-py3-none-any.whl"
    dist = f"{module}-{version}.dist-info"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr(f"{module}.py", body)
        z.writestr(f"{dist}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        z.writestr(
            f"{dist}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        z.writestr(f"{dist}/RECORD", "")
    return whl


@pytest.fixture
def wheelhouse(tmp_path):
    house = tmp_path / "wheelhouse"
    _wheel(house, PROBE_NAME, PROBE_VERSION, PROBE_MODULE, "VALUE = 'rebuilt'\n")
    return house


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_SIGNING_KEY", "k")
    monkeypatch.setenv("EXAMLOPS_REPRO_WITNESS", str(tmp_path / "witness.txt"))
    from examlops import platform_db

    platform_db.init_db()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "uv.lock").write_text("lock-a\n")
    (repo / "train.py").write_text(TRAIN)
    # A committed no-op trainer for the cases that are not about the interpreter: the `code`
    # step runs everything in a checkout of the recorded commit, so an uncommitted script is
    # simply not there.
    (repo / "plain.py").write_text("print('EXAMLOPS_REPRO_METRICS={\"rmse\": 5.0}')\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "init")
    monkeypatch.chdir(repo)
    return repo


def _bundle(*, packages=None, image_digest=None, metrics=None):
    """Store a bundle whose manifest we fully control (the captured package set is replaced)."""
    from examlops import platform_db
    from examlops.reproducibility import _canonical_hash, build_bundle

    b = build_bundle(
        "M", "1", metrics=metrics or {"rmse": 5.0}, seeds={"global": 7}, image_digest=image_digest
    )
    m = dict(b.manifest)
    if packages is not None:
        m["environment"] = dict(m["environment"], packages=dict(packages))
    platform_db.store_repro_bundle("M", "1", m, _canonical_hash(m))
    return m


def _run(repo, **kw):
    from examlops.reproducibility.execute import execute_reproduction

    kw.setdefault("train_cmd", [sys.executable, "train.py"])
    return execute_reproduction("M", "1", repo=repo, **kw)


def _status(res):
    return {s.step: s.status for s in res.steps}


def _detail(res, step):
    return next(s.detail for s in res.steps if s.step == step)


# --------------------------------------------------------------------------- rebuild: the venv


def test_rebuild_builds_a_real_venv_and_training_runs_in_it(env, wheelhouse, tmp_path):
    """The recorded set is installed into a fresh venv and *that* interpreter runs training."""
    _bundle(packages={PROBE_NAME: PROBE_VERSION})
    venv = tmp_path / "rebuilt"
    res = _run(
        env,
        train_cmd=["python", "train.py"],  # resolved through the rebuilt venv's PATH
        rebuild_env=True,
        venv_dir=venv,
        rebuild_env_args=["--no-index", "--find-links", str(wheelhouse)],
    )
    assert res.ok, [(s.step, s.status, s.detail) for s in res.steps]
    assert _status(res)["env"] == "ok" and _status(res)["train"] == "ok"

    python = venv / "bin" / "python"
    assert python.exists(), "uv venv did not produce an interpreter"
    assert res.env.rebuilt is True
    assert res.env.python == str(python) and res.env.venv == str(venv)
    # The subprocess wrote down the interpreter it actually ran on.
    assert (tmp_path / "witness.txt").read_text().strip() == str(python)
    assert str(python) in _detail(res, "train")
    # The probe package exists only in the rebuilt venv, never in the caller's.
    assert (
        subprocess.run([str(python), "-c", "import exarepro_probe"], capture_output=True).returncode
        == 0
    )
    with pytest.raises(ImportError):
        __import__(PROBE_MODULE)
    # The caller's interpreter was not the one used, and nothing was installed into it.
    assert res.env.python != sys.executable


def test_rebuild_never_touches_the_callers_environment(env, wheelhouse, tmp_path):
    from examlops.reproducibility import capture_packages

    before = capture_packages()
    _bundle(packages={PROBE_NAME: PROBE_VERSION})
    assert _run(
        env,
        train_cmd=["python", "train.py"],
        rebuild_env=True,
        venv_dir=tmp_path / "rebuilt",
        rebuild_env_args=["--no-index", "--find-links", str(wheelhouse)],
    ).ok
    assert capture_packages() == before
    assert PROBE_NAME not in before


def test_unsatisfiable_package_set_fails_naming_the_packages(env, wheelhouse, tmp_path):
    _bundle(packages={PROBE_NAME: PROBE_VERSION, "totally-absent-pkg": "9.9.9"})
    res = _run(
        env,
        rebuild_env=True,
        venv_dir=tmp_path / "rebuilt",
        rebuild_env_args=["--no-index", "--find-links", str(wheelhouse)],
    )
    assert not res.ok
    assert _status(res)["env"] == "failed"
    assert _status(res)["train"] == "not_run" and _status(res)["compare"] == "not_run"
    assert res.env.unsatisfied == ["totally-absent-pkg"]
    assert "totally-absent-pkg" in _detail(res, "env")
    assert res.env.rebuilt is False
    assert not (tmp_path / "witness.txt").exists(), "training ran despite an unresolvable env"


def test_an_empty_recorded_package_set_cannot_be_rebuilt(env, tmp_path):
    _bundle(packages={})
    res = _run(env, rebuild_env=True, venv_dir=tmp_path / "rebuilt")
    assert _status(res)["env"] == "failed"
    assert "nothing to rebuild" in _detail(res, "env")


def test_workspace_local_distributions_are_skipped_not_failed(wheelhouse, tmp_path):
    """`examlops` is in no index; it comes from the checkout, and that is said out loud."""
    from examlops.reproducibility.rebuild import rebuild_environment

    res = rebuild_environment(
        {"examlops": "0.62.0", PROBE_NAME: PROBE_VERSION},
        tmp_path / "v",
        recorded_python=".".join(str(p) for p in sys.version_info[:3]),
        extra_args=["--no-index", "--find-links", str(wheelhouse)],
    )
    assert res.ok, res.detail
    assert res.skipped_local == ["examlops"] and res.unsatisfied == []
    assert "workspace-local" in res.detail


def test_rebuild_refuses_when_uv_is_missing(monkeypatch, tmp_path):
    from examlops.reproducibility import rebuild as rebuild_mod

    monkeypatch.setattr(rebuild_mod.shutil, "which", lambda _n: None)
    res = rebuild_mod.rebuild_environment({PROBE_NAME: PROBE_VERSION}, tmp_path / "v")
    assert res.ok is False and res.python is None
    assert "uv is not on PATH" in res.detail
    assert not (tmp_path / "v").exists()


def test_unsatisfied_names_survive_uvs_line_wrapping():
    from examlops.reproducibility.rebuild import unsatisfied_from_output

    wrapped = (
        "  x No solution found when resolving dependencies:\n"
        "  '-> Because no-such-pkg-xyz was not found in the provided package locations\n"
        "      and you require\n      no-such-pkg-xyz\n      ==9.9.9, we can conclude that\n"
        "      your requirements are unsatisfiable.\n"
    )
    recorded = {"no-such-pkg-xyz": "9.9.9", "innocent": "1.0"}
    assert unsatisfied_from_output(wrapped, recorded) == ["no-such-pkg-xyz"]
    # A name uv never named, and a name that is not in the recorded set, are both ignored.
    assert unsatisfied_from_output("stranger==1.0", recorded) == []


def test_the_probe_pass_names_offenders_when_uv_says_nothing_useful(tmp_path):
    """Fallback path: uv failed with text naming no recorded package, so each is resolved alone."""
    from examlops.reproducibility import rebuild as rebuild_mod

    calls: list[list[str]] = []

    def runner(args, timeout):
        args = list(args)
        calls.append(args)
        if args[1] == "venv":
            Path(args[-1]).joinpath("bin").mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1:3] == ["pip", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[1:3] == ["pip", "install"]:
            if "--dry-run" in args:
                bad = args[-1].startswith("bad-")
                return subprocess.CompletedProcess(args, 1 if bad else 0, "", "nope")
            return subprocess.CompletedProcess(args, 1, "", "error: something opaque\n")
        return subprocess.CompletedProcess(args, 0, "3.12.3", "")

    res = rebuild_mod.rebuild_environment(
        {"bad-one": "1", "good-one": "2", "bad-two": "3"},
        tmp_path / "v",
        uv_bin="/usr/bin/uv",
        runner=runner,
    )
    assert res.ok is False
    assert res.unsatisfied == ["bad-one", "bad-two"]
    assert sum(1 for c in calls if "--dry-run" in c) == 3


# ------------------------------------------------------------------- image digest verification


def _docker(version_rc=0, inspect=(0, "", "")):
    def runner(args, timeout):
        args = list(args)
        if "version" in args:
            return subprocess.CompletedProcess(
                args, version_rc, "29.7.2" if version_rc == 0 else "", "cannot connect"
            )
        rc, out, err = inspect
        return subprocess.CompletedProcess(args, rc, out, err)

    return runner


def test_digest_statuses_are_distinct():
    from examlops.reproducibility import image as img

    named = "ghcr.io/mskazemi/examlops-agent:v1@sha256:" + "a" * 64

    verified = img.verify_image_digest(
        named,
        docker_bin="/usr/bin/docker",
        runner=_docker(inspect=(0, f'sha256:{"c" * 64}|["ghcr.io/x/y@sha256:{"a" * 64}"]', "")),
    )
    assert verified.status == img.STATUS_VERIFIED and not verified.failing

    mismatch = img.verify_image_digest(
        named,
        docker_bin="/usr/bin/docker",
        runner=_docker(inspect=(0, f'sha256:{"c" * 64}|["ghcr.io/x/y@sha256:{"b" * 64}"]', "")),
    )
    assert mismatch.status == img.STATUS_MISMATCH and mismatch.failing
    assert "sha256:aaaa" in mismatch.detail

    absent = img.verify_image_digest(
        named,
        docker_bin="/usr/bin/docker",
        runner=_docker(inspect=(1, "", "Error response from daemon: No such image: x")),
    )
    assert absent.status == img.STATUS_ABSENT and absent.failing

    no_daemon = img.verify_image_digest(
        named, docker_bin="/usr/bin/docker", runner=_docker(version_rc=1)
    )
    assert no_daemon.status == img.STATUS_UNVERIFIABLE and not no_daemon.failing

    assert img.verify_image_digest(None).status == img.STATUS_UNCHECKED
    assert len({verified.status, mismatch.status, absent.status, no_daemon.status}) == 4


def test_no_docker_binary_is_unverifiable_not_a_pass(monkeypatch):
    from examlops.reproducibility import image as img

    monkeypatch.setattr(img.shutil, "which", lambda _n: None)
    got = img.verify_image_digest("sha256:" + "a" * 64)
    assert got.status == img.STATUS_UNVERIFIABLE and not got.failing
    assert "no docker binary" in got.detail


def test_a_bare_digest_present_locally_verifies():
    from examlops.reproducibility import image as img

    digest = "sha256:" + "d" * 64
    got = img.verify_image_digest(
        digest, docker_bin="/usr/bin/docker", runner=_docker(inspect=(0, f"{digest}|[]", ""))
    )
    assert got.status == img.STATUS_VERIFIED


def test_split_reference():
    from examlops.reproducibility.image import split_reference

    assert split_reference("sha256:" + "a" * 64) == (None, "sha256:" + "a" * 64)
    assert split_reference("ghcr.io/x/y:v1@sha256:" + "a" * 64) == (
        "ghcr.io/x/y:v1",
        "sha256:" + "a" * 64,
    )
    assert split_reference("ghcr.io/x/y:v1") == ("ghcr.io/x/y:v1", None)


# ---------------------------------------------------- the digest verdict reaches the env step


def _patch_digest(monkeypatch, check):
    from examlops.reproducibility import execute as ex

    monkeypatch.setattr(ex, "verify_image_digest", lambda _d, **_k: check)


def test_a_mismatched_digest_fails_the_env_step(env, monkeypatch):
    from examlops.reproducibility import image as img

    _bundle(image_digest="ghcr.io/x/y:v1@sha256:" + "a" * 64)
    _patch_digest(monkeypatch, img.DigestCheck(img.STATUS_MISMATCH, "digest differs"))
    res = _run(env, train_cmd=[sys.executable, "-c", "pass"])
    assert _status(res)["env"] == "failed" and not res.ok
    assert _status(res)["train"] == "not_run"
    assert res.env.image_status == img.STATUS_MISMATCH


def test_an_absent_image_fails_but_allow_env_drift_downgrades_it(env, monkeypatch):
    from examlops.reproducibility import image as img

    _bundle(image_digest="ghcr.io/x/y:v1@sha256:" + "a" * 64)
    _patch_digest(monkeypatch, img.DigestCheck(img.STATUS_ABSENT, "not present locally"))
    assert _status(_run(env, train_cmd=[sys.executable, "-c", "pass"]))["env"] == "failed"
    allowed = _run(env, allow_env_drift=True, train_cmd=[sys.executable, "plain.py"])
    assert _status(allowed)["env"] == "drift_allowed"
    assert allowed.env.image_status == img.STATUS_ABSENT


def test_an_unverifiable_digest_does_not_fail_but_is_reported(env, monkeypatch, tmp_path):
    from examlops.reproducibility import image as img

    _bundle(image_digest="ghcr.io/x/y:v1@sha256:" + "a" * 64)
    _patch_digest(monkeypatch, img.DigestCheck(img.STATUS_UNVERIFIABLE, "no reachable daemon"))
    res = _run(env, train_cmd=[sys.executable, "plain.py"])
    assert res.ok and _status(res)["env"] == "ok"
    assert res.env.image_status == img.STATUS_UNVERIFIABLE
    # Not silent: it is in the step detail the CLI prints, and in the machine-readable result.
    assert "unverifiable" in _detail(res, "env")


def test_a_verified_digest_is_reported_on_a_passing_step(env, monkeypatch):
    from examlops.reproducibility import image as img

    _bundle(image_digest="ghcr.io/x/y:v1@sha256:" + "a" * 64)
    _patch_digest(monkeypatch, img.DigestCheck(img.STATUS_VERIFIED, "matches"))
    res = _run(env, train_cmd=[sys.executable, "plain.py"])
    assert res.ok and res.env.image_status == img.STATUS_VERIFIED
    assert "image digest verified" in _detail(res, "env")


def test_cli_exposes_the_rebuild_flag_and_the_environment_block(env):
    import json

    from typer.testing import CliRunner

    from examlops.cli.main import app

    _bundle()
    r = CliRunner().invoke(
        app,
        [
            "--json",
            "reproduce",
            "run",
            "M",
            "1",
            "--execute",
            "--train-cmd",
            f"{sys.executable} plain.py",
        ],
    )
    assert r.exit_code == 0, r.output
    block = json.loads(r.stdout)["environment"]
    assert block["rebuilt"] is False and block["image_digest_status"] == "unchecked"
    assert block["python"] == sys.executable
    help_text = CliRunner().invoke(app, ["reproduce", "run", "--help"]).output
    assert "--rebuild-env" in help_text


# ------------------------------------------------------------------------------- regression


def test_the_no_rebuild_path_is_byte_identical(env):
    """Without --rebuild-env and with no digest recorded, the env step says what it always said."""
    from examlops.reproducibility import capture_packages

    pkgs = capture_packages()
    m = _bundle(packages=pkgs)
    res = _run(env, train_cmd=[sys.executable, "plain.py"])
    want = m["environment"]["lock_sha256"]
    assert res.ok
    assert _status(res) == {
        "code": "ok",
        "dataset": "skipped",
        "env": "ok",
        "train": "ok",
        "compare": "ok",
    }
    assert _detail(res, "env") == (
        f"uv.lock sha256 matches recorded {str(want)[:12]}; {len(pkgs)} packages compared"
    )
    assert res.env.rebuilt is False and res.env.python == sys.executable
    assert res.env.image_status == "unchecked" and res.env.unsatisfied == []


def test_the_no_rebuild_uncaptured_package_set_message_is_unchanged(env):
    m = _bundle(packages={})
    res = _run(env, train_cmd=[sys.executable, "plain.py"])
    want = m["environment"]["lock_sha256"]
    assert _detail(res, "env") == (
        f"uv.lock sha256 matches recorded {str(want)[:12]}"
        " (package set not captured — package-level check not possible)"
    )
