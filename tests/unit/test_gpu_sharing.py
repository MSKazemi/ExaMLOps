"""E3 — GPU sharing & fractional allocation (ADR 0030)."""

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


def test_whole_gpu_when_fraction_one():
    from examlops.gpu_sharing import ClusterGpuCaps, FractionalAsk, select_mechanism

    choice = select_mechanism(FractionalAsk("m", fraction=1.0), ClusterGpuCaps())
    assert choice.mechanism == "whole"
    assert choice.isolation == "exclusive"
    assert choice.wasted_fraction == 0.0


def test_mig_selected_when_capable():
    from examlops.gpu_sharing import ClusterGpuCaps, FractionalAsk, select_mechanism

    caps = ClusterGpuCaps(supports_mig=True, mig_profiles=["2g.10gb"])
    choice = select_mechanism(FractionalAsk("m", mig_profile="2g.10gb"), caps)
    assert choice.mechanism == "mig"
    assert choice.isolation == "hardware"


def test_timeslice_soft_isolation():
    from examlops.gpu_sharing import ClusterGpuCaps, FractionalAsk, select_mechanism

    caps = ClusterGpuCaps(supports_timeslice=True)
    choice = select_mechanism(FractionalAsk("m", fraction=0.5), caps)
    assert choice.mechanism == "timeslice"
    assert choice.isolation == "soft"


def test_honest_fallback_rounds_up_and_surfaces_waste():
    """No fractional support => whole GPU, waste surfaced (not silently pretended)."""
    from examlops.gpu_sharing import ClusterGpuCaps, FractionalAsk, select_mechanism

    choice = select_mechanism(FractionalAsk("m", fraction=0.25), ClusterGpuCaps())
    assert choice.mechanism == "whole"
    assert choice.allocated_fraction == 1.0
    assert choice.wasted_fraction == pytest.approx(0.75)
    assert "wasted" in choice.note


def test_mig_snaps_up_to_smallest_fitting_profile():
    from examlops.gpu_sharing import ClusterGpuCaps, FractionalAsk, select_mechanism

    caps = ClusterGpuCaps(supports_mig=True, mig_profiles=["1g.5gb", "2g.10gb", "7g.40gb"])
    # Ask 0.2 (< 1/7≈0.143? no, > 1/7) => needs 2g (2/7≈0.286).
    choice = select_mechanism(FractionalAsk("m", fraction=0.2), caps)
    assert choice.mechanism == "mig"
    assert choice.allocated_fraction == pytest.approx(2 / 7)
    assert choice.wasted_fraction > 0


def test_bin_pack_fits_on_fewer_gpus():
    from examlops.gpu_sharing import ClusterGpuCaps, FractionalAsk, bin_pack

    caps = ClusterGpuCaps(supports_timeslice=True)
    asks = [FractionalAsk("a", 0.5), FractionalAsk("b", 0.3), FractionalAsk("c", 0.4)]
    result = bin_pack(asks, 2, caps)
    assert not result.unplaced
    assert result.gpus_used <= 2
    assert len(result.placements) == 3


def test_bin_pack_reports_unplaced():
    from examlops.gpu_sharing import ClusterGpuCaps, FractionalAsk, bin_pack

    caps = ClusterGpuCaps(supports_timeslice=True)
    asks = [FractionalAsk("a", 0.8), FractionalAsk("b", 0.8)]
    result = bin_pack(asks, 1, caps)  # only 1 GPU, both need 0.8
    assert "b" in result.unplaced


def test_fractional_accounting():
    from examlops.gpu_sharing import fractional_gpu_hours

    # 0.25 GPU for 2 hours = 0.5 GPU-hours.
    assert fractional_gpu_hours(0.25, 7200) == pytest.approx(0.5)


def test_record_and_list_allocations():
    from examlops.gpu_sharing import (
        ClusterGpuCaps,
        FractionalAsk,
        list_allocations,
        record_allocation,
        select_mechanism,
    )

    choice = select_mechanism(
        FractionalAsk("JPCP", fraction=0.5), ClusterGpuCaps(supports_timeslice=True)
    )
    record_allocation("JPCP", choice, tenant="acme")
    rows = list_allocations(tenant="acme")
    assert len(rows) == 1
    assert rows[0]["model"] == "JPCP"
    assert rows[0]["mechanism"] == "timeslice"


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r1 = runner.invoke(app, ["hpc", "gpu-share", "plan", "JPCP", "--fraction", "0.25"])
    assert r1.exit_code == 0, r1.output
    assert "wasted" in r1.output.lower() or "whole" in r1.output.lower()
    r2 = runner.invoke(
        app,
        [
            "hpc",
            "gpu-share",
            "pack",
            "--ask",
            "a:0.5",
            "--ask",
            "b:0.4",
            "--gpus",
            "1",
            "--timeslice",
        ],
    )
    assert r2.exit_code == 0, r2.output
