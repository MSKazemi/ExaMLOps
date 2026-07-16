"""E4 — inference gateway & KV-cache-aware routing (ADR 0039)."""

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


def _replicas(n):
    from examlops.inference_gateway import Replica

    return [Replica(f"r{i}") for i in range(n)]


def test_prefix_key_stable_and_distinct():
    from examlops.inference_gateway import prefix_key

    a = prefix_key(system_prompt="sys", session_id="s1")
    b = prefix_key(system_prompt="sys", session_id="s1")
    c = prefix_key(system_prompt="sys", session_id="s2")
    assert a == b
    assert a != c


def test_gwt1_cache_aware_beats_round_robin_hit_rate():
    from examlops.inference_gateway import (
        MODE_CACHE_AWARE,
        MODE_ROUND_ROBIN,
        InferenceGateway,
        measure_hit_rate,
    )

    keys = ["shared"] * 100
    ca = measure_hit_rate(InferenceGateway(_replicas(4), mode=MODE_CACHE_AWARE), keys)
    rr = measure_hit_rate(InferenceGateway(_replicas(4), mode=MODE_ROUND_ROBIN), keys)
    assert ca > rr
    assert ca > 0.95  # after the first miss, everything is an affinity hit


def test_gwt2_no_affinity_routes_by_load():
    from examlops.inference_gateway import MODE_CACHE_AWARE, InferenceGateway, Replica

    r0 = Replica("r0", queue_depth=10)
    r1 = Replica("r1", queue_depth=1)  # least loaded
    gw = InferenceGateway([r0, r1], mode=MODE_CACHE_AWARE)
    d = gw.route("brand-new-prefix")
    assert d.decision == "load_aware"
    assert d.replica.id == "r1"


def test_gwt2_affinity_wins_over_load():
    from examlops.inference_gateway import MODE_CACHE_AWARE, InferenceGateway, Replica

    r0 = Replica("r0", queue_depth=10, prefixes={"k"})  # holds prefix but loaded
    r1 = Replica("r1", queue_depth=1)  # least loaded, no prefix
    gw = InferenceGateway([r0, r1], mode=MODE_CACHE_AWARE)
    d = gw.route("k")
    assert d.decision == "affinity"
    assert d.replica.id == "r0"
    assert d.hit is True


def test_gwt3_round_robin_default_serves_all():
    from examlops.inference_gateway import InferenceGateway

    gw = InferenceGateway(_replicas(3))  # default round-robin
    seen = {gw.route("k").replica.id for _ in range(3)}
    assert seen == {"r0", "r1", "r2"}


def test_gwt5_slo_breaching_replica_avoided():
    from examlops.inference_gateway import MODE_CACHE_AWARE, InferenceGateway, Replica

    fast = Replica("fast", latency_ms=100, queue_depth=5)
    slow = Replica("slow", latency_ms=2000, queue_depth=0)  # lowest load but breaches SLO
    gw = InferenceGateway([fast, slow], mode=MODE_CACHE_AWARE, slo_latency_ms=500)
    d = gw.route("new")
    assert d.replica.id == "fast"  # slow avoided despite lower load


def test_slo_all_breaching_falls_back():
    from examlops.inference_gateway import InferenceGateway, Replica

    a = Replica("a", latency_ms=2000)
    b = Replica("b", latency_ms=3000)
    gw = InferenceGateway([a, b], slo_latency_ms=500)
    # Never route to nothing — falls back to eligible even if all breach.
    assert gw.route("k").replica.id in {"a", "b"}


def test_unhealthy_replica_excluded():
    from examlops.inference_gateway import InferenceGateway, Replica

    gw = InferenceGateway([Replica("dead", healthy=False), Replica("live")])
    assert gw.route("k").replica.id == "live"


def test_gwt4_disaggregation_identical_output():
    from examlops.inference_gateway import Replica, disaggregated_route

    result = disaggregated_route("k", [Replica("p0"), Replica("p1")], [Replica("d0")])
    assert result["identical_output"] is True
    assert result["prefill_replica"] in {"p0", "p1"}
    assert result["decode_replica"] == "d0"


def test_persist_records_routing_events():
    from examlops.inference_gateway import MODE_CACHE_AWARE, InferenceGateway
    from examlops.platform_db import routing_stats

    gw = InferenceGateway(_replicas(2), model="JPCP", mode=MODE_CACHE_AWARE, persist=True)
    gw.route("k")
    gw.route("k")  # affinity hit
    st = routing_stats("JPCP")
    assert st["total"] == 2
    assert st["hits"] == 1  # second route hits


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r = runner.invoke(app, ["serve", "routing", "set", "JPCP", "--mode", "cache_aware"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "serve",
            "routing",
            "simulate",
            "JPCP",
            "--replicas",
            "4",
            "--shared-prefix-requests",
            "50",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "hit rate" in r.output.lower()
    r = runner.invoke(app, ["serve", "routing", "stats", "JPCP"])
    assert r.exit_code == 0, r.output
