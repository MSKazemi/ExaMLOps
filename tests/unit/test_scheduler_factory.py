"""Tests for scheduler-backend selection, including EXAMLOPS_SLURM_MODE back-compat."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ADAPTER_DIR = Path(__file__).resolve().parents[2] / "platform" / "infra" / "slurm-adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

import adapter as adapter_mod  # noqa: E402
from adapter import RealSlurmAdapter, get_scheduler_adapter  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "EXAMLOPS_HPC_SCHEDULER",
        "EXAMLOPS_SLURM_MODE",
        "EXAMLOPS_HPC_SSH_HOST",
        "EXAMLOPS_HPC_TRANSPORT",
    ):
        monkeypatch.delenv(var, raising=False)
    # Force local transport so the SSH branch never tries to connect.
    monkeypatch.setenv("EXAMLOPS_HPC_TRANSPORT", "local")


def _name(adapter) -> str:
    return type(adapter).__name__


def test_default_is_mock():
    assert _name(get_scheduler_adapter()) == "MockSlurmAdapter"


def test_legacy_slurm_mode_selects_slurm(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "slurm")
    assert _name(get_scheduler_adapter()) == "RealSlurmAdapter"


def test_legacy_slurm_mode_mock(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "mock")
    assert _name(get_scheduler_adapter()) == "MockSlurmAdapter"


def test_hpc_scheduler_flux(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "flux")
    assert _name(get_scheduler_adapter()) == "FluxAdapter"


def test_hpc_scheduler_overrides_legacy(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "slurm")
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "flux")
    assert _name(get_scheduler_adapter()) == "FluxAdapter"


def test_unknown_scheduler_raises(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "condor")
    with pytest.raises(adapter_mod.SchedulerAdapterError):
        get_scheduler_adapter()


def test_injected_executor_is_used(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_HPC_SCHEDULER", "slurm")
    sentinel = object()
    a = get_scheduler_adapter(executor=sentinel)
    assert isinstance(a, RealSlurmAdapter)
    assert a.executor is sentinel


def test_slurm_alias_backcompat(monkeypatch):
    from adapter import get_slurm_adapter  # noqa: PLC0415

    monkeypatch.setenv("EXAMLOPS_SLURM_MODE", "slurm")
    assert _name(get_slurm_adapter()) == "RealSlurmAdapter"
