"""Next-Gen 40 · A4 — declarative asset-centric pipelines (ADR 0036).

An asset-centric layer *over* the existing Prefect orchestration. Datasets (A1), features
(A3), and models are declared as **assets** with an `@asset` decorator that names upstream
dependencies and the production function. The platform builds the asset DAG (which
coincides with the A2 lineage graph), tracks each asset's materialized version against the
upstream versions it was built from, reports staleness, and rebuilds **only** a target
asset plus its stale ancestors (selective/incremental).

The engine is behind an `AssetOrchestrator` seam so it is swappable (a thin layer over
Prefect by default, or Dagster). The existing `exa pipeline run` path is untouched (R2/GWT-5).

Every materialization emits OpenLineage (A2), is checked against policy (D5), and is
audited (D4). Pure-Python and fully testable — no Prefect/Dagster required to declare
assets, compute freshness, or run selective materialization in-process.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from examlops import data as platform_db

# In-process registry of declared assets (production functions live here; version/freshness
# state lives in platform.db so it survives across processes).
_REGISTRY: dict[str, AssetDef] = {}

# Whether `declare_asset` writes the declaration to platform.db. A scheduler job imports the module
# that holds a production function, and importing it re-runs its `@asset` decorators; the job
# (`examlops.assets.job`) turns this off so that import stays a pure lookup, needing no datastore.
_PERSIST_DECLARATIONS = True


class AssetBuildError(RuntimeError):
    """An asset's build did not complete, so no version was recorded for it."""


@dataclass
class AssetDef:
    name: str
    kind: str = "model"  # dataset | feature | model
    deps: list[str] = field(default_factory=list)
    fn: Callable[..., Any] | None = None
    description: str | None = None
    # What a scheduler job asks for (nodes, gpus, time, partition…) — used only by the
    # `scheduler` orchestrator; the phase-23 adapter maps the keys to sbatch / flux flags.
    resources: dict[str, Any] | None = None


@dataclass
class Freshness:
    name: str
    fresh: bool
    reasons: list[str]
    version: int


@dataclass
class MaterializeResult:
    target: str
    rebuilt: list[str]
    skipped: list[str]
    blocked: str | None = None  # policy denial reason, if any


def asset(
    name: str | None = None,
    *,
    kind: str = "model",
    deps: list[str] | None = None,
    description: str | None = None,
    resources: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare an asset and its upstream dependencies (R1).

    ::

        @asset(kind="model", deps=["dataset:PM100", "feature:jpcp_features"])
        def jpcp_model(**upstream): ...
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        asset_name = name or fn.__name__
        declare_asset(
            asset_name,
            kind=kind,
            deps=deps or [],
            description=description,
            fn=fn,
            resources=resources,
        )
        return fn

    return wrap


def declare_asset(
    name: str,
    *,
    kind: str = "model",
    deps: list[str] | None = None,
    description: str | None = None,
    fn: Callable[..., Any] | None = None,
    resources: dict[str, Any] | None = None,
) -> AssetDef:
    """Register an asset without the decorator (also used for source/dataset assets)."""
    a = AssetDef(
        name=name,
        kind=kind,
        deps=deps or [],
        fn=fn,
        description=description,
        resources=resources,
    )
    _REGISTRY[name] = a
    if _PERSIST_DECLARATIONS:
        platform_db.register_asset(name, kind, a.deps, description=description)
    return a


def _current_version(name: str) -> int:
    row = platform_db.get_asset(name)
    return row["current_version"] if row else 0


def asset_status(name: str) -> Freshness:
    """Freshness of an asset vs the upstream versions it was last built from (R3/GWT-2).

    Stale if it was never materialized, any declared upstream advanced past the recorded
    build version, or any upstream is itself stale (transitive).
    """
    row = platform_db.get_asset(name)
    if row is None:
        return Freshness(name, fresh=False, reasons=["not declared"], version=0)
    reasons: list[str] = []
    if row["current_version"] == 0 or row["last_materialized_at"] is None:
        reasons.append("never materialized")
    built_from = row.get("built_from", {})
    for dep in row["deps"]:
        cur = _current_version(dep)
        was = built_from.get(dep)
        if was is None:
            if row["current_version"] > 0:
                reasons.append(f"upstream {dep} not recorded at build")
        elif cur != was:
            reasons.append(f"upstream {dep} changed ({was} → {cur})")
        elif not asset_status(dep).fresh:
            reasons.append(f"upstream {dep} is stale")
    return Freshness(name, fresh=not reasons, reasons=reasons, version=row["current_version"])


def _topo_order(name: str) -> list[str]:
    """Dependencies-first ordering of the sub-DAG rooted at ``name`` (declared assets only)."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(n: str, stack: tuple[str, ...]) -> None:
        if n in seen:
            return
        if n in stack:
            raise ValueError(f"cycle in asset graph at {n}")
        row = platform_db.get_asset(n)
        deps = row["deps"] if row else []
        for d in deps:
            if platform_db.get_asset(d) is not None:
                visit(d, stack + (n,))
        seen.add(n)
        order.append(n)

    visit(name, ())
    return order


def materialize(
    name: str,
    *,
    actor: str | None = None,
    force: bool = False,
    run_id: str | None = None,
    no_deps: bool = False,
    orchestrator: str | None = None,
) -> MaterializeResult:
    """Rebuild ``name`` + its stale ancestors only (R4/GWT-3), emit lineage (R6), audit (R7).

    Governed by policy (D5): a ``deny`` on ``asset_materialize`` blocks the run.

    ``no_deps`` builds only ``name``, never its stale ancestors.

    ``orchestrator`` overrides ``EXAMLOPS_ASSET_ORCHESTRATOR`` for this call. A build that does
    not complete raises (:class:`AssetBuildError` from the scheduler path, the production
    function's own exception otherwise) and records no version for that asset.
    """
    from examlops.data.audit import write_audit_event

    # Policy gate (D5) — default allow if no policy file.
    blocked = _policy_block("asset_materialize", {"asset": name, "actor": actor})
    if blocked is not None:
        write_audit_event(
            "exa-assets", actor, "asset_materialize_denied", name, {"reason": blocked}
        )
        return MaterializeResult(name, rebuilt=[], skipped=[], blocked=blocked)

    order = [name] if no_deps else _topo_order(name)
    rebuilt: list[str] = []
    skipped: list[str] = []
    for node in order:
        needs = force or not asset_status(node).fresh
        if not needs:
            skipped.append(node)
            continue
        _run_asset(node, run_id=run_id, actor=actor, orchestrator=orchestrator)
        rebuilt.append(node)
    write_audit_event(
        "exa-assets",
        actor,
        "asset_materialize",
        name,
        {
            "rebuilt": rebuilt,
            "skipped": skipped,
            "forced": force,
            "orchestrator": get_orchestrator(orchestrator).name,
        },
    )
    return MaterializeResult(name, rebuilt=rebuilt, skipped=skipped)


# ── The AssetOrchestrator seam (ADR 0036 clause 1, clause 3's "via the scheduler") ──────────
#
# The seam was named in this module's docstring and existed nowhere as code, so nothing was
# swappable and clause 3's "via the scheduler (phase 23)" could not be true: materializing an
# asset called a local Python function and bumped a row.


@runtime_checkable
class AssetOrchestrator(Protocol):
    """How an asset's production function is executed. Three implementations ship."""

    name: str

    def run(self, definition: Any, upstream: dict[str, int]) -> dict[str, Any]:
        """Execute one asset's production. Returns provenance for the version record."""
        ...


class LocalOrchestrator:
    """Call the production function in this process — what materialization has always done.

    The default, and byte-identical to the previous behaviour. An asset layer that suddenly
    started submitting scheduler jobs on upgrade would surprise every existing caller.
    """

    name = "local"

    def run(self, definition: Any, upstream: dict[str, int]) -> dict[str, Any]:
        if definition is not None and definition.fn is not None:
            definition.fn(**upstream)
        return {"orchestrator": self.name}


class SchedulerOrchestrator:
    """Run the materialization as a job on the phase-23 scheduler — mock, Slurm or Flux (clause 3).

    The job runs **only the production function**: a generated ``run.sh`` calls
    ``python -m examlops.assets.job --entrypoint <module:qualname>`` with the upstream versions.
    This process submits it, waits for a terminal state, and records the one version bump; the job
    never touches the asset graph, so it cannot resubmit itself and needs no access to
    ``platform.db``. That is the pipeline generator's shape (``slurm_submit`` → ``slurm_wait``)
    and it reuses its settings: ``EXAMLOPS_HPC_REMOTE_PYTHON``, ``EXAMLOPS_HPC_REMOTE_REPO``,
    ``EXAMLOPS_HPC_REMOTE_WORKDIR``. The job inherits the submitter's environment through the
    scheduler (``sbatch``/``flux batch`` export it; mock runs a child process) — no value is ever
    written into the script.

    A job is a separate process, so the production function must be **importable** there:
    a module-level function. A closure or lambda cannot cross a process boundary; it is built
    locally and the provenance says why. With no scheduler in this environment the build falls
    back to local too. A scheduler that **refuses** the job, or a job that ends in any state but
    ``COMPLETED``, raises :class:`AssetBuildError` — quietly running a cluster-sized build on the
    submitting host instead would be the worse surprise.
    """

    name = "scheduler"

    def run(self, definition: Any, upstream: dict[str, int]) -> dict[str, Any]:
        asset_name = getattr(definition, "name", None) or "asset"
        fn = getattr(definition, "fn", None)
        if fn is None:
            return {"orchestrator": self.name, "fallback": "none (no production function to run)"}
        entrypoint, why = _job_entrypoint(fn)
        if entrypoint is None:
            LocalOrchestrator().run(definition, upstream)
            return {"orchestrator": self.name, "fallback": f"local ({why})"}
        try:
            adapter = _scheduler_adapter()
        except Exception as exc:  # noqa: BLE001
            # A missing scheduler is an environment fact, not an asset failure. Fall back to
            # local execution and say so, rather than leaving the asset unbuilt.
            LocalOrchestrator().run(definition, upstream)
            return {"orchestrator": self.name, "fallback": f"local ({exc})"}

        import os
        import uuid

        scheduler = (os.getenv("EXAMLOPS_HPC_SCHEDULER") or "mock").strip().lower()
        job_key = f"asset-{uuid.uuid4().hex[:12]}"
        local_dir = _job_dir() / job_key
        local_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        script = local_dir / "run.sh"
        script.write_text(_job_script(asset_name, entrypoint, fn, upstream))
        script.chmod(0o700)
        remote_base = os.getenv("EXAMLOPS_HPC_REMOTE_WORKDIR") or str(adapter.working_dir)
        resources = {
            "job_name": f"asset-{asset_name}",
            **(getattr(definition, "resources", None) or {}),
        }
        try:
            job_id = str(
                adapter.submit_job(
                    script_path=str(script),
                    resources=resources,
                    remote_dir=f"{remote_base}/{job_key}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise AssetBuildError(
                f"asset {asset_name!r}: the {scheduler} scheduler refused the job ({exc})"
            ) from exc
        _record_asset_job(job_id, scheduler, asset_name, resources)
        try:
            adapter.wait_until_complete(job_id)
            status = adapter.get_job_status(job_id)
        except Exception as exc:  # noqa: BLE001 - timeout / lost job: the build did not finish
            raise AssetBuildError(
                f"asset {asset_name!r}: {scheduler} job {job_id} did not finish ({exc})"
            ) from exc
        state = str(status.get("state") or "UNKNOWN")
        _finish_asset_job(job_id, scheduler, status)
        if state != "COMPLETED":
            raise AssetBuildError(
                f"asset {asset_name!r}: {scheduler} job {job_id} ended {state}"
                f"{_log_tail(adapter, job_id)}"
            )
        return {"orchestrator": self.name, "hpc_job_id": job_id, "scheduler": scheduler}


def _job_entrypoint(fn: Any) -> tuple[str | None, str]:
    """``module:qualname`` a job can import ``fn`` by — or ``None`` and the reason it cannot.

    Proven, not guessed: the name is resolved here and must give back the very same object, so
    a function shadowed or rebound after definition is not sent to a job that would run another.
    """
    import importlib

    module, qualname = getattr(fn, "__module__", None), getattr(fn, "__qualname__", None)
    if not module or not qualname or module == "__main__" or "<" in qualname:
        return None, (
            f"production function {qualname or fn!r} is not importable by a job — "
            "closures, lambdas and __main__ functions run in-process"
        )
    try:
        obj: Any = importlib.import_module(module)
        for part in qualname.split("."):
            obj = getattr(obj, part)
    except Exception as exc:  # noqa: BLE001
        return None, f"production function {module}:{qualname} does not resolve ({exc})"
    if obj is not fn:
        return None, f"{module}:{qualname} resolves to a different object than the asset's"
    return f"{module}:{qualname}", ""


def _job_script(asset_name: str, entrypoint: str, fn: Any, upstream: dict[str, int]) -> str:
    """The job's ``run.sh``. Every interpolated value is shell-quoted; no environment value is
    written into it (the scheduler exports the submitter's environment to the job)."""
    import json
    import os
    import shlex
    import sys
    from pathlib import Path

    repo = _repo_root()
    remote_repo = os.getenv("EXAMLOPS_HPC_REMOTE_REPO")
    python = os.getenv("EXAMLOPS_HPC_REMOTE_PYTHON") or (
        str(Path(remote_repo) / ".venv" / "bin" / "python") if remote_repo else sys.executable
    )
    # The directory the production function's top-level package sits in, so the job's python
    # can import it. Inside the repo it is re-rooted at EXAMLOPS_HPC_REMOTE_REPO when that is
    # set; elsewhere it is assumed shared (the NFS layout the platform deploys on).
    root = _import_root(fn)
    if root is not None and remote_repo and root.is_relative_to(repo):
        root = Path(remote_repo) / root.relative_to(repo)
    lines = [
        "#!/usr/bin/env bash",
        f"# ExaMLOps asset build: {asset_name!r} (ADR 0036 clause 3). Generated — runs the",
        "# production function only; the submitting process records the version.",
        "set -euo pipefail",
    ]
    if root is not None:
        lines.append(f'export PYTHONPATH={shlex.quote(str(root))}"${{PYTHONPATH:+:$PYTHONPATH}}"')
    lines.append(
        f"exec {shlex.quote(python)} -m examlops.assets.job"
        f" --asset {shlex.quote(asset_name)}"
        f" --entrypoint {shlex.quote(entrypoint)}"
        f" --upstream {shlex.quote(json.dumps(upstream, sort_keys=True))}"
    )
    return "\n".join(lines) + "\n"


def _import_root(fn: Any) -> Any:
    """The sys.path entry that makes ``fn.__module__`` importable, from the module's file."""
    import sys
    from pathlib import Path

    module = sys.modules.get(getattr(fn, "__module__", "") or "")
    file = getattr(module, "__file__", None)
    if not file:
        return None
    path = Path(file).resolve()
    if path.name == "__init__.py":
        path = path.parent
    depth = len(fn.__module__.split("."))
    return path.parents[depth - 1]


def _job_dir() -> Any:
    """Where generated job scripts are kept: ``EXAMLOPS_ASSET_JOB_DIR``, else
    ``$XDG_CACHE_HOME/examlops/asset-jobs``.

    Never the adapter's working directory, which for the mock sits inside the repository: a
    ``run.sh`` holding this host's absolute paths is one ``git add -A`` away from being published.
    Kept after the run, because the script is the exact record of what a job was asked to do.
    """
    import os
    from pathlib import Path

    configured = os.getenv("EXAMLOPS_ASSET_JOB_DIR")
    if configured:
        return Path(configured)
    cache = os.getenv("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "examlops" / "asset-jobs"


def _repo_root() -> Any:
    """The checkout this module runs from — the file's one repository coupling (ADR 0128's
    ratchet): where the phase-23 adapters live, and what `EXAMLOPS_HPC_REMOTE_REPO` re-roots."""
    from pathlib import Path

    return Path(__file__).resolve().parents[5]


def _log_tail(adapter: Any, job_id: str, lines: int = 20) -> str:
    try:
        text = str(adapter.get_job_logs(job_id) or "")
    except Exception:  # noqa: BLE001 - the state is the finding; logs are a courtesy
        return ""
    tail = "\n".join(text.strip().splitlines()[-lines:])
    return f" — last log lines:\n{tail}" if tail else ""


def _record_asset_job(job_id: str, scheduler: str, asset_name: str, resources: dict) -> None:
    """Best-effort `hpc_jobs` row, so an asset build appears in `exa hpc jobs` like a training."""
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
            model=f"asset:{asset_name}",
            dataset="",
            nodes=_int(resources.get("nodes")),
            gpus=_int(resources.get("gpus")),
            cpus=_int(resources.get("cpus_per_task")),
        )
    except Exception:  # noqa: BLE001 - bookkeeping never fails a build
        pass


def _finish_asset_job(job_id: str, scheduler: str, status: dict) -> None:
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


class PrefectOrchestrator:
    """Run the materialization as a **Prefect flow run** — clause 1's "thin asset layer
    generating Prefect runs".

    One flow run per asset (flow ``examlops-asset-materialize``, run name ``asset:<name>``) with
    the production function as its task, so every build shows up in the Prefect UI with its
    state, duration and logs, and one called from inside a Prefect flow becomes that flow's
    subflow. The run executes **in this process**: the version bump that follows it is the only
    one, and an in-process closure needs no deployment or worker to reach it.

    Retries are opt-in (``EXAMLOPS_ASSET_PREFECT_RETRIES``, default 0), because a production
    function that failed halfway is not known to be safe to repeat.

    **Prefect's absence is an environment fact, not an asset failure** (the scheduler's rule). With
    no Prefect API configured, with the package missing, or with the server unreachable, the
    asset is built locally and the provenance says why. The judge is whether the production
    function *started*: an error before that is Prefect's, and falls back; an error after it is
    the asset's, and propagates exactly as it would under ``local``. The no-API case falls back
    rather than letting Prefect start an ephemeral server, because a run recorded in a throwaway
    database under ``~/.prefect`` is a run nobody can see.
    """

    name = "prefect"

    def run(self, definition: Any, upstream: dict[str, int]) -> dict[str, Any]:
        asset_name = getattr(definition, "name", None) or "asset"
        try:
            from prefect import flow, task
            from prefect.settings import PREFECT_API_URL
        except ImportError as exc:
            return self._local(definition, upstream, f"prefect is not installed ({exc})")
        if not PREFECT_API_URL.value():
            return self._local(definition, upstream, "no Prefect API configured (PREFECT_API_URL)")

        started = False

        def _produce() -> None:
            nonlocal started
            started = True
            LocalOrchestrator().run(definition, upstream)

        produce = task(
            name=f"produce:{asset_name}",
            retries=_env_int("EXAMLOPS_ASSET_PREFECT_RETRIES", 0),
            retry_delay_seconds=_env_int("EXAMLOPS_ASSET_PREFECT_RETRY_DELAY", 10),
        )(_produce)

        @flow(name="examlops-asset-materialize", flow_run_name=f"asset:{asset_name}")
        def _materialize() -> str:
            from prefect.runtime import flow_run

            produce()
            return str(flow_run.id)

        try:
            flow_run_id = _materialize()
        except Exception as exc:
            if started:
                raise  # the production function failed: the asset's error, not Prefect's
            return self._local(definition, upstream, f"Prefect unavailable ({exc})")
        return {"orchestrator": self.name, "prefect_flow_run_id": flow_run_id}

    def _local(self, definition: Any, upstream: dict[str, int], why: str) -> dict[str, Any]:
        LocalOrchestrator().run(definition, upstream)
        return {"orchestrator": self.name, "fallback": f"local ({why})"}


def _env_int(var: str, default: int) -> int:
    import os

    try:
        return max(0, int(os.getenv(var, "")))
    except ValueError:
        return default


def _scheduler_adapter() -> Any:
    """The phase-23 adapter for the configured scheduler (mock / slurm / flux)."""
    import sys

    adapter_dir = _repo_root() / "platform" / "infra" / "slurm-adapter"
    if str(adapter_dir) not in sys.path:
        sys.path.insert(0, str(adapter_dir))
    from adapter import get_scheduler_adapter  # noqa: PLC0415

    return get_scheduler_adapter()


_ORCHESTRATORS: dict[str, Callable[[], Any]] = {
    "local": LocalOrchestrator,
    "scheduler": SchedulerOrchestrator,
    "prefect": PrefectOrchestrator,
}


def get_orchestrator(name: str | None = None) -> Any:
    """Resolve the orchestrator — explicit name, else ``EXAMLOPS_ASSET_ORCHESTRATOR``, else local.

    An unrecognised name resolves to ``local``: a typo must leave the asset built, not silently
    routed to an engine nobody configured.
    """
    import os

    chosen = (name or os.getenv("EXAMLOPS_ASSET_ORCHESTRATOR") or "local").strip().lower()
    return _ORCHESTRATORS.get(chosen, LocalOrchestrator)()


def _run_asset(
    name: str, *, run_id: str | None, actor: str | None, orchestrator: str | None = None
) -> int:
    row = platform_db.get_asset(name)
    deps = row["deps"] if row else []
    definition = _REGISTRY.get(name)
    # Provide upstream versions so a production fn can be revision-aware.
    upstream = {d: _current_version(d) for d in deps if d in _REGISTRY}
    provenance = get_orchestrator(orchestrator).run(definition, upstream)
    built_from = {d: _current_version(d) for d in deps}
    version = platform_db.bump_asset_version(name, built_from, run_id=run_id, actor=actor)
    _emit_lineage(name, deps, version, run_id, provenance=provenance)
    return version


def mark_source_changed(name: str, *, actor: str | None = None) -> int:
    """Advance a source asset's version (e.g. an A1 dataset revision landed) (GWT-2).

    Downstream feature/model assets become stale on their next status check.
    """
    if platform_db.get_asset(name) is None:
        declare_asset(name, kind="dataset")
    version = platform_db.bump_asset_version(name, {}, actor=actor)
    _emit_lineage(name, [], version, None)
    return version


def _emit_lineage(
    name: str,
    deps: list[str],
    version: int,
    run_id: str | None,
    *,
    provenance: dict[str, Any] | None = None,
) -> None:
    """Emit an OpenLineage event so the asset DAG coincides with the lineage graph (R6).

    The orchestrator that produced the version, and the scheduler job when there was one, ride
    along as facets: an asset built on a cluster and one built in a notebook are different facts,
    and a graph that cannot tell them apart cannot answer where a version came from.
    """
    try:
        from examlops.lineage import Node, emit_lineage, hpc_job_id_facet

        prov = provenance or {}
        facets: dict[str, Any] = {"orchestrator": prov.get("orchestrator", "local")}
        if prov.get("fallback"):
            facets["fallback"] = prov["fallback"]
        if prov.get("hpc_job_id"):
            facets.update(hpc_job_id_facet(str(prov["hpc_job_id"])))
        if prov.get("prefect_flow_run_id"):
            facets["prefect_flow_run_id"] = str(prov["prefect_flow_run_id"])
        emit_lineage(
            "COMPLETE",
            job=f"asset:{name}",
            run_id=run_id or f"asset-{name}-v{version}",
            inputs=[Node(name=d, type="dataset") for d in deps],
            outputs=[Node(name=f"{name}/v{version}", type="dataset")],
            facets=facets,
        )
    except Exception:  # fail-open — lineage is bookkeeping (R6)
        pass


def _policy_block(action: str, context: dict[str, Any]) -> str | None:
    """Return a denial reason if policy denies ``action``, else None (fail-open) (R7/D5)."""
    try:
        from examlops.policy import decide

        decision = decide(action, context)
        if decision.denied:
            return decision.reason
    except Exception:
        return None
    return None


def build_dag() -> dict[str, list[str]]:
    """Return the asset DAG as ``{asset: [upstream, ...]}`` (R1/GWT-1)."""
    return {a["name"]: a["deps"] for a in platform_db.list_assets()}


__all__ = [
    "AssetBuildError",
    "AssetDef",
    "Freshness",
    "MaterializeResult",
    "asset",
    "declare_asset",
    "asset_status",
    "materialize",
    "mark_source_changed",
    "build_dag",
]
