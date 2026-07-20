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
from typing import Any

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
) -> MaterializeResult:
    """Rebuild ``name`` + its stale ancestors only (R4/GWT-3), emit lineage (R6), audit (R7).

    Governed by policy (D5): a ``deny`` on ``asset_materialize`` blocks the run.
    """
    from examlops.data.audit import write_audit_event

    # Policy gate (D5) — default allow if no policy file.
    blocked = _policy_block("asset_materialize", {"asset": name, "actor": actor})
    if blocked is not None:
        write_audit_event(
            "exa-assets", actor, "asset_materialize_denied", name, {"reason": blocked}
        )
        return MaterializeResult(name, rebuilt=[], skipped=[], blocked=blocked)

    order = _topo_order(name)
    rebuilt: list[str] = []
    skipped: list[str] = []
    for node in order:
        needs = force or not asset_status(node).fresh
        if not needs:
            skipped.append(node)
            continue
        _run_asset(node, run_id=run_id, actor=actor)
        rebuilt.append(node)
    write_audit_event(
        "exa-assets",
        actor,
        "asset_materialize",
        name,
        {"rebuilt": rebuilt, "skipped": skipped, "forced": force},
    )
    return MaterializeResult(name, rebuilt=rebuilt, skipped=skipped)


def _run_asset(name: str, *, run_id: str | None, actor: str | None) -> int:
    row = platform_db.get_asset(name)
    deps = row["deps"] if row else []
    definition = _REGISTRY.get(name)
    if definition is not None and definition.fn is not None:
        # Provide upstream versions so a production fn can be revision-aware.
        definition.fn(**{d: _current_version(d) for d in deps if d in _REGISTRY})
    built_from = {d: _current_version(d) for d in deps}
    version = platform_db.bump_asset_version(name, built_from, run_id=run_id, actor=actor)
    _emit_lineage(name, deps, version, run_id)
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


def _emit_lineage(name: str, deps: list[str], version: int, run_id: str | None) -> None:
    """Emit an OpenLineage event so the asset DAG coincides with the lineage graph (R6)."""
    try:
        from examlops.lineage import Node, emit_lineage

        emit_lineage(
            "COMPLETE",
            job=f"asset:{name}",
            run_id=run_id or f"asset-{name}-v{version}",
            inputs=[Node(name=d, type="dataset") for d in deps],
            outputs=[Node(name=f"{name}/v{version}", type="dataset")],
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
