"""
Read-only HPC resource discovery — auto-detect the scheduler and enumerate resources.

This module is the "propose, never connect" half of the fleet story: given a
:class:`RemoteExecutor` (local subprocess or SSH), it probes a candidate login node with
side-effect-free commands (``flux resource list``, ``sinfo``, ``scontrol show node``,
``nvidia-smi``) and returns a *normalized* inventory. It never submits, cancels, or mutates
anything on the cluster, and it never decides to use a cluster — that is the registry +
sysadmin-approval layer's job (see ``clusters.yaml`` / ``hpc_clusters``).

Pluggability: every backend implements the :class:`SchedulerProbe` protocol and registers
itself via :func:`register_probe`. Adding PBS/LSF/k8s/cloud later means dropping in one new
probe module — the CLI, registry, and placement layers speak only the normalized vocab
(:class:`NodeInfo`, :class:`GpuInfo`, :class:`ClusterCaps`) and never learn a new scheduler's
CLI.

The module has **no hard dependency** on ``executor.py`` — it only needs an object exposing
``run(cmd: list[str]) -> object`` where the result has ``returncode``/``stdout``/``stderr``.
That keeps it trivially unit-testable with a fake executor and decoupled from the transport.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from executor import CompletedCommand, RemoteExecutor


# ── Normalized vocabulary ────────────────────────────────────────────────────────

# Normalized node states shared across every scheduler.
NODE_STATES = ("idle", "allocated", "mixed", "down", "drain", "unknown")


@dataclass
class GpuInfo:
    """One GPU device (or an aggregate count when the scheduler can't see devices)."""

    node: str
    index: int | None = None
    model: str | None = None
    memory_mb: int | None = None
    used_memory_mb: int | None = None
    utilization_pct: float | None = None
    state: str = "unknown"  # online | offline | unknown

    def to_dict(self) -> dict:
        return {
            "node": self.node,
            "index": self.index,
            "model": self.model,
            "memory_mb": self.memory_mb,
            "used_memory_mb": self.used_memory_mb,
            "utilization_pct": self.utilization_pct,
            "state": self.state,
        }


@dataclass
class NodeInfo:
    """A compute node normalized across schedulers."""

    name: str
    cpus: int | None = None
    memory_mb: int | None = None
    gpus: int = 0
    gpu_model: str | None = None
    state: str = "unknown"  # one of NODE_STATES
    partition: str | None = None  # Slurm partition / Flux queue

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "gpus": self.gpus,
            "gpu_model": self.gpu_model,
            "state": self.state,
            "partition": self.partition,
        }


@dataclass
class ClusterCaps:
    """What a probe learned about a candidate cluster (the 'suggested config')."""

    scheduler: str  # flux | slurm | unmanaged | unknown
    available: bool = False
    version: str | None = None
    has_accounting: bool = False
    has_gpu: bool = False
    total_nodes: int = 0
    total_cpus: int = 0
    total_gpus: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "scheduler": self.scheduler,
            "available": self.available,
            "version": self.version,
            "has_accounting": self.has_accounting,
            "has_gpu": self.has_gpu,
            "total_nodes": self.total_nodes,
            "total_cpus": self.total_cpus,
            "total_gpus": self.total_gpus,
            "notes": self.notes,
        }


@runtime_checkable
class SchedulerProbe(Protocol):
    """Read-only backend detector + resource enumerator."""

    name: str

    def available(self, executor: RemoteExecutor) -> bool: ...

    def capabilities(self, executor: RemoteExecutor) -> ClusterCaps: ...

    def list_nodes(self, executor: RemoteExecutor) -> list[NodeInfo]: ...

    def list_gpus(self, executor: RemoteExecutor) -> list[GpuInfo]: ...


# ── Probe registry (pluggable) ───────────────────────────────────────────────────

_PROBES: dict[str, SchedulerProbe] = {}
# Detection order: real schedulers first (they own the node), then the unmanaged fallback.
_DETECT_ORDER = ["flux", "slurm"]


def register_probe(probe: SchedulerProbe) -> SchedulerProbe:
    """Register a probe under ``probe.name`` (idempotent; last registration wins)."""
    _PROBES[probe.name] = probe
    return probe


def get_probe(name: str) -> SchedulerProbe | None:
    return _PROBES.get(name)


def registered_probes() -> dict[str, SchedulerProbe]:
    return dict(_PROBES)


# ── Small helpers ─────────────────────────────────────────────────────────────────


def _ok(result: CompletedCommand | object) -> bool:
    return getattr(result, "returncode", 1) == 0


def _out(result: CompletedCommand | object) -> str:
    return (getattr(result, "stdout", "") or "").strip()


def _run(executor: RemoteExecutor, cmd: list[str]) -> CompletedCommand | object:
    """Run a command, swallowing transport errors into a non-zero sentinel result."""
    try:
        return executor.run(cmd)
    except Exception:  # noqa: BLE001 - discovery must never raise on a flaky probe
        return _Failed()


@dataclass
class _Failed:
    returncode: int = 127
    stdout: str = ""
    stderr: str = ""


def _to_mb(value: str) -> int | None:
    """Parse a memory token like ``64000``, ``64G``, ``512000M`` → megabytes."""
    value = value.strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)B?$", value, re.IGNORECASE)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2).upper()
    factor = {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit]
    # Round (not truncate) so KB-reported memory doesn't collapse to 0/1 MB
    # (e.g. ``2000K`` → 2 MB, not 1).
    return round(num * factor)


def expand_hostlist(spec: str) -> list[str]:
    """Expand an RFC-style hostlist (``node[1-3,5]``, ``a,b``, ``foo``) into names.

    Handles the common bracketed-range form used by both Slurm and Flux nodelists. Ranges
    keep zero-padding (``node[01-03]`` → ``node01 node02 node03``). Anything it can't parse
    is returned verbatim as a single element so discovery never loses a node.
    """
    spec = spec.strip()
    if not spec or spec in ("(null)", "None"):
        return []
    hosts: list[str] = []
    # Split on commas that are NOT inside brackets.
    parts, depth, buf = [], 0, ""
    for ch in spec:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        parts.append(buf)

    for part in parts:
        m = re.match(r"^(.*?)\[([0-9,\-]+)\](.*)$", part)
        if not m:
            hosts.append(part)
            continue
        prefix, ranges, suffix = m.group(1), m.group(2), m.group(3)
        # A suffix may hold a second bracket group (multi-dimensional hostlists like
        # ``rack[1-2]node[3-4]``). Recurse on each generated candidate so both
        # dimensions expand instead of the second bracket surviving verbatim.
        recurse = "[" in suffix

        def _emit(name: str) -> None:
            hosts.extend(expand_hostlist(name) if recurse else [name])

        for rng in ranges.split(","):
            if "-" in rng:
                lo_s, hi_s = rng.split("-", 1)
                width = len(lo_s)
                for n in range(int(lo_s), int(hi_s) + 1):
                    _emit(f"{prefix}{str(n).zfill(width)}{suffix}")
            else:
                _emit(f"{prefix}{rng}{suffix}")
    return hosts


def _normalize_slurm_state(raw: str) -> str:
    s = raw.strip().rstrip("*~#$@+").lower()
    if s in ("idle",):
        return "idle"
    if s in ("alloc", "allocated"):
        return "allocated"
    if s in ("mix", "mixed"):
        return "mixed"
    if s.startswith("down") or s in ("fail", "failed", "error"):
        return "down"
    if s.startswith("drain") or s in ("drng", "maint", "resv"):
        return "drain"
    return "unknown"


def _normalize_flux_state(raw: str) -> str:
    s = raw.strip().lower()
    return {
        "free": "idle",
        "avail": "idle",
        "up": "idle",
        "allocated": "allocated",
        "alloc": "allocated",
        "down": "down",
        "drain": "drain",
        "exclude": "drain",
    }.get(s, "unknown")


def _parse_slurm_gres(gres: str) -> tuple[int, str | None]:
    """Parse a Slurm GRES token like ``gpu:a100:4`` / ``gpu:4`` → (count, model)."""
    gres = gres.strip()
    if not gres or gres in ("(null)", "N/A"):
        return 0, None
    total, model = 0, None
    for item in gres.split(","):
        item = item.strip()
        if not item.startswith("gpu"):
            continue
        # Strip the socket-affinity suffix "(S:0-1)" first — it contains a colon that would
        # otherwise break the field split.
        item = re.sub(r"\(.*?\)", "", item)
        fields = item.split(":")
        # gpu:<model>:<count>  or  gpu:<count>
        if len(fields) == 3:
            model = fields[1]
            count = fields[2]
        elif len(fields) == 2:
            count = fields[1]
        else:
            continue
        try:
            total += int(count)
        except ValueError:
            continue
    return total, model


# ── Flux probe ────────────────────────────────────────────────────────────────────


class FluxProbe:
    name = "flux"

    def available(self, executor: RemoteExecutor) -> bool:
        return _ok(_run(executor, ["flux", "version"]))

    def _version(self, executor: RemoteExecutor) -> str | None:
        res = _run(executor, ["flux", "version"])
        if not _ok(res):
            return None
        for line in _out(res).splitlines():
            if line.lower().startswith("commands:"):
                return line.split(":", 1)[1].strip()
        first = _out(res).splitlines()
        return first[0].strip() if first else None

    def _has_accounting(self, executor: RemoteExecutor) -> bool:
        return _ok(_run(executor, ["flux", "account", "--help"]))

    def list_nodes(self, executor: RemoteExecutor) -> list[NodeInfo]:
        # State-grouped rows; expand each group's hostlist into per-node entries.
        res = _run(
            executor,
            ["flux", "resource", "list", "-no", "{state} {nnodes} {ncores} {ngpus} {nodelist}"],
        )
        nodes: list[NodeInfo] = []
        if not _ok(res):
            return nodes
        for line in _out(res).splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            state = _normalize_flux_state(fields[0])
            try:
                nnodes = int(fields[1])
                ncores = int(fields[2])
                ngpus = int(fields[3])
            except ValueError:
                continue
            nodelist = fields[4] if len(fields) > 4 else ""
            # When Flux reports a count but no nodelist, synthesise *distinct* names —
            # identical names collide on the ``hpc_nodes`` primary key and make N nodes
            # look like one, undercounting capacity/GPU headroom for placement.
            names = expand_hostlist(nodelist) or [
                f"{fields[0]}-group-{i}" for i in range(max(nnodes, 0))
            ]
            per_cpu = ncores // len(names) if names else ncores
            per_gpu = ngpus // len(names) if names else ngpus
            for nm in names:
                nodes.append(
                    NodeInfo(name=nm, cpus=per_cpu, gpus=per_gpu, state=state, partition=None)
                )
        return nodes

    def list_gpus(self, executor: RemoteExecutor) -> list[GpuInfo]:
        # Flux tracks GPU counts, not device models. Emit count-only placeholders;
        # nvidia-smi (NvidiaSmiProbe) supplies model/memory/util when reachable.
        gpus: list[GpuInfo] = []
        for node in self.list_nodes(executor):
            for i in range(node.gpus):
                gpus.append(GpuInfo(node=node.name, index=i, state="unknown"))
        return gpus

    def capabilities(self, executor: RemoteExecutor) -> ClusterCaps:
        if not self.available(executor):
            return ClusterCaps(scheduler="flux", available=False)
        nodes = self.list_nodes(executor)
        total_gpus = sum(n.gpus for n in nodes)
        caps = ClusterCaps(
            scheduler="flux",
            available=True,
            version=self._version(executor),
            has_accounting=self._has_accounting(executor),
            has_gpu=total_gpus > 0,
            total_nodes=len(nodes),
            total_cpus=sum(n.cpus or 0 for n in nodes),
            total_gpus=total_gpus,
        )
        if not caps.has_accounting:
            caps.notes.append("flux-accounting not detected — --bank/--queue flags may be ignored")
        return caps


# ── Slurm probe ─────────────────────────────────────────────────────────────────


class SlurmProbe:
    name = "slurm"

    def available(self, executor: RemoteExecutor) -> bool:
        return _ok(_run(executor, ["sinfo", "--version"]))

    def _version(self, executor: RemoteExecutor) -> str | None:
        res = _run(executor, ["sinfo", "--version"])
        return _out(res) or None if _ok(res) else None

    def _has_accounting(self, executor: RemoteExecutor) -> bool:
        return _ok(_run(executor, ["sacctmgr", "--version"]))

    def list_nodes(self, executor: RemoteExecutor) -> list[NodeInfo]:
        # %N node, %c cpus, %m mem(MB), %G gres, %t state(compact), %P partition.
        res = _run(executor, ["sinfo", "-N", "-h", "-o", "%N|%c|%m|%G|%t|%P"])
        nodes: list[NodeInfo] = []
        if not _ok(res):
            return nodes
        seen: set[str] = set()
        for line in _out(res).splitlines():
            fields = line.split("|")
            if len(fields) < 6:
                continue
            name, cpus_s, mem_s, gres_s, state_s, part_s = (f.strip() for f in fields[:6])
            # A node appears once per partition; keep the first (partition kept on that row).
            if name in seen:
                continue
            seen.add(name)
            gpus, model = _parse_slurm_gres(gres_s)
            nodes.append(
                NodeInfo(
                    name=name,
                    cpus=int(cpus_s) if cpus_s.isdigit() else None,
                    memory_mb=_to_mb(mem_s),
                    gpus=gpus,
                    gpu_model=model,
                    state=_normalize_slurm_state(state_s),
                    partition=part_s.rstrip("*") or None,
                )
            )
        return nodes

    def list_gpus(self, executor: RemoteExecutor) -> list[GpuInfo]:
        gpus: list[GpuInfo] = []
        for node in self.list_nodes(executor):
            for i in range(node.gpus):
                gpus.append(GpuInfo(node=node.name, index=i, model=node.gpu_model, state="unknown"))
        return gpus

    def capabilities(self, executor: RemoteExecutor) -> ClusterCaps:
        if not self.available(executor):
            return ClusterCaps(scheduler="slurm", available=False)
        nodes = self.list_nodes(executor)
        total_gpus = sum(n.gpus for n in nodes)
        return ClusterCaps(
            scheduler="slurm",
            available=True,
            version=self._version(executor),
            has_accounting=self._has_accounting(executor),
            has_gpu=total_gpus > 0,
            total_nodes=len(nodes),
            total_cpus=sum(n.cpus or 0 for n in nodes),
            total_gpus=total_gpus,
        )


# ── nvidia-smi probe (unmanaged GPU hosts) ────────────────────────────────────────


class NvidiaSmiProbe:
    """Live GPU inventory for hosts with no scheduler (e.g. remote-gpu01, 4× A16)."""

    name = "nvidia-smi"

    def available(self, executor: RemoteExecutor) -> bool:
        return _ok(_run(executor, ["nvidia-smi", "-L"]))

    def _hostname(self, executor: RemoteExecutor) -> str:
        res = _run(executor, ["hostname"])
        return _out(res) or "localhost"

    def list_gpus(self, executor: RemoteExecutor) -> list[GpuInfo]:
        res = _run(
            executor,
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
        )
        gpus: list[GpuInfo] = []
        if not _ok(res):
            return gpus
        host = self._hostname(executor)
        for line in _out(res).splitlines():
            fields = [c.strip() for c in line.split(",")]
            if len(fields) < 5:
                continue
            idx, name, mtot, mused, util = fields[:5]
            gpus.append(
                GpuInfo(
                    node=host,
                    index=int(idx) if idx.isdigit() else None,
                    model=name or None,
                    memory_mb=int(mtot) if mtot.isdigit() else None,
                    used_memory_mb=int(mused) if mused.isdigit() else None,
                    utilization_pct=float(util) if util.replace(".", "", 1).isdigit() else None,
                    state="online",
                )
            )
        return gpus

    def list_nodes(self, executor: RemoteExecutor) -> list[NodeInfo]:
        gpus = self.list_gpus(executor)
        if not gpus:
            return []
        host = gpus[0].node
        return [
            NodeInfo(
                name=host,
                gpus=len(gpus),
                gpu_model=gpus[0].model,
                state="idle",
                partition=None,
            )
        ]

    def capabilities(self, executor: RemoteExecutor) -> ClusterCaps:
        if not self.available(executor):
            return ClusterCaps(scheduler="unmanaged", available=False)
        gpus = self.list_gpus(executor)
        caps = ClusterCaps(
            scheduler="unmanaged",
            available=True,
            has_gpu=len(gpus) > 0,
            total_nodes=1 if gpus else 0,
            total_gpus=len(gpus),
        )
        caps.notes.append("no scheduler — GPUs visible via nvidia-smi only (manual placement)")
        return caps


register_probe(FluxProbe())
register_probe(SlurmProbe())
register_probe(NvidiaSmiProbe())


# ── Top-level detection ───────────────────────────────────────────────────────────


def probe_scheduler(executor: RemoteExecutor) -> ClusterCaps:
    """Detect which scheduler runs on the host and return its capabilities.

    Tries real schedulers in :data:`_DETECT_ORDER` (Flux first — it owns the remote nodes),
    then falls back to the ``nvidia-smi`` unmanaged probe, then to ``unknown``. Read-only.
    """
    for name in _DETECT_ORDER:
        probe = _PROBES.get(name)
        if probe and probe.available(executor):
            return probe.capabilities(executor)
    nsmi = _PROBES.get("nvidia-smi")
    if nsmi and nsmi.available(executor):
        return nsmi.capabilities(executor)
    return ClusterCaps(scheduler="unknown", available=False, notes=["no known scheduler detected"])


def discover_inventory(executor: RemoteExecutor, scheduler: str | None = None) -> dict:
    """Return a full normalized inventory: caps + nodes + gpus.

    ``scheduler`` forces a specific probe; otherwise the scheduler is auto-detected. When a
    real scheduler is present but reports zero GPUs, the ``nvidia-smi`` probe is layered on
    to enrich GPU device details (model/memory) if reachable.
    """
    if scheduler:
        probe = _PROBES.get(scheduler)
        if probe is None:
            raise ValueError(f"unknown scheduler probe: {scheduler!r}")
        caps = probe.capabilities(executor)
    else:
        caps = probe_scheduler(executor)
        probe = _PROBES.get(caps.scheduler)

    nodes = probe.list_nodes(executor) if probe else []
    gpus = probe.list_gpus(executor) if probe else []

    # Enrich with live GPU devices from nvidia-smi when the scheduler only knows counts.
    nsmi = _PROBES.get("nvidia-smi")
    if (
        nsmi
        and probe is not nsmi
        and any(g.model is None for g in gpus)
        and nsmi.available(executor)
    ):
        smi_gpus = nsmi.list_gpus(executor)
        if smi_gpus:
            gpus = smi_gpus
            caps.notes.append("GPU device details enriched via nvidia-smi")

    return {
        "capabilities": caps.to_dict(),
        "nodes": [n.to_dict() for n in nodes],
        "gpus": [g.to_dict() for g in gpus],
    }


# ── live queue (Phase 35c) ────────────────────────────────────────────────────────

_FLUX_QUEUE_STATE = {
    "RUN": "RUNNING",
    "CLEANUP": "RUNNING",
    "DEPEND": "PENDING",
    "PRIORITY": "PENDING",
    "SCHED": "PENDING",
    "INACTIVE": "DONE",
}
_SLURM_QUEUE_STATE = {
    "RUNNING": "RUNNING",
    "R": "RUNNING",
    "PENDING": "PENDING",
    "PD": "PENDING",
    "CONFIGURING": "PENDING",
    "COMPLETING": "RUNNING",
}


def queue_jobs(executor: RemoteExecutor, scheduler: str) -> list[dict]:
    """Return the live scheduler queue, normalized to id/name/user/state/nodes.

    Read-only. ``scheduler`` is ``flux`` or ``slurm``; anything else yields ``[]``.
    """
    if scheduler == "flux":
        # Use a ``|`` delimiter (like squeue) so job names containing spaces don't
        # shift every subsequent field and corrupt the parsed state/node count.
        res = _run(
            executor,
            ["flux", "jobs", "-a", "--no-header", "-o", "{id}|{name}|{username}|{state}|{nnodes}"],
        )
        state_map = _FLUX_QUEUE_STATE
    elif scheduler == "slurm":
        res = _run(executor, ["squeue", "-h", "-o", "%i|%j|%u|%T|%D"])
        state_map = _SLURM_QUEUE_STATE
    else:
        return []
    if not _ok(res):
        return []

    jobs: list[dict] = []
    for line in _out(res).splitlines():
        # Split from the right so a name containing ``|`` (rare) still leaves the
        # trailing four fixed fields intact.
        fields = line.rsplit("|", 4)
        if len(fields) < 5:
            continue
        job_id, name, user, state, nnodes = fields[:5]
        nnodes = nnodes.strip()
        jobs.append(
            {
                "job_id": job_id,
                "name": name,
                "user": user,
                "state": state_map.get(state.upper(), state.upper()),
                "nodes": int(nnodes) if nnodes.isdigit() else None,
            }
        )
    return jobs


def preflight(executor: RemoteExecutor, scheduler: str, ask: dict | None = None) -> list[dict]:
    """Fail-fast pre-submit checks. Returns a list of ``{check, ok, detail}`` results.

    Read-only: it verifies the transport is reachable, the scheduler responds, and the
    requested resources could exist — it never submits anything.
    """
    ask = ask or {}
    checks: list[dict] = []

    reachable = _ok(_run(executor, ["hostname"]))
    checks.append({"check": "transport", "ok": reachable, "detail": "host reachable"})
    if not reachable:
        return checks  # nothing else is meaningful if we can't reach the host

    probe = _PROBES.get(scheduler)
    responds = bool(probe and probe.available(executor))
    checks.append(
        {"check": "scheduler", "ok": responds, "detail": f"{scheduler} responds to queries"}
    )
    if not responds:
        return checks

    caps = probe.capabilities(executor)
    want_gpus = int(ask.get("gpus", 0) or 0)
    if want_gpus > 0:
        ok = caps.total_gpus >= want_gpus
        checks.append(
            {
                "check": "gpus",
                "ok": ok,
                "detail": f"asked {want_gpus}, cluster has {caps.total_gpus}",
            }
        )
    want_nodes = int(ask.get("nodes", 0) or 0)
    if want_nodes > 0:
        ok = caps.total_nodes >= want_nodes
        checks.append(
            {
                "check": "nodes",
                "ok": ok,
                "detail": f"asked {want_nodes}, cluster has {caps.total_nodes}",
            }
        )
    return checks
