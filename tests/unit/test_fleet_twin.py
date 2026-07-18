"""Fleet Digital Twin what-if engine (enterprise-readiness Phase 5, item 5.1).

Proves the simulation engine is a faithful, pure projection: jobs place onto real clusters using the
production scoring fn, capacity depletes, overflow queues, and cost/carbon/GPU-hours roll up — all
without touching live state. Scenario mutations (added capacity, carbon/cost shifts) change the
projection the way an operator would expect in a what-if.
"""

from __future__ import annotations

from examlops.fleet_twin import JobSpec, Scenario, project
from examlops.hpc_placement_providers import resolve_placement_score_fn


def _cluster(name, gpus, *, carbon=None, cost=None):
    # Ample nodes so GPU count is the binding constraint in these GPU-focused scenarios.
    caps = {"total_gpus": gpus, "total_nodes": 100, "idle_gpus": gpus, "idle_nodes": 100}
    if carbon is not None:
        caps["carbon_intensity"] = carbon
    if cost is not None:
        caps["cost_per_gpu_hour"] = cost
    return {"name": name, "scheduler": "flux", "capabilities": caps, "nodes": []}


def test_jobs_place_and_deplete_capacity():
    clusters = [_cluster("a", 4)]
    scenario = Scenario(jobs=[JobSpec(gpus=2, nodes=1, count=2)])
    result = project(clusters, scenario)
    assert result["placed"] == 2 and result["queued"] == 0
    assert result["clusters"][0]["idle_gpus"] == 0  # 4 - 2 - 2


def test_overflow_goes_to_queue():
    clusters = [_cluster("a", 2)]
    scenario = Scenario(jobs=[JobSpec(gpus=2, count=3)])  # only one fits
    result = project(clusters, scenario)
    assert result["placed"] == 1 and result["queued"] == 2
    assert result["queue_depth"] == 2


def test_added_capacity_absorbs_more_jobs():
    clusters = [_cluster("a", 2)]
    base = project(clusters, Scenario(jobs=[JobSpec(gpus=2, count=3)]))
    scaled = project(clusters, Scenario(add_gpus={"a": 4}, jobs=[JobSpec(gpus=2, count=3)]))
    assert base["queued"] == 2
    assert scaled["queued"] == 0  # +4 GPUs → all three 2-GPU jobs fit


def test_cost_and_carbon_projection():
    clusters = [_cluster("a", 4, carbon=400, cost=2.0)]
    scenario = Scenario(jobs=[JobSpec(gpus=2, duration_h=3.0, count=1)])
    result = project(clusters, scenario)
    # 2 GPUs × 3h = 6 gpu-hours; cost = 6 × $2 = $12.
    assert result["projected_gpu_hours"] == 6.0
    assert result["projected_cost_usd"] == 12.0
    assert result["projected_carbon_kg"] > 0  # 6 gpu-h × 400W × PUE × 400 gCO2e/kWh


def test_carbon_override_steers_placement():
    clusters = [_cluster("dirty", 8, carbon=50), _cluster("clean", 8, carbon=50)]
    # Make 'dirty' dirty via the scenario; carbon-aware placement should avoid it.
    scenario = Scenario(carbon_overrides={"dirty": 800}, jobs=[JobSpec(gpus=2, count=1)])
    result = project(clusters, scenario, score_fn=resolve_placement_score_fn("carbon-aware"))
    assert result["placements"][0]["cluster"] == "clean"


def test_empty_scenario_is_zero_projection():
    clusters = [_cluster("a", 4)]
    result = project(clusters, Scenario())
    assert result["placed"] == 0 and result["projected_gpu_hours"] == 0.0
