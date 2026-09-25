"""ADR 0038 clause 2 — ``exa reproduce run --execute``: a real rebuild, step by step.

Five ordered steps, each a real check with a failure path and a reported outcome; the first
failing step stops the run and every later step is reported ``not_run`` (never as a pass):

1. **code**  — the bundle's recorded git commit is checked out into a detached ``git worktree``.
   An unrecorded or unreachable sha is a failure; ``HEAD`` is never substituted.
2. **dataset** — a bundle pinned to a dataplane snapshot (ADR 0130) is verified against the
   snapshot manifest and part checksums (the training-time check). Otherwise the pinned revision
   must be recorded and, when ``--data-path`` is given, the local data must hash to it
   (``versioning.content_revision``); without a path only the record is checked and the step
   says so (``skipped`` for ``--dummy`` or a bundle with no dataset).
3. **env**   — the lockfile hash recorded in the bundle is compared with the lockfile in the
   checkout, and the recorded package set with this interpreter's, package by package. Drift (or
   an uncaptured lock hash) fails unless ``allow_env_drift``. With ``rebuild_env`` the recorded
   package set is instead *materialised* into a fresh, isolated venv
   (:mod:`examlops.reproducibility.rebuild`) which step 4 then runs — a package that cannot be
   resolved fails the step by name rather than falling back to the caller's interpreter. A
   recorded container image digest is verified against the local runtime
   (:mod:`examlops.reproducibility.image`): a mismatched or absent image fails the step (unless
   ``allow_env_drift``), and an unreachable Docker daemon is reported ``unverifiable``.
4. **train** — training runs in a subprocess *inside the worktree* (the checked-out code, not the
   caller's), pinned to the dataset revision and recorded seed, and on the interpreter the result
   reports (:func:`resolve_interpreter`: the rebuilt venv's, else ``EXAMLOPS_REPRO_PYTHON``, else
   this process's — never a bare ``python`` resolved against ``PATH``), whose bin directory leads
   ``PATH``. Without a custom command this is the real ``training_flow`` against a throw-away
   MLflow store + platform DB.
5. **compare** — produced metrics vs recorded metrics within a relative tolerance. A bundle that
   records no metrics, a missing metric or a non-finite value is a divergence, not a pass.

Bit-exactness is never claimed (see ``NONDETERMINISM_CAVEAT``).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from examlops import data as platform_db
from examlops.reproducibility import (
    DEFAULT_TOLERANCE,
    NONDETERMINISM_CAVEAT,
    _canonical_hash,
    _dataplane_source,
    _file_sha256,
    _rel_diff,
    compare_packages,
    describe_package_drift,
    lakefs,
    verify_dataplane_snapshot,
)
from examlops.reproducibility import capture as cap
from examlops.reproducibility.image import STATUS_UNCHECKED as IMG_UNCHECKED
from examlops.reproducibility.image import verify_image_digest
from examlops.reproducibility.rebuild import rebuild_environment

METRICS_MARKER = "EXAMLOPS_REPRO_METRICS="
STEPS = ("code", "dataset", "env", "train", "compare")
#: Schedulers a rebuild may run on (``--scheduler``; ``recorded`` resolves to one of these).
SCHEDULERS = ("mock", "slurm", "flux")
DEFAULT_RTOL = float(cast("float", DEFAULT_TOLERANCE["rel"]))

#: Explicit interpreter override, and the name under which the interpreter that was actually
#: chosen is handed to the training subprocess.
ENV_PYTHON = "EXAMLOPS_REPRO_PYTHON"
#: argv[0] spellings a custom ``--train-cmd`` may use to mean "the reproduction's interpreter".
_BARE_PYTHON = ("python", "python3", "python.exe", "python3.exe")


def resolve_interpreter(venv: str | Path | None = None) -> str:
    """Absolute path of the interpreter a reproduction runs on.

    Order: a rebuilt venv's interpreter (``--rebuild-env`` asked for exactly that one, so it
    outranks everything) → ``EXAMLOPS_REPRO_PYTHON``, the explicit override → the interpreter
    this process is running on. Never a bare ``python`` resolved against ``PATH``: many hosts
    (this one included) ship only ``python3``, so a bare name is a reproduction that dies at the
    ``train`` step — or, worse, one that runs on an interpreter other than the one the report
    names.
    """
    if venv:
        bindir = Path(venv) / ("Scripts" if os.name == "nt" else "bin")
        for name in ("python.exe", "python") if os.name == "nt" else ("python", "python3"):
            if (bindir / name).exists():
                return str(bindir / name)
    override = (os.getenv(ENV_PYTHON) or "").strip()
    if override:
        # which() handles both a bare name and a path; an override that resolves to nothing is
        # returned as given so the failure names what the caller asked for instead of silently
        # running something else.
        return shutil.which(override) or override
    return sys.executable


# In-worktree driver: the existing training flow, metrics emitted on a marker line.
_DRIVER = (
    "import json, sys\n"
    "from pipelines.pipeline_generator import training_flow\n"
    "r = training_flow(sys.argv[1], sys.argv[2], is_dummy=sys.argv[3] == '1',\n"
    "                  backend_name=sys.argv[4] or None)\n"
    f"print('{METRICS_MARKER}' + json.dumps(r['metrics']))\n"
)


@dataclass
class StepResult:
    step: str
    status: str  # ok | failed | skipped | not_run | drift_allowed
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass
class EnvOutcome:
    """What the ``env`` step did beyond its pass/fail — reported even when the step is ``ok``."""

    #: Interpreter the ``train`` step will run, and the one the report names: the rebuilt one,
    #: else ``EXAMLOPS_REPRO_PYTHON``, else the caller's. Always a real path (see
    #: :func:`resolve_interpreter`).
    python: str = field(default_factory=resolve_interpreter)
    venv: str | None = None
    rebuilt: bool = False
    unsatisfied: list[str] = field(default_factory=list)
    image_status: str = IMG_UNCHECKED
    image_detail: str = ""


@dataclass
class ExecuteResult:
    model: str
    version: str
    steps: list[StepResult] = field(default_factory=list)
    produced_metrics: dict[str, float] = field(default_factory=dict)
    rtol: float = DEFAULT_RTOL
    worktree: str | None = None
    bit_exact: bool = False
    env: EnvOutcome = field(default_factory=EnvOutcome)
    #: Where step 2 restored the pinned dataset (``--restore-dataset``), else ``None``.
    dataset_dir: str | None = None
    #: Checkout of the recorded model-library commit the training step runs against, if any.
    modelzoo_worktree: str | None = None
    #: Scheduler the training step ran on and the resources it re-requested.
    scheduler: str = "mock"
    resources: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and not any(s.failed for s in self.steps)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120
    )


def _step_code(
    repo: Path, sha: str | None, wt: Path, *, dirty: bool | None = None, allow_dirty: bool = False
) -> StepResult:
    if not sha:
        return StepResult("code", "failed", "bundle records no code commit — refusing to use HEAD")
    if dirty and not allow_dirty:
        return StepResult(
            "code",
            "failed",
            "bundle was built from a dirty tree: the recorded commit does NOT contain the code "
            "that ran, so it cannot be rebuilt (--allow-dirty-code to rebuild the commit anyway)",
        )
    if _git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return StepResult("code", "failed", f"{repo} is not a git repository")
    if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        return StepResult("code", "failed", f"commit {sha[:12]} is not available in {repo}")
    added = _git(repo, "worktree", "add", "--detach", str(wt), sha)
    if added.returncode != 0:
        return StepResult(
            "code", "failed", f"git worktree add failed: {added.stderr.strip()[:200]}"
        )
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    if head != sha:
        return StepResult("code", "failed", f"worktree is at {head[:12]}, expected {sha[:12]}")
    note = " (bundle was dirty — allowed)" if dirty else ""
    return StepResult("code", "ok", f"detached worktree {wt} at {sha[:12]}{note}")


def _modelzoo_candidates(repo: Path, recorded: str | None) -> list[Path]:
    out: list[Path] = []
    env_dir = (os.getenv(cap.ENV_MODELZOO_DIR) or "").strip()
    for cand in (env_dir, str(repo / "modelzoo"), recorded or ""):
        if cand and Path(cand) not in out:
            out.append(Path(cand))
    return out


def _checkout_modelzoo(
    manifest: dict[str, Any], repo: Path, wt: Path, *, allow_dirty: bool = False
) -> tuple[bool, str, Path | None, Path | None]:
    """Check the recorded model-library commit out next to the platform worktree.

    ``(ok, detail, worktree, source checkout)``. A bundle that recorded a library commit must be rebuilt against that
    commit: when no known checkout has it the ``code`` step fails rather than training against
    whatever library is on disk. A bundle that recorded only a distribution version (or nothing)
    is reported as such and uses the library on disk — stated, not hidden.
    """
    mz = (manifest.get("code_commits") or {}).get("modelzoo") or {}
    sha = mz.get("commit")
    if not sha:
        if mz.get("distribution_version"):
            return (
                True,
                (
                    f"; modelzoo recorded as distribution {mz['distribution_version']} (no commit) — "
                    "the library on disk is used"
                ),
                None,
                None,
            )
        return (
            True,
            "; bundle records no modelzoo commit — the library on disk is used",
            None,
            None,
        )
    if mz.get("dirty") and not allow_dirty:
        return (
            False,
            (
                "modelzoo was dirty when the bundle was built: its recorded commit does NOT contain "
                "the library code that ran (--allow-dirty-code to rebuild the commit anyway)"
            ),
            None,
            None,
        )
    for cand in _modelzoo_candidates(repo, mz.get("path")):
        if not cand.is_dir() or _git(cand, "cat-file", "-e", f"{sha}^{{commit}}").returncode:
            continue
        added = _git(cand, "worktree", "add", "--detach", str(wt), sha)
        if added.returncode != 0:
            msg = f"modelzoo worktree add failed: {added.stderr.strip()[:200]}"
            return False, msg, None, None
        if _git(wt, "rev-parse", "HEAD").stdout.strip() != sha:
            return False, f"modelzoo worktree is not at {sha[:12]}", wt, cand
        return True, f"; modelzoo {sha[:12]} checked out from {cand}", wt, cand
    return (
        False,
        (
            f"modelzoo commit {sha[:12]} is not available in any known checkout "
            f"({cap.ENV_MODELZOO_DIR}, <repo>/modelzoo, the recorded path)"
        ),
        None,
        None,
    )


def _restore_dataplane(source_key: str, revision: str, dest: Path) -> tuple[str | None, str]:
    """Materialise a dataplane snapshot into ``dest`` (checksum-verified); ``(why, path)``."""
    try:
        from examlops.dataplane import materialize, resolve, store_from_env
        from examlops.dataplane.types import DataplaneError

        st = store_from_env()
        try:
            out = materialize(st, resolve(st, source_key, revision), dest)
        except DataplaneError as exc:
            return f"snapshot {source_key}@{revision[:16]} does not verify: {exc}", ""
    except Exception as exc:  # noqa: BLE001 - an unreachable store is a failure, not a pass
        return f"cannot restore snapshot {source_key}@{revision[:16]}: {exc}", ""
    return None, str(out)


def _dest_is_empty(dest: Path) -> bool:
    return not dest.exists() or (dest.is_dir() and not any(dest.iterdir()))


def _step_dataset(
    manifest: dict[str, Any],
    data_path: str | None,
    dummy: bool,
    restore_to: Path | None = None,
) -> StepResult:
    """Verify — and with ``restore_to``, restore — the pinned dataset revision.

    Restorable sources: a dataplane snapshot (materialised + checksum-verified) and a lakeFS
    commit (every object downloaded + size/MD5-checked). A content revision records a hash, not
    a source, so it cannot be restored: asking for it fails and says so.
    """
    name, rev = manifest.get("dataset_name"), manifest.get("dataset_revision")
    if not rev:
        return StepResult("dataset", "skipped", "bundle pins no dataset revision")
    if not name:
        return StepResult("dataset", "failed", "bundle pins a revision but no dataset name")
    if restore_to is not None and not _dest_is_empty(restore_to):
        return StepResult(
            "dataset", "failed", f"--restore-dataset {restore_to} is not an empty directory"
        )
    plane = _dataplane_source(manifest)
    if plane and restore_to is not None:
        why, where = _restore_dataplane(plane, str(rev), restore_to)
        if why:
            return StepResult("dataset", "failed", why)
        return StepResult(
            "dataset", "ok", f"dataplane snapshot {plane}@{str(rev)[:16]} restored to {where}"
        )
    if plane:
        # A dataplane snapshot is verified against its manifest + part checksums — the same
        # check training runs. Done even under --dummy: the bundle pins the snapshot, and an
        # unverifiable pin is a bundle problem whether or not this rebuild reads the data.
        why = verify_dataplane_snapshot(plane, str(rev))
        if why:
            return StepResult("dataset", "failed", why)
        return StepResult(
            "dataset", "ok", f"dataplane snapshot {plane}@{str(rev)[:16]} verified against manifest"
        )
    row = platform_db.get_dataset_revision(name, rev)
    if row is None:
        return StepResult("dataset", "failed", f"revision {rev[:16]} of {name} is not recorded")
    if row.get("kind") == "lakefs":
        parsed = lakefs.parse_uri(row.get("uri"))
        if parsed is None or parsed[1] != rev:
            return StepResult(
                "dataset", "failed", f"lakeFS revision {rev[:16]} has no matching lakefs:// uri"
            )
        if restore_to is not None:
            got = lakefs.restore(parsed[0], parsed[1], restore_to)
            return StepResult("dataset", "ok" if got.ok else "failed", got.detail)
        why = lakefs.verify_commit(parsed[0], parsed[1])
        if why:
            return StepResult("dataset", "failed", why)
        if not data_path:
            return StepResult(
                "dataset",
                "ok",
                f"lakeFS commit {parsed[0]}@{rev[:16]} exists (not downloaded; "
                "--restore-dataset to restore it)",
            )
    elif restore_to is not None:
        return StepResult(
            "dataset",
            "failed",
            f"revision {rev[:16]} is a {row.get('kind') or 'content'} revision: it records a "
            "hash, not a source, so it cannot be restored (--data-path verifies local data)",
        )
    if not data_path:
        if dummy:
            return StepResult("dataset", "skipped", "--dummy: training uses synthetic data")
        return StepResult(
            "dataset", "skipped", "revision recorded; content NOT verified (no --data-path)"
        )
    try:
        from examlops.cli.commands.data_cmd import _load_versioning

        vs = _load_versioning()
        files = vs.discover_files(data_path)
        actual, _, _ = vs.content_revision(files)
    except Exception as exc:  # noqa: BLE001
        return StepResult("dataset", "failed", f"cannot hash data at {data_path}: {exc}")
    if actual != rev:
        return StepResult(
            "dataset", "failed", f"data at {data_path} hashes to {actual[:16]}, pinned {rev[:16]}"
        )
    return StepResult("dataset", "ok", f"{data_path} matches pinned revision {rev[:16]}")


def _step_env(
    manifest: dict[str, Any],
    wt: Path,
    allow_drift: bool,
    *,
    rebuild: bool = False,
    venv_dir: Path | None = None,
    rebuild_args: list[str] | None = None,
    rebuild_timeout: int = 1800,
) -> tuple[StepResult, EnvOutcome]:
    """Check (and, with ``rebuild``, reconstitute) the recorded environment.

    Three independent clauses, each able to fail the step: the lockfile hash in the checkout,
    the recorded package set (compared against this interpreter, or rebuilt into a fresh venv),
    and the recorded container image digest. ``allow_drift`` downgrades a failure to
    ``drift_allowed``; it never turns one into ``ok``.
    """
    env = manifest.get("environment") or {}
    out = EnvOutcome()
    want, lock_path = env.get("lock_sha256"), env.get("lock_path")
    note = ""
    problem = None
    if not want or not lock_path:
        problem = "bundle captured no lockfile hash — environment unverifiable"
    else:
        got = _file_sha256(wt / lock_path)
        if got is None:
            problem = f"{lock_path} not present in the checked-out commit"
        elif got != want:
            problem = f"{lock_path} differs: recorded {str(want)[:12]}, checkout {got[:12]}"
    pkgs = env.get("packages")
    if rebuild:
        # The recorded set is put back rather than diffed: a rebuild that cannot resolve a
        # recorded package fails here, naming it — it never proceeds in another environment.
        built = rebuild_environment(
            pkgs or {},
            venv_dir if venv_dir is not None else Path(tempfile.mkdtemp(prefix="exa-repro-venv-")),
            recorded_python=env.get("python"),
            extra_args=rebuild_args,
            timeout=rebuild_timeout,
        )
        out.unsatisfied = list(built.unsatisfied)
        if built.ok and built.python:
            out.python, out.venv, out.rebuilt = built.python, built.venv, True
            note += f"; rebuilt env: {built.detail}"
            if built.python_mismatch:
                mm = f"rebuilt interpreter is {built.python_actual}, recorded {env.get('python')}"
                problem = f"{problem}; {mm}" if problem else mm
        else:
            problem = f"{problem}; {built.detail}" if problem else built.detail
    elif pkgs:
        cmp = compare_packages(pkgs)
        if cmp["drift"]:
            pkg_problem = describe_package_drift(cmp)
            problem = f"{problem}; {pkg_problem}" if problem else pkg_problem
        note += f"; {len(pkgs)} packages compared" if not cmp["drift"] else ""
    else:
        note += " (package set not captured — package-level check not possible)"

    rec_hw = manifest.get("hardware") or {}
    if rec_hw:
        hw_diff = cap.describe_hardware_difference(rec_hw, cap.capture_hardware())
        # Reported, never failed on: results are compared within a tolerance precisely because
        # the hardware may differ (ADR 0038 "Alternatives": no bit-exact claim). Silent when it
        # matches, so the detail of an unchanged host stays what it always was.
        if hw_diff:
            note += f"; hardware differs from the record ({hw_diff})"

    digest = verify_image_digest(env.get("image_digest"))
    out.image_status, out.image_detail = digest.status, digest.detail
    if digest.status != IMG_UNCHECKED:
        note += f"; image digest {digest.status}: {digest.detail}"
    if digest.failing:
        problem = f"{problem}; image digest {digest.status}" if problem else digest.detail

    if problem is None:
        return StepResult(
            "env", "ok", f"{lock_path} sha256 matches recorded {str(want)[:12]}{note}"
        ), out
    if allow_drift:
        return StepResult(
            "env", "drift_allowed", f"{problem} — continuing (--allow-env-drift)"
        ), out
    return StepResult("env", "failed", problem), out


def _run_train(
    manifest: dict[str, Any],
    wt: Path,
    *,
    dummy: bool,
    train_cmd: list[str] | None,
    timeout: int,
    scratch: Path | None = None,
    repo: Path | None = None,
    env_out: EnvOutcome | None = None,
    scheduler: str | None = None,
    dataset_dir: str | None = None,
    modelzoo_dir: Path | None = None,
    placement: dict[str, Any] | None = None,
) -> tuple[StepResult, dict[str, float]]:
    built = env_out or EnvOutcome()
    env = dict(os.environ)
    # The recorded scheduler-neutral request is exported as EXAMLOPS_HPC_*, so a rebuild on a
    # real scheduler asks for exactly what the original run asked for (ADR 0038 cl. 2).
    rec_res = manifest.get("resources") or {}
    res_env = cap.resource_env(rec_res)
    if res_env:
        # The recorded request is complete: every key the original run left unset was None,
        # not "whatever the caller's shell says". Without this, an EXAMLOPS_HPC_GPUS=8 (or a
        # legacy EXAMLOPS_SLURM_* fallback) in the operator's environment silently joins the
        # "recorded" request and the rebuild asks for resources the original never did.
        for var in cap.resource_env_vars():
            env.pop(var, None)
    env.update(res_env)
    if scheduler == "recorded":
        scheduler = rec_res.get("scheduler") or None
        if not scheduler:
            return StepResult("train", "failed", "bundle records no scheduler to re-use"), {}
    if scheduler:
        scheduler = scheduler.strip().lower()
        if scheduler not in SCHEDULERS:
            # An unknown name must not be exported and then reported as "the scheduler training
            # ran on": the pipeline would fail or fall back, and the result would name a lie.
            return (
                StepResult(
                    "train",
                    "failed",
                    f"unknown scheduler {scheduler!r} (expected one of {', '.join(SCHEDULERS)})",
                ),
                {},
            )
        env["EXAMLOPS_HPC_SCHEDULER"] = scheduler
    ran_on = env.get("EXAMLOPS_HPC_SCHEDULER") or (
        "slurm" if env.get("EXAMLOPS_SLURM_MODE", "mock").strip().lower() == "slurm" else "mock"
    )
    if placement is not None:
        placement["scheduler"] = ran_on
        placement["resources"] = dict(res_env)
    if dataset_dir:
        env["EXAMLOPS_REPRO_DATA_DIR"] = dataset_dir
    if modelzoo_dir is not None:
        env[cap.ENV_MODELZOO_DIR] = str(modelzoo_dir)
    extra = os.pathsep.join([str(wt), str(wt / "platform" / "cli" / "src")])
    env["PYTHONPATH"] = extra + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # The interpreter the reproduction actually runs on — the one `res.env.python` reports. With
    # a rebuilt environment it is the throw-away venv's python, never the caller's. It reaches a
    # custom --train-cmd three ways, so the report can never name one interpreter while another
    # runs: the variable, a PATH whose first entry is that interpreter's own bin directory, and
    # substitution of a bare `python`/`python3` argv[0] (a bare name resolved against whatever
    # PATH happens to hold is how a reproduction ends up on an interpreter nobody chose — on a
    # host with no `python` at all it simply dies).
    env[ENV_PYTHON] = built.python
    bindir = Path(built.python).parent
    if bindir.is_absolute() and bindir.is_dir():  # never put "." on PATH
        env["PATH"] = str(bindir) + (os.pathsep + env["PATH"] if env.get("PATH") else "")
    if built.rebuilt and built.venv:
        env["VIRTUAL_ENV"] = built.venv
        env.pop("PYTHONHOME", None)
    if manifest.get("dataset_revision"):
        env["EXAMLOPS_DATASET_REVISION"] = str(manifest["dataset_revision"])
    seeds = manifest.get("seeds") or {}
    if "global" in seeds:
        env["EXAMLOPS_SEED"] = str(seeds["global"])
        env["PYTHONHASHSEED"] = str(seeds["global"])
    env.setdefault("EXAMLOPS_SLURM_MODE", "mock")
    if train_cmd:
        cmd = list(train_cmd)
        if cmd and Path(cmd[0]).name == cmd[0] and cmd[0].lower() in _BARE_PYTHON:
            cmd[0] = built.python
    else:
        spec = manifest.get("run_spec") or {}
        cmd = [
            built.python,
            "-c",
            _DRIVER,
            str(spec.get("registry_model") or manifest.get("model")),
            str(spec.get("dataset") or manifest.get("dataset_name") or ""),
            "1" if dummy else "0",
            str(spec.get("backend") or ""),
        ]
        _isolate_default_run(env, scratch)
        # The upstream model library is not part of the public tree, so a checkout of the bundle's
        # commit may not carry it. When the bundle recorded a library commit, the `code` step
        # checked that commit out and `modelzoo_dir` already points at it; only a bundle that
        # recorded none falls back to the caller's library (EXAMLOPS_MODELZOO_DIR wins).
        if not env.get("EXAMLOPS_MODELZOO_DIR") and not (wt / "modelzoo").is_dir():
            if repo is not None and (repo / "modelzoo").is_dir():
                env["EXAMLOPS_MODELZOO_DIR"] = str(repo / "modelzoo")
    try:
        done = subprocess.run(cmd, cwd=wt, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return StepResult("train", "failed", f"training could not run: {exc}"), {}
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or [""]
        return StepResult("train", "failed", f"exit {done.returncode}: {tail[0][:200]}"), {}
    where = f" on {built.python}" if built.rebuilt else ""
    where += f"; scheduler {ran_on}" + (
        f", re-requested {len(res_env)} recorded resource(s)" if res_env else ""
    )
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith(METRICS_MARKER)]
    if not lines:
        return StepResult("train", "failed", f"training printed no '{METRICS_MARKER}' line"), {}
    try:
        raw = json.loads(lines[-1][len(METRICS_MARKER) :])
        metrics = {str(k): float(v) for k, v in raw.items()}
    except (ValueError, TypeError, AttributeError) as exc:
        return StepResult("train", "failed", f"unparseable metrics line: {exc}"), {}
    return (
        StepResult("train", "ok", f"training exited 0 in {wt}{where}; {len(metrics)} metrics"),
        metrics,
    )


def _isolate_default_run(env: dict[str, str], scratch: Path | None) -> None:
    """Keep a rebuild from touching the live platform: the default path really runs the training
    flow, which registers a model version and writes audit/lineage rows. Point it at a throw-away
    SQLite MLflow store and platform DB (``EXAMLOPS_REPRO_MLFLOW_URI`` opts into another store)
    and switch automatic bundling off inside it."""
    base = scratch or Path(tempfile.mkdtemp(prefix="exa-repro-run-"))
    env["MLFLOW_TRACKING_URI"] = os.getenv("EXAMLOPS_REPRO_MLFLOW_URI") or (
        f"sqlite:///{base / 'mlflow.db'}"
    )
    env["PLATFORM_DB"] = str(base / "platform.db")
    env["EXAMLOPS_REPRO_AUTO_BUNDLE"] = "0"
    env.pop("EXAMLOPS_DATA_DIR", None)


def _step_compare(recorded: dict[str, Any], produced: dict[str, float], rtol: float) -> StepResult:
    if not recorded:
        return StepResult("compare", "failed", "bundle records no metrics — nothing to compare")
    bad: list[str] = []
    for k, v in recorded.items():
        if k not in produced:
            bad.append(f"{k}: not produced")
            continue
        p = produced[k]
        try:
            r = float(v)
        except (TypeError, ValueError):
            bad.append(f"{k}: recorded value {v!r} is not numeric")
            continue
        if not (math.isfinite(p) and math.isfinite(r)) or _rel_diff(r, p) > rtol:
            bad.append(f"{k}: recorded {r} vs produced {p} (rtol {rtol:g})")
    if bad:
        return StepResult("compare", "failed", "; ".join(bad))
    return StepResult(
        "compare", "ok", f"{len(recorded)} metrics within rtol {rtol:g} (not bit-exact)"
    )


def execute_reproduction(
    model: str,
    version: str,
    *,
    repo: str | Path = ".",
    data_path: str | None = None,
    dummy: bool = False,
    allow_env_drift: bool = False,
    allow_dirty_code: bool = False,
    rtol: float | None = None,
    train_cmd: list[str] | None = None,
    timeout: int = 3600,
    keep_worktree: bool = False,
    on_step: Callable[[StepResult], None] | None = None,
    rebuild_env: bool = False,
    venv_dir: str | Path | None = None,
    rebuild_env_args: list[str] | None = None,
    restore_dataset: str | Path | None = None,
    scheduler: str | None = None,
) -> ExecuteResult:
    """Run the five steps; stop at the first failure (later steps ``not_run``).

    ``rebuild_env`` materialises the bundle's recorded package set into a fresh venv and runs
    the training step on it (ADR 0038 clause 2). ``venv_dir`` places that venv somewhere the
    caller can inspect — by default it lives in the same throw-away directory as the worktree
    and is removed with it. ``rebuild_env_args`` is passed through to ``uv pip install`` and is
    not reachable from the CLI; the test suite uses it to rebuild from a local wheelhouse.
    ``restore_dataset`` restores the pinned dataset (dataplane snapshot or lakeFS commit) into
    that empty directory and hands it to training as ``EXAMLOPS_REPRO_DATA_DIR``. ``scheduler``
    selects where training runs: ``None`` keeps the caller's configuration (mock unless set),
    ``"recorded"`` re-uses the bundle's scheduler, anything else names one; the recorded
    resources are re-requested in every case.
    """
    res = ExecuteResult(model, str(version))

    def record(s: StepResult) -> bool:
        res.steps.append(s)
        if on_step:
            on_step(s)
        return not s.failed

    def finish() -> ExecuteResult:
        done = {s.step for s in res.steps}
        for name in STEPS:
            if name not in done:
                res.steps.append(StepResult(name, "not_run", "an earlier step failed"))
        _audit(res)
        return res

    row = platform_db.get_repro_bundle(model, version)
    if not row:
        record(StepResult("code", "failed", f"no bundle for {model}/{version}"))
        return finish()
    manifest = row["manifest"]
    if _canonical_hash(manifest) != row["manifest_hash"]:
        record(StepResult("code", "failed", "manifest hash mismatch — bundle tampered"))
        return finish()
    tol = manifest.get("tolerance") or DEFAULT_TOLERANCE
    res.rtol = float(rtol if rtol is not None else cast("float", tol.get("rel", DEFAULT_RTOL)))

    tmp = Path(tempfile.mkdtemp(prefix="exa-repro-"))
    wt = tmp / "worktree"
    placement: dict[str, Any] = {}
    repo_p = Path(repo).resolve()
    try:
        code = _step_code(
            repo_p,
            manifest.get("code_commit"),
            wt,
            dirty=manifest.get("code_dirty"),
            allow_dirty=allow_dirty_code,
        )
        if not code.failed:
            mz_ok, mz_detail, mz_path, mz_src = _checkout_modelzoo(
                manifest, repo_p, tmp / "modelzoo", allow_dirty=allow_dirty_code
            )
            if mz_src is not None:
                placement["modelzoo_source"] = str(mz_src)
            code = (
                StepResult("code", "ok", code.detail + mz_detail)
                if mz_ok
                else StepResult("code", "failed", mz_detail)
            )
            res.modelzoo_worktree = str(mz_path) if mz_path is not None else None
        if not record(code):
            return finish()
        res.worktree = str(wt) if keep_worktree else None
        restore_to = Path(restore_dataset).resolve() if restore_dataset else None
        ds_step = _step_dataset(manifest, data_path, dummy, restore_to)
        if not record(ds_step):
            return finish()
        if restore_to is not None and ds_step.status == "ok":
            res.dataset_dir = str(restore_to)
        env_step, res.env = _step_env(
            manifest,
            wt,
            allow_env_drift,
            rebuild=rebuild_env,
            venv_dir=Path(venv_dir) if venv_dir is not None else tmp / "venv",
            rebuild_args=list(rebuild_env_args) if rebuild_env_args else None,
            rebuild_timeout=timeout,
        )
        if not record(env_step):
            return finish()
        step, produced = _run_train(
            manifest,
            wt,
            dummy=dummy,
            train_cmd=train_cmd,
            timeout=timeout,
            scratch=tmp,
            repo=repo_p,
            env_out=res.env,
            scheduler=scheduler,
            dataset_dir=res.dataset_dir,
            modelzoo_dir=Path(res.modelzoo_worktree) if res.modelzoo_worktree else None,
            placement=placement,
        )
        res.scheduler = str(placement.get("scheduler") or res.scheduler)
        res.resources = dict(placement.get("resources") or {})
        if not record(step):
            return finish()
        res.produced_metrics = produced
        record(_step_compare(manifest.get("metrics") or {}, produced, res.rtol))
        return finish()
    finally:
        if not keep_worktree:
            if res.modelzoo_worktree and placement.get("modelzoo_source"):
                _git(
                    Path(placement["modelzoo_source"]),
                    "worktree",
                    "remove",
                    "--force",
                    res.modelzoo_worktree,
                )
            _git(repo_p, "worktree", "remove", "--force", str(wt))
            shutil.rmtree(tmp, ignore_errors=True)


def _audit(res: ExecuteResult) -> None:
    from examlops.data.audit import audit_best_effort

    audit_best_effort(
        "exa-reproduce",
        os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER"),
        "repro_execute",
        f"{res.model}/{res.version}",
        {
            "ok": res.ok,
            "steps": {s.step: s.status for s in res.steps},
            "env_rebuilt": res.env.rebuilt,
            "image_digest": res.env.image_status,
            "scheduler": res.scheduler,
            "dataset_restored": res.dataset_dir is not None,
        },
    )


__all__ = [
    "EnvOutcome",
    "ExecuteResult",
    "StepResult",
    "STEPS",
    "METRICS_MARKER",
    "DEFAULT_RTOL",
    "ENV_PYTHON",
    "NONDETERMINISM_CAVEAT",
    "execute_reproduction",
    "resolve_interpreter",
]
