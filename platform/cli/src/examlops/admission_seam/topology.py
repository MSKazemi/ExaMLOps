"""The admission seam's typed resource graph (ADR 0116 decision 6, requirement G3.11).

Vertices are typed — ``cluster`` · ``node`` · ``accelerator`` · ``scale_up_domain`` · ``fabric`` ·
``power`` · ``storage`` — and so are edges: ``contains`` (a hierarchy: cluster→node,
node→accelerator, scale-up domain→node, power unit→node) and ``connects`` (fabric↔node,
storage↔node). Only the pairs in :data:`EDGE_RULES` are accepted; anything else is a programming
error and raises, so the graph cannot quietly grow a shape nobody decided on.

**Where the facts come from.** Nodes and their GPUs come from the fleet inventory the platform
already records (``hpc_nodes``, written by ``exa hpc nodes``). Everything the inventory cannot know
— which nodes share an NVLink/NVL72 scale-up domain, which power distribution unit feeds them, which
fabric and storage they reach — is *declared* by the site in a topology file
(``EXAMLOPS_RESOURCE_TOPOLOGY``, else ``<config dir>/topology.yaml``). None of it is inferred: a
cluster with no declared domains has an **unknown** scale-up topology, and the admission policy
then refuses to promise ``scale_up_domain: required`` rather than guessing (principle P5).

**Free capacity is counted conservatively.** A node's GPUs are free only when the inventory says the
node is ``idle``; ``mixed`` (partly allocated — the snapshot does not say how much) counts as zero.
Under-counting makes a ``required`` request wait; over-counting would place it spanning domains,
which is the failure ADR 0116 verification 2 exists to prevent.

**Not a scheduler.** The graph answers "could any single domain hold this?" for admission. Placement
stays with the backend — and where the backend is Flux, whose Fluxion resource graph already models
this natively, the backend's own answer is authoritative; this graph never overrides it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TOPOLOGY_ENV = "EXAMLOPS_RESOURCE_TOPOLOGY"
#: A node whose inventory row is older than this many seconds counts 0 free GPUs (``0`` disables).
MAX_AGE_ENV = "EXAMLOPS_RESOURCE_TOPOLOGY_MAX_AGE_S"
DEFAULT_MAX_AGE_S = 3600.0

VERTEX_KINDS = (
    "cluster",
    "node",
    "accelerator",
    "scale_up_domain",
    "fabric",
    "power",
    "storage",
)
EDGE_RULES: dict[str, frozenset[tuple[str, str]]] = {
    "contains": frozenset(
        {
            ("cluster", "node"),
            ("node", "accelerator"),
            ("scale_up_domain", "node"),
            ("power", "node"),
        }
    ),
    "connects": frozenset({("fabric", "node"), ("storage", "node")}),
}
#: Topology-file section -> the vertex kind it declares and the edge that joins it to its nodes.
_SECTIONS = {
    "scale_up_domains": ("scale_up_domain", "contains"),
    "power": ("power", "contains"),
    "fabrics": ("fabric", "connects"),
    "storage": ("storage", "connects"),
}
#: Node states whose GPUs are all free.
_FREE_STATES = frozenset({"idle"})
#: Bound on declared vertices per section (a topology file is config, not a data dump).
MAX_DECLARED = 10_000


class TopologyError(ValueError):
    """A topology document or graph operation that is not well formed."""


@dataclass(frozen=True)
class Vertex:
    id: str
    kind: str
    attrs: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ResourceGraph:
    vertices: dict[str, Vertex] = field(default_factory=dict)
    edges: set[tuple[str, str, str]] = field(default_factory=set)  # (kind, src, dst)
    #: Declared facts that did not fit (unknown node, a node in two domains). Reported, not fatal.
    problems: list[str] = field(default_factory=list)
    #: (edge kind, src) -> dst ids; an index so a fleet-sized graph is not scanned per query.
    _out: dict[tuple[str, str], set[str]] = field(default_factory=dict, repr=False)

    # ── construction ─────────────────────────────────────────────────────────────────────
    def add_vertex(self, vid: str, kind: str, attrs: Mapping[str, Any] | None = None) -> Vertex:
        if kind not in VERTEX_KINDS:
            raise TopologyError(f"unknown vertex kind {kind!r}; one of {list(VERTEX_KINDS)}")
        existing = self.vertices.get(vid)
        if existing is not None:
            if existing.kind != kind:
                raise TopologyError(f"vertex {vid!r} is a {existing.kind}, not a {kind}")
            return existing
        v = Vertex(vid, kind, dict(attrs or {}))
        self.vertices[vid] = v
        return v

    def add_edge(self, kind: str, src: str, dst: str) -> None:
        if kind not in EDGE_RULES:
            raise TopologyError(f"unknown edge kind {kind!r}")
        a, b = self.vertices.get(src), self.vertices.get(dst)
        if a is None or b is None:
            raise TopologyError(f"edge {kind} {src!r}->{dst!r} names a missing vertex")
        if (a.kind, b.kind) not in EDGE_RULES[kind]:
            raise TopologyError(f"a {a.kind} cannot {kind} a {b.kind}")
        self.edges.add((kind, src, dst))
        self._out.setdefault((kind, src), set()).add(dst)

    # ── queries ──────────────────────────────────────────────────────────────────────────
    def of_kind(self, kind: str) -> list[Vertex]:
        return sorted((v for v in self.vertices.values() if v.kind == kind), key=lambda v: v.id)

    def children(self, vid: str, *, kind: str = "contains") -> list[str]:
        return sorted(self._out.get((kind, vid), ()))

    def free_gpus(self, node_id: str) -> int:
        node = self.vertices[node_id]
        if node.attrs.get("stale"):
            return 0
        if str(node.attrs.get("state") or "").lower() not in _FREE_STATES:
            return 0
        return len([c for c in self.children(node_id) if self.vertices[c].kind == "accelerator"])

    def domain_free_gpus(self) -> dict[str, int]:
        """Free GPUs inside each declared scale-up domain."""
        return {
            d.id: sum(self.free_gpus(n) for n in self.children(d.id))
            for d in self.of_kind("scale_up_domain")
        }

    def largest_free_domain_gpus(self) -> int | None:
        """The admission input. ``None`` = no domain declared (topology unknown), never ``0``."""
        free = self.domain_free_gpus()
        return max(free.values()) if free else None

    def summary(self) -> dict[str, Any]:
        counts = {k: len(self.of_kind(k)) for k in VERTEX_KINDS}
        edge_counts: dict[str, int] = {}
        for kind, _, _ in self.edges:
            edge_counts[kind] = edge_counts.get(kind, 0) + 1
        free = self.domain_free_gpus()
        return {
            "vertices": counts,
            "edges": dict(sorted(edge_counts.items())),
            "scale_up_domains": [
                {
                    "id": d.id,
                    "nodes": len(self.children(d.id)),
                    "free_gpus": free[d.id],
                }
                for d in self.of_kind("scale_up_domain")
            ],
            "largest_free_domain_gpus": max(free.values()) if free else None,
            "problems": list(self.problems),
        }


def _node_id(cluster: str, node: str) -> str:
    return f"node:{cluster}/{node}"


def max_age_s() -> float:
    """How old a node's inventory row may be before its GPUs stop counting as free."""
    raw = os.getenv(MAX_AGE_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("%s=%r is not a number; using %s", MAX_AGE_ENV, raw, DEFAULT_MAX_AGE_S)
        return DEFAULT_MAX_AGE_S


def _is_stale(captured_at: Any, now: datetime, limit: float) -> bool:
    """True when a row is older than ``limit`` — or its age cannot be read (conservative)."""
    if limit <= 0 or captured_at is None:
        return False  # disabled, or a row with no timestamp column (a hand-built inventory)
    try:
        ts = datetime.fromisoformat(str(captured_at).strip())
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)  # SQLite CURRENT_TIMESTAMP is UTC
    return (now - ts).total_seconds() > limit


def build(
    inventory: Iterable[Mapping[str, Any]],
    topology: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    max_age: float | None = None,
) -> ResourceGraph:
    """Build the graph from ``hpc_nodes`` rows and an optional declared topology document.

    A node whose row is older than ``max_age`` seconds (default :func:`max_age_s`) is marked
    ``stale`` and counts no free GPUs: an ``idle`` read an hour ago is not evidence of room now,
    and over-counting is the failure this graph exists to prevent.
    """
    g = ResourceGraph()
    by_name: dict[str, list[str]] = {}
    at = now or datetime.now(UTC)
    limit = max_age_s() if max_age is None else max_age
    stale = 0
    for row in inventory:
        cluster = str(row.get("cluster") or "default")
        name = str(row.get("node") or "")
        if not name:
            continue
        cid = f"cluster:{cluster}"
        g.add_vertex(cid, "cluster", {"scheduler": row.get("scheduler")})
        nid = _node_id(cluster, name)
        is_stale = _is_stale(row.get("captured_at"), at, limit)
        stale += is_stale
        g.add_vertex(
            nid, "node", {"state": row.get("state"), "cpus": row.get("cpus"), "stale": is_stale}
        )
        g.add_edge("contains", cid, nid)
        by_name.setdefault(name, []).append(nid)
        for i in range(max(0, int(row.get("gpus") or 0))):
            aid = f"accelerator:{cluster}/{name}/{i}"
            g.add_vertex(aid, "accelerator", {"model": row.get("gpu_model")})
            g.add_edge("contains", nid, aid)

    if stale:
        g.problems.append(
            f"{stale} node(s) have an inventory snapshot older than {limit:.0f}s "
            f"({MAX_AGE_ENV}); their GPUs count as 0 free - refresh with `exa hpc nodes`"
        )
    if not topology:
        return g
    if not isinstance(topology, Mapping):
        raise TopologyError("a topology document must be a mapping")
    unknown = sorted(set(topology) - set(_SECTIONS))
    if unknown:
        raise TopologyError(f"unknown topology section(s) {unknown}; allowed {sorted(_SECTIONS)}")
    domain_of: dict[str, str] = {}
    for section, (kind, edge) in _SECTIONS.items():
        entries = topology.get(section) or {}
        if not isinstance(entries, Mapping):
            raise TopologyError(f"{section} must map a name to {{nodes: [...]}}")
        if len(entries) > MAX_DECLARED:
            raise TopologyError(f"{section} declares {len(entries)} entries (max {MAX_DECLARED})")
        for name, spec in entries.items():
            spec = spec or {}
            if not isinstance(spec, Mapping):
                raise TopologyError(f"{section}.{name} must be a mapping")
            nodes = spec.get("nodes") or []
            if not isinstance(nodes, list):
                raise TopologyError(f"{section}.{name}.nodes must be a list")
            attrs = {k: v for k, v in spec.items() if k != "nodes"}
            vid = f"{kind}:{name}"
            g.add_vertex(vid, kind, attrs)
            for ref in nodes:
                ref = str(ref)
                if "/" in ref:
                    cluster, _, node = ref.partition("/")
                    matches = (
                        [_node_id(cluster, node)] if _node_id(cluster, node) in g.vertices else []
                    )
                else:
                    matches = by_name.get(ref, [])
                if not matches:
                    g.problems.append(f"{section}.{name}: node {ref!r} is not in the inventory")
                    continue
                if len(matches) > 1:
                    g.problems.append(
                        f"{section}.{name}: node {ref!r} is ambiguous across clusters; "
                        "write it as <cluster>/<node>"
                    )
                    continue
                nid = matches[0]
                if kind == "scale_up_domain":
                    prior = domain_of.get(nid)
                    if prior is not None and prior != vid:
                        g.problems.append(
                            f"{nid} is declared in two scale-up domains ({prior}, {vid}); "
                            "kept in the first"
                        )
                        continue
                    domain_of[nid] = vid
                if edge == "contains":
                    g.add_edge("contains", vid, nid)
                else:
                    g.add_edge("connects", vid, nid)
    return g


def topology_path() -> Path | None:
    """The declared topology file, or ``None`` when the site has declared none."""
    raw = os.getenv(TOPOLOGY_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        from examlops.lifecycle.datadir import config_dir

        candidate = config_dir() / "topology.yaml"
    except Exception:  # noqa: BLE001 - no config dir means no declared topology
        return None
    return candidate if candidate.is_file() else None


def load_topology(path: Path | None = None) -> dict[str, Any] | None:
    """Read the topology file (YAML or JSON). Missing file named by the env var raises: a site
    that pointed at a topology and lost it must not silently fall back to "unknown"."""
    target = path or topology_path()
    if target is None:
        return None
    text = target.read_text(encoding="utf-8")
    if target.suffix.lower() == ".json":
        doc = json.loads(text)
    else:
        import yaml

        doc = yaml.safe_load(text)
    if doc is None:
        return None
    if not isinstance(doc, dict):
        raise TopologyError(f"{target}: a topology document must be a mapping")
    return doc


def current_graph() -> ResourceGraph:
    """The live graph: the latest ``hpc_nodes`` snapshot plus the declared topology."""
    from examlops.data.hpc import get_node_snapshot

    return build(get_node_snapshot(), load_topology())


def live_largest_free_domain_gpus() -> int | None:
    """What :func:`examlops.admission_seam.service.current_state` feeds the policy.

    Any failure (unreadable or malformed topology, datastore error) returns ``None`` — topology
    *unknown* — and logs why. ``None`` is the fail-closed answer: a ``required`` request is then
    refused a promise instead of being placed on a guess.
    """
    try:
        if topology_path() is None:  # nothing declared: unknown, and no inventory read needed
            return None
        return current_graph().largest_free_domain_gpus()
    except Exception as exc:  # noqa: BLE001 - unknown topology is the safe degradation
        log.warning("admission: resource topology unavailable, treated as unknown: %s", exc)
        return None
