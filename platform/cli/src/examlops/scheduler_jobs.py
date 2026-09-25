"""Running platform work as a real job on the phase-23 scheduler — the rules in one place.

An asset build (ADR 0036 clause 3) and an embedding reindex (ADR 0043 clause 4) both hand work to
mock / Slurm / Flux. Both used to call ``submit_job(script_path=None, …)`` with the command in
``training_data``, which the real adapters ignore: Slurm and Flux refused every such job, and the
mock — which executes only when waited on — accepted it and never ran it. This module is what
both now use, so the rules that make a job safe and real are written once:

* **A real script.** ``run.sh`` is generated, mode 0700, every value shell-quoted, and **no
  environment value is written into it** — the scheduler exports the submitter's environment
  (``sbatch``/``flux batch``; the mock runs a child process).
* **Kept out of the repository.** Scripts live under ``EXAMLOPS_JOB_SCRIPT_DIR`` (default
  ``$XDG_CACHE_HOME/examlops/jobs``), never the adapter's working directory, which for the mock
  is inside the checkout: a script holding this host's absolute paths is one ``git add -A`` from
  being published. They are kept after the run as the record of what a job was asked to do.
* **The same code on both sides.** The job's ``PYTHONPATH`` leads with the *submitter's*
  ``examlops``, so a job runs the code that wrote it rather than whatever copy its interpreter
  has installed. An interpreter's ``site-packages`` is passed only to that same interpreter.
* **The pipeline generator's settings.** ``EXAMLOPS_HPC_REMOTE_PYTHON`` (the job's interpreter),
  ``EXAMLOPS_HPC_REMOTE_REPO`` (re-roots paths inside the checkout), ``EXAMLOPS_HPC_REMOTE_WORKDIR``
  (per-job directories on the cluster). With none set, the job uses the submitting interpreter.
* **Visible.** Every job is recorded in ``hpc_jobs``, so ``exa hpc jobs`` lists it.
"""

from __future__ import annotations

import os
import shlex
import sys
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

_LIBRARY_DIRS = ("site-packages", "dist-packages")


def repo_root() -> Path:
    """The checkout this package runs from — this module's one repository coupling (ADR 0128's
    ratchet): where the phase-23 adapters live, and what ``EXAMLOPS_HPC_REMOTE_REPO`` re-roots."""
    return Path(__file__).resolve().parents[4]


def scheduler_adapter(scheduler: str | None = None) -> Any:
    """The phase-23 adapter for ``scheduler``, else the configured one (mock / slurm / flux)."""
    adapter_dir = repo_root() / "platform" / "infra" / "slurm-adapter"
    if str(adapter_dir) not in sys.path:
        sys.path.insert(0, str(adapter_dir))
    from adapter import get_scheduler_adapter  # noqa: PLC0415

    if scheduler:
        return get_scheduler_adapter(scheduler=scheduler)
    return get_scheduler_adapter()


def scheduler_name() -> str:
    """``mock`` / ``slurm`` / ``flux`` — the same variable that selects the adapter."""
    return (os.getenv("EXAMLOPS_HPC_SCHEDULER") or "mock").strip().lower()


def executes_only_when_waited(scheduler: str | None = None) -> bool:
    """The mock runs a job inside ``wait_until_complete`` and nowhere else, so a fire-and-forget
    submission to it never runs. The pipeline generator special-cases mock for the same reason."""
    return (scheduler or scheduler_name()) == "mock"


def job_dir() -> Path:
    """Where generated scripts are kept (see the module docstring for why not the repo)."""
    # EXAMLOPS_ASSET_JOB_DIR is v0.53.0's name for this (asset jobs were then the only kind);
    # still honoured, so upgrading does not move a deployment's scripts.
    configured = os.getenv("EXAMLOPS_JOB_SCRIPT_DIR") or os.getenv("EXAMLOPS_ASSET_JOB_DIR")
    if configured:
        return Path(configured)
    cache = os.getenv("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "examlops" / "jobs"


def adapter_working_dir(kind: str) -> Path:
    """Where an adapter keeps its own job files (logs, per-job folders) for scheduler ``kind``.

    ``EXAMLOPS_HPC_WORKDIR`` wins, as ``<it>/<kind>`` — a cluster wants this on the shared
    filesystem its compute nodes can see, and CI wants it in a temporary directory.

    Unset, the **mock** scheduler uses the cache directory: it runs locally, and its historical
    default was a folder *inside the installed package* (``platform/infra/slurm-adapter/
    mock_hpc_jobs``), so a mock run wrote generated scripts and pickles into the checkout — files
    holding absolute local paths, untracked but not ignored by anything committed, one whole-tree
    ``add`` from being published. A real **slurm**/**flux** adapter keeps its relative default
    (``slurm_jobs`` / ``flux_jobs``): those paths are passed to ``sbatch --output`` and must resolve
    on the cluster, so the submit directory is the operator's choice to make, not ours to move.
    """
    configured = os.getenv("EXAMLOPS_HPC_WORKDIR")
    if configured:
        return Path(configured) / kind
    if kind == "mock":
        return job_dir().parent / "mock_hpc_jobs"
    return Path(f"{kind}_jobs")


def job_python() -> str:
    """The interpreter the job runs: ``EXAMLOPS_HPC_REMOTE_PYTHON``, else the remote repo's venv
    when ``EXAMLOPS_HPC_REMOTE_REPO`` is set, else this one (the mock and a shared filesystem)."""
    remote_repo = os.getenv("EXAMLOPS_HPC_REMOTE_REPO")
    return os.getenv("EXAMLOPS_HPC_REMOTE_PYTHON") or (
        str(Path(remote_repo) / ".venv" / "bin" / "python") if remote_repo else sys.executable
    )


def import_root(fn: Any) -> Path | None:
    """The sys.path entry that makes ``fn.__module__`` importable, from the module's file."""
    module = sys.modules.get(getattr(fn, "__module__", "") or "")
    file = getattr(module, "__file__", None)
    if not file:
        return None
    path = Path(file).resolve()
    if path.name == "__init__.py":
        path = path.parent
    depth = len(fn.__module__.split("."))
    return path.parents[depth - 1]


def pythonpath_roots(python: str, extra: Iterable[Path | None] = ()) -> list[str]:
    """What the job's interpreter must import, in order: the submitter's ``examlops`` first,
    then ``extra`` (e.g. a production function's package).

    Paths inside the checkout are re-rooted at ``EXAMLOPS_HPC_REMOTE_REPO`` when that is set;
    elsewhere they are assumed shared (the NFS layout the platform deploys on). An interpreter's
    library directory goes only to that same interpreter — another Python's ``site-packages`` in
    front of the job's own would mix two sets of compiled libraries.
    """
    import examlops

    repo = repo_root()
    remote_repo = os.getenv("EXAMLOPS_HPC_REMOTE_REPO")
    same_interpreter = python == sys.executable
    roots: list[str] = []
    for root in (Path(examlops.__file__).resolve().parent.parent, *extra):
        if root is None:
            continue
        if not same_interpreter and root.name in _LIBRARY_DIRS:
            continue
        if remote_repo and root.is_relative_to(repo):
            root = Path(remote_repo) / root.relative_to(repo)
        if str(root) not in roots:
            roots.append(str(root))
    return roots


def script_text(argv: Sequence[str], *, title: str, extra_roots: Iterable[Path | None] = ()) -> str:
    """A ``run.sh`` that ``exec``s ``argv`` (``argv[0]`` must be the job's python)."""
    # The title is a comment: collapse every whitespace run (newlines included) so no value in
    # it can end the comment and become a command.
    title = " ".join(str(title).split())
    lines = [
        "#!/usr/bin/env bash",
        f"# ExaMLOps scheduler job: {title}. Generated — see examlops.scheduler_jobs.",
        "set -euo pipefail",
    ]
    roots = pythonpath_roots(argv[0], extra_roots)
    if roots:
        joined = shlex.quote(os.pathsep.join(roots))
        lines.append(f'export PYTHONPATH={joined}"${{PYTHONPATH:+:$PYTHONPATH}}"')
    lines.append("exec " + " ".join(shlex.quote(str(a)) for a in argv))
    return "\n".join(lines) + "\n"


def write_script(kind: str, text: str) -> tuple[Path, str]:
    """Write ``text`` as ``<job_dir>/<kind>-<id>/run.sh`` (0700); returns (script, job_key)."""
    job_key = f"{kind}-{uuid.uuid4().hex[:12]}"
    local_dir = job_dir() / job_key
    local_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    script = local_dir / "run.sh"
    script.write_text(text)
    script.chmod(0o700)
    return script, job_key


def submit(adapter: Any, script: Path, job_key: str, resources: dict[str, Any]) -> str:
    """Submit ``script``; the adapter stages it to ``<remote workdir>/<job_key>`` itself."""
    remote_base = os.getenv("EXAMLOPS_HPC_REMOTE_WORKDIR") or str(adapter.working_dir)

    def _submit() -> str:
        return str(
            adapter.submit_job(
                script_path=str(script), resources=resources, remote_dir=f"{remote_base}/{job_key}"
            )
        )

    # Admission seam (ADR 0116): every platform job reaches the scheduler through here, so this is
    # where it is admitted. Behind EXAMLOPS_ADMISSION_DISPATCH_ENABLED (default OFF, then this is
    # exactly the direct submission it always was). Enabled, a refusal raises AdmissionRefused
    # before anything is submitted, and an admitted job's quota is bound to its job id and released
    # when its terminal state is recorded (examlops.data.hpc.update_hpc_job).
    from examlops.admission_seam import dispatch

    if not dispatch.is_enabled():
        return _submit()
    return dispatch.submit_admitted(
        dispatch.request_for_hpc_job(resources, scheduler=scheduler_name()),
        scheduler=scheduler_name(),
        submit=_submit,
    )


def log_tail(adapter: Any, job_id: str, lines: int = 20) -> str:
    """The last lines of a job's log, prefixed for an error message — or ``""``."""
    try:
        text = str(adapter.get_job_logs(job_id) or "")
    except Exception:  # noqa: BLE001 - the state is the finding; logs are a courtesy
        return ""
    tail = "\n".join(text.strip().splitlines()[-lines:])
    return f" — last log lines:\n{tail}" if tail else ""


def record_job(job_id: str, scheduler: str, label: str, resources: dict[str, Any]) -> None:
    """Best-effort ``hpc_jobs`` row (``model`` = ``label``) — bookkeeping never fails a job."""
    try:
        from examlops.data.hpc import record_hpc_job

        def _int(v: Any) -> int | None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        record_hpc_job(
            job_id=job_id,
            scheduler=scheduler,
            flow_run_id=None,
            model=label,
            dataset="",
            nodes=_int(resources.get("nodes")),
            gpus=_int(resources.get("gpus")),
            cpus=_int(resources.get("cpus_per_task")),
        )
    except Exception:  # noqa: BLE001
        pass


def finish_job(job_id: str, scheduler: str, status: dict[str, Any]) -> None:
    """Best-effort terminal update of the ``hpc_jobs`` row."""
    try:
        from examlops.data.hpc import update_hpc_job

        update_hpc_job(
            job_id,
            scheduler,
            state=status.get("state"),
            start_time=status.get("start_time"),
            end_time=status.get("end_time"),
            exit_code=status.get("exit_code"),
        )
    except Exception:  # noqa: BLE001
        pass
