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


@dataclass
class AssetDef:
    name: str
    kind: str = "model"  # dataset | feature | model
    deps: list[str] = field(default_factory=list)
    fn: Callable[..., Any] | None = None
    description: str | None = None


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
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare an asset and its upstream dependencies (R1).

    ::

        @asset(kind="model", deps=["dataset:PM100", "feature:jpcp_features"])
        def jpcp_model(**upstream): ...
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        asset_name = name or fn.__name__
        declare_asset(asset_name, kind=kind, deps=deps or [], description=description, fn=fn)
        return fn

    return wrap


def declare_asset(
    name: str,
    *,
    kind: str = "model",
    deps: list[str] | None = None,
    description: str | None = None,
    fn: Callable[..., Any] | None = None,
) -> AssetDef:
    """Register an asset without the decorator (also used for source/dataset assets)."""
    a = AssetDef(name=name, kind=kind, deps=deps or [], fn=fn, description=description)
    _REGISTRY[name] = a
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

    ``no_deps`` builds only ``name``. It exists for the scheduler orchestrator, whose submitted
    job re-enters this command for one asset — without it the job would re-walk the graph and
    submit again, once per ancestor, forever.

    ``orchestrator`` overrides ``EXAMLOPS_ASSET_ORCHESTRATOR`` for this call. The submitted job
    passes ``local`` explicitly for the same reason.
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
    """Submit the materialization through the phase-23 scheduler seam (clause 3).

    Submits ``exa assets materialize <name> --no-deps`` under the **local** orchestrator, because
    a scheduler job is a separate process and cannot call an in-process closure. The job id is
    returned as provenance and recorded on the asset version.

    **What this requires of a deployment, stated rather than assumed:** the job must be able to
    reach the same ``platform.db`` (shared filesystem, or the Postgres backend) and have `exa`
    installed, since it is the job that bumps the version. Under
    ``EXAMLOPS_HPC_SCHEDULER=mock`` the job runs inline and both hold trivially. Where they do
    not, the submission still records its job id and the version bump happens in the submitting
    process — the graph stays correct, and the compute simply ran elsewhere.
    """

    name = "scheduler"

    def run(self, definition: Any, upstream: dict[str, int]) -> dict[str, Any]:
        asset_name = getattr(definition, "name", None) or "asset"
        try:
            adapter = _scheduler_adapter()
        except Exception as exc:  # noqa: BLE001
            # A missing scheduler is an environment fact, not an asset failure. Fall back to
            # local execution and say so, rather than leaving the asset unbuilt.
            LocalOrchestrator().run(definition, upstream)
            return {"orchestrator": self.name, "fallback": f"local ({exc})"}
        job_id = adapter.submit_job(
            script_path=None,
            resources={"job_name": f"asset-{asset_name}"},
            training_data={
                "command": (f"exa assets materialize {asset_name} --no-deps --orchestrator local")
            },
        )
        return {"orchestrator": self.name, "hpc_job_id": str(job_id)}


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
    from pathlib import Path

    root = Path(__file__).resolve().parents[5]
    adapter_dir = root / "platform" / "infra" / "slurm-adapter"
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
