"""Unit tests for HPC fleet discovery (offline — no real cluster).

A prefix-matching ``FakeExecutor`` returns canned scheduler-CLI output so we can assert
scheduler auto-detection, node/GPU parsing across Flux/Slurm/nvidia-smi, hostlist
expansion, GRES parsing, and the nvidia-smi GPU enrichment path — all without SSH.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADAPTER_DIR = Path(__file__).resolve().parents[2] / "platform" / "infra" / "slurm-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

import discovery  # noqa: E402
from executor import CompletedCommand  # noqa: E402


class FakeExecutor:
    """Returns the first canned response whose argv prefix matches the command."""

    def __init__(self, responses: list[tuple[tuple, CompletedCommand]]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def run(self, cmd, *, timeout=None, cwd=None):
        self.calls.append(list(cmd))
        for prefix, resp in self.responses:
            if tuple(cmd[: len(prefix)]) == prefix:
                return resp
        return CompletedCommand(127, "", "command not found")

    def close(self):
        pass


def _cc(rc=0, out="", err=""):
    return CompletedCommand(rc, out, err)


# ── fixtures: canned cluster shapes ──────────────────────────────────────────────


def _flux_executor(ngpus=0, nodelist="lxp-cpu[01-02]", accounting=False):
    return FakeExecutor(
        [
            (("flux", "version"), _cc(0, "commands: 0.85.0\nlibflux-core: 0.85.0")),
            (("flux", "account"), _cc(0 if accounting else 1, "")),
            (
                ("flux", "resource", "list"),
                _cc(
                    0,
                    f"free 2 64 {ngpus} {nodelist}\nallocated 0 0 0 \ndown 0 0 0 ",
                ),
            ),
        ]
    )


def _slurm_executor(accounting=True):
    sinfo = (
        "node01|32|64000|gpu:a100:4|idle|gpu\n"
        "node02|32|64000|gpu:a100:4|alloc|gpu\n"
        "node03|16|32000|(null)|down*|cpu"
    )
    return FakeExecutor(
        [
            (("sinfo", "--version"), _cc(0, "slurm 23.11.1")),
            (("sacctmgr", "--version"), _cc(0 if accounting else 1, "slurm 23.11.1")),
            (("sinfo", "-N"), _cc(0, sinfo)),
        ]
    )


def _nvidia_executor(host="lxp-gpu01"):
    query = "0, NVIDIA A16, 16384, 512, 12\n1, NVIDIA A16, 16384, 0, 0"
    return FakeExecutor(
        [
            (("nvidia-smi", "-L"), _cc(0, "GPU 0: NVIDIA A16\nGPU 1: NVIDIA A16")),
            (("nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,utilization.gpu"),
             _cc(0, query)),
            (("hostname",), _cc(0, host)),
        ]
    )


# ── helper parsers ───────────────────────────────────────────────────────────────


def test_expand_hostlist():
    assert discovery.expand_hostlist("lxp-cpu[01-02]") == ["lxp-cpu01", "lxp-cpu02"]
    assert discovery.expand_hostlist("a,b") == ["a", "b"]
    assert discovery.expand_hostlist("node[1,3-4]") == ["node1", "node3", "node4"]
    assert discovery.expand_hostlist("(null)") == []
    assert discovery.expand_hostlist("foo") == ["foo"]
    assert discovery.expand_hostlist("n[08-10],m5") == ["n08", "n09", "n10", "m5"]


def test_parse_slurm_gres():
    assert discovery._parse_slurm_gres("gpu:a100:4") == (4, "a100")
    assert discovery._parse_slurm_gres("gpu:4") == (4, None)
    assert discovery._parse_slurm_gres("(null)") == (0, None)
    assert discovery._parse_slurm_gres("gpu:a100:2(S:0-1)") == (2, "a100")


def test_to_mb():
    assert discovery._to_mb("64000") == 64000
    assert discovery._to_mb("64G") == 64 * 1024
    assert discovery._to_mb("512000M") == 512000
    assert discovery._to_mb("bogus") is None


# ── Flux probe ───────────────────────────────────────────────────────────────────


def test_flux_probe_detects_and_lists_nodes():
    ex = _flux_executor()
    caps = discovery.probe_scheduler(ex).to_dict()
    assert caps["scheduler"] == "flux"
    assert caps["available"] is True
    assert caps["version"] == "0.85.0"
    assert caps["total_nodes"] == 2
    assert caps["total_cpus"] == 64
    assert caps["total_gpus"] == 0
    assert caps["has_accounting"] is False

    nodes = discovery.FluxProbe().list_nodes(ex)
    assert [n.name for n in nodes] == ["lxp-cpu01", "lxp-cpu02"]
    assert all(n.state == "idle" and n.cpus == 32 for n in nodes)


def test_flux_probe_with_gpus():
    ex = _flux_executor(ngpus=8, nodelist="gpunode[01-02]")
    caps = discovery.FluxProbe().capabilities(ex)
    assert caps.total_gpus == 8
    assert caps.has_gpu is True
    gpus = discovery.FluxProbe().list_gpus(ex)
    assert len(gpus) == 8
    assert all(g.model is None for g in gpus)  # flux knows counts, not device models


# ── Slurm probe ──────────────────────────────────────────────────────────────────


def test_slurm_probe_parses_sinfo_and_gres():
    ex = _slurm_executor()
    caps = discovery.SlurmProbe().capabilities(ex)
    assert caps.scheduler == "slurm"
    assert caps.version == "slurm 23.11.1"
    assert caps.total_nodes == 3
    assert caps.total_cpus == 80
    assert caps.total_gpus == 8
    assert caps.has_accounting is True

    nodes = {n.name: n for n in discovery.SlurmProbe().list_nodes(ex)}
    assert nodes["node01"].gpus == 4 and nodes["node01"].gpu_model == "a100"
    assert nodes["node01"].state == "idle" and nodes["node01"].partition == "gpu"
    assert nodes["node02"].state == "allocated"
    assert nodes["node03"].state == "down" and nodes["node03"].gpus == 0


# ── nvidia-smi probe ─────────────────────────────────────────────────────────────


def test_nvidia_smi_probe():
    ex = _nvidia_executor()
    caps = discovery.NvidiaSmiProbe().capabilities(ex)
    assert caps.scheduler == "unmanaged"
    assert caps.total_gpus == 2
    gpus = discovery.NvidiaSmiProbe().list_gpus(ex)
    assert [g.model for g in gpus] == ["NVIDIA A16", "NVIDIA A16"]
    assert gpus[0].memory_mb == 16384 and gpus[0].used_memory_mb == 512
    assert gpus[0].utilization_pct == 12.0 and gpus[0].state == "online"
    nodes = discovery.NvidiaSmiProbe().list_nodes(ex)
    assert len(nodes) == 1 and nodes[0].name == "lxp-gpu01" and nodes[0].gpus == 2


# ── top-level detection ──────────────────────────────────────────────────────────


def test_probe_scheduler_prefers_flux_over_slurm():
    ex = FakeExecutor(
        [
            (("flux", "version"), _cc(0, "commands: 0.85.0")),
            (("flux", "account"), _cc(1, "")),
            (("flux", "resource", "list"), _cc(0, "free 1 32 0 n1\n")),
            (("sinfo", "--version"), _cc(0, "slurm 23.11.1")),
        ]
    )
    assert discovery.probe_scheduler(ex).scheduler == "flux"


def test_probe_scheduler_unmanaged_fallback():
    # No flux, no slurm, but nvidia-smi answers.
    ex = _nvidia_executor()
    caps = discovery.probe_scheduler(ex)
    assert caps.scheduler == "unmanaged"
    assert caps.has_gpu is True


def test_probe_scheduler_unknown_when_nothing_present():
    ex = FakeExecutor([])  # everything returns rc 127
    caps = discovery.probe_scheduler(ex)
    assert caps.scheduler == "unknown"
    assert caps.available is False


def test_discover_inventory_enriches_gpus_via_nvidia_smi():
    # Flux reports 2 GPUs (count-only) AND nvidia-smi is reachable → devices enriched.
    ex = FakeExecutor(
        [
            (("flux", "version"), _cc(0, "commands: 0.85.0")),
            (("flux", "account"), _cc(1, "")),
            (("flux", "resource", "list"), _cc(0, "free 1 32 2 gpunode1\n")),
            (("nvidia-smi", "-L"), _cc(0, "GPU 0: NVIDIA A16\nGPU 1: NVIDIA A16")),
            (
                ("nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,utilization.gpu"),
                _cc(0, "0, NVIDIA A16, 16384, 0, 0\n1, NVIDIA A16, 16384, 0, 0"),
            ),
            (("hostname",), _cc(0, "gpunode1")),
        ]
    )
    inv = discovery.discover_inventory(ex)
    assert inv["capabilities"]["scheduler"] == "flux"
    assert [g["model"] for g in inv["gpus"]] == ["NVIDIA A16", "NVIDIA A16"]
    assert any("nvidia-smi" in note for note in inv["capabilities"]["notes"])


def test_discover_inventory_unknown_scheduler_raises():
    with pytest.raises(ValueError):
        discovery.discover_inventory(FakeExecutor([]), scheduler="bogus-sched")


def test_registered_probes_present():
    names = set(discovery.registered_probes())
    assert {"flux", "slurm", "nvidia-smi"} <= names


# ── node-snapshot persistence ────────────────────────────────────────────────────


def test_node_snapshot_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    nodes = [
        {
            "name": "n1",
            "cpus": 32,
            "memory_mb": 64000,
            "gpus": 4,
            "gpu_model": "a100",
            "state": "idle",
            "partition": "gpu",
        }
    ]
    assert platform_db.record_node_snapshot("lxp", "flux", nodes) == 1
    rows = platform_db.get_node_snapshot("lxp")
    assert len(rows) == 1
    assert rows[0]["node"] == "n1" and rows[0]["gpus"] == 4 and rows[0]["scheduler"] == "flux"

    # Snapshot replaces (latest wins).
    assert platform_db.record_node_snapshot("lxp", "flux", []) == 0
    assert platform_db.get_node_snapshot("lxp") == []
