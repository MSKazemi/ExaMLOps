"""Unit tests for per-cluster capacity/cost (Phase 35d, pure/offline)."""

from __future__ import annotations

from examlops.hpc_capacity import capacity_report, gpu_hours_by_scheduler


def test_gpu_hours_by_scheduler():
    jobs = [
        {"scheduler": "flux", "gpus": 4, "run_seconds": 3600},  # 4 gpu-h
        {"scheduler": "flux", "gpus": 2, "run_seconds": 1800},  # 1 gpu-h
        {"scheduler": "slurm", "gpus": 1, "run_seconds": 7200},  # 2 gpu-h
        {"scheduler": "flux", "gpus": None, "run_seconds": 100},  # skipped
    ]
    gh = gpu_hours_by_scheduler(jobs)
    assert gh["flux"] == 5.0
    assert gh["slurm"] == 2.0


def test_capacity_report_joins_inventory_and_cost():
    clusters = [
        {
            "name": "remote",
            "scheduler": "flux",
            "capabilities": {"total_gpus": 8},
            "nodes": [
                {"state": "idle", "gpus": 4},
                {"state": "allocated", "gpus": 4},
            ],
        }
    ]
    jobs = [{"scheduler": "flux", "gpus": 4, "run_seconds": 3600}]
    rows = capacity_report(clusters, jobs, gpu_cost_per_hour=2.0)
    r = rows[0]
    assert r["total_gpus"] == 8
    assert r["idle_gpus"] == 4
    assert r["utilization_pct"] == 50.0  # 4 of 8 allocated
    assert r["gpu_hours_used"] == 4.0
    assert r["cost_usd"] == 8.0  # 4 gpu-h × $2


def test_capacity_falls_back_to_capabilities_without_snapshot():
    clusters = [
        {"name": "c", "scheduler": "slurm", "capabilities": {"total_gpus": 16}, "nodes": []}
    ]
    rows = capacity_report(clusters, [], gpu_cost_per_hour=1.0)
    assert rows[0]["total_gpus"] == 16
    assert rows[0]["gpu_hours_used"] == 0.0
