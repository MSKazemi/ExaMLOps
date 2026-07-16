"""E8 — heterogeneous hardware & hybrid HPC↔cloud (ADR 0041).

GWT coverage: CPU placement, portability rejection of an incompatible engine, fractional
fallback on a non-fractioning vendor, residency-blocked cloud burst (+ audit), and device+region
accounting flowing into cost + carbon.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def _seed_pools():
    from examlops.platform_db import register_device_pool

    register_device_pool(
        "hpc-nvidia",
        target="hpc",
        accelerator="nvidia",
        count=8,
        region="eu",
        cost_per_hour=2.5,
        carbon_factor=300,
        supports_fractions=True,
    )
    register_device_pool(
        "hpc-amd",
        target="hpc",
        accelerator="amd",
        count=4,
        region="eu",
        cost_per_hour=1.8,
        carbon_factor=250,
    )
    register_device_pool(
        "hpc-cpu",
        target="hpc",
        accelerator="cpu",
        count=64,
        region="eu",
        cost_per_hour=0.1,
        carbon_factor=20,
    )
    register_device_pool(
        "cloud-nvidia",
        target="cloud",
        accelerator="nvidia",
        count=100,
        region="us",
        cost_per_hour=4.0,
        carbon_factor=500,
    )


def test_place_cpu(_seed=None):
    # GWT-1: --accelerator cpu runs on a CPU pool.
    _seed_pools()
    from examlops.hardware import Placement, Workload, place

    res = place(Workload("w", accelerator="cpu", engine="cpu", target="hpc"))
    assert isinstance(res, Placement)
    assert res.accelerator == "cpu" and res.pool == "hpc-cpu"


def test_portability_rejects_incompatible_engine():
    # GWT-2: a CUDA-only engine on an AMD target is rejected with a clear error.
    _seed_pools()
    from examlops.hardware import Rejection, Workload, place

    res = place(Workload("w", accelerator="amd", engine="sglang"))
    assert isinstance(res, Rejection)
    assert "cannot run on amd" in res.reason


def test_portable_helper():
    from examlops.hardware import portable

    assert portable("vllm", "amd") is True
    assert portable("sglang", "amd") is False
    assert portable("cpu", "cpu") is True
    assert portable("generic", "tpu") is True


def test_fraction_fallback_on_non_fractioning_vendor():
    # GWT-3: a fractional request on AMD (no MIG) allocates a whole device, flagged honestly.
    _seed_pools()
    from examlops.hardware import Placement, Workload, place

    res = place(Workload("w", accelerator="amd", engine="vllm", fraction=0.5))
    assert isinstance(res, Placement)
    assert res.fraction_honored is False
    assert "whole device" in res.note


def test_fraction_honored_on_nvidia():
    _seed_pools()
    from examlops.hardware import Workload, place

    res = place(Workload("w", accelerator="nvidia", engine="vllm", fraction=0.5, target="hpc"))
    assert res.fraction_honored is True


def test_honest_fallback_when_requested_unavailable():
    # Requested accelerator has no pool → fall back to a compatible one, flagged.
    _seed_pools()
    from examlops.hardware import Placement, Workload, place

    res = place(Workload("w", accelerator="tpu", engine="generic", target="hpc"))
    assert isinstance(res, Placement)
    assert res.fallback is True
    assert res.accelerator != "tpu"


def test_cheapest_eligible_chosen():
    # Two nvidia pools eligible (hpc + cloud, no target filter) → cheapest wins.
    _seed_pools()
    from examlops.hardware import Workload, place

    res = place(Workload("w", accelerator="nvidia", engine="vllm"))
    assert res.pool == "hpc-nvidia"  # 2.5/hr < 4.0/hr cloud


def test_no_compatible_pool_rejected():
    from examlops.hardware import Rejection, Workload, place

    res = place(Workload("w", accelerator="nvidia", engine="vllm"), pools=[])
    assert isinstance(res, Rejection)
    assert "no compatible device" in res.reason


def test_burst_blocked_by_residency_and_audited():
    # GWT-4: no-egress residency blocks a burst and records an audit + burst event.
    _seed_pools()
    from examlops import platform_db
    from examlops.hardware import Rejection, Workload, plan_burst

    res = plan_burst(
        Workload("w", accelerator="nvidia", engine="vllm", residency="no-egress", allow_burst=True)
    )
    assert isinstance(res, Rejection)
    events = platform_db.list_burst_events()
    assert events and events[0]["allowed"] == 0
    with platform_db.get_db() as conn:
        audits = conn.execute("SELECT 1 FROM audit_events WHERE action='hardware_burst'").fetchall()
    assert audits


def test_burst_requires_opt_in():
    _seed_pools()
    from examlops.hardware import Rejection, Workload, plan_burst

    res = plan_burst(Workload("w", accelerator="nvidia", engine="vllm", allow_burst=False))
    assert isinstance(res, Rejection)
    assert "opt-in" in res.reason


def test_burst_allowed_when_open():
    _seed_pools()
    from examlops.hardware import Placement, Workload, plan_burst

    res = plan_burst(
        Workload("w", accelerator="nvidia", engine="vllm", residency="open", allow_burst=True)
    )
    assert isinstance(res, Placement)
    assert res.target == "cloud"


def test_device_accounting():
    # GWT-5: device type + region flow into cost + carbon.
    _seed_pools()
    from examlops.hardware import Workload, device_accounting, place

    res = place(Workload("w", accelerator="amd", engine="vllm", target="hpc"))
    acct = device_accounting(res, hours=10)
    assert acct["device"] == "amd" and acct["region"] == "eu"
    assert acct["cost"] == pytest.approx(18.0)
    assert acct["carbon_g"] == pytest.approx(2500.0)


def test_invalid_accelerator_raises():
    from examlops.hardware import Workload, place

    with pytest.raises(ValueError):
        place(Workload("w", accelerator="quantum", engine="generic"))
