"""A serving replica serves the snapshot it is given, by exact version, and survives an outage.

Plan P4.2 / ADR 0127. The server is hand-built (``object.__new__``), the way the other Ray Serve
tests build it, so no Ray actor starts and no MLflow is contacted: model loads are recorded.
"""

from __future__ import annotations

import json
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from examlops.serving_snapshot import digest_of  # noqa: E402
from serving.ray_serving import app as rs_app  # noqa: E402
from serving.ray_serving import snapshot as rs_snapshot  # noqa: E402


def _snapshot(generation: int, aliases: dict[str, dict[str, str]], shadow=None) -> dict:
    """``aliases``: model -> {alias: version}."""
    models = {
        name.lower(): {
            "name": name,
            "aliases": {
                a: {"version": v, "run_id": f"r-{name}-{v}", "framework": "sklearn"}
                for a, v in per_model.items()
            },
        }
        for name, per_model in aliases.items()
    }
    content = {"models": models, "traffic": {}, "shadow": shadow or {}}
    return {"schema": 1, "generation": generation, "digest": digest_of(content), **content}


def _server(hot: dict | None = None, fail_versions: set[str] | None = None):
    cls = rs_app.MultiModelServer.func_or_class
    server = object.__new__(cls)
    server._cache_lock = threading.RLock()
    server._hot = dict(hot or {})
    server._version_cache = OrderedDict()
    server._version_cache_size = 8
    server._preload_aliases = ["Production", "Canary"]
    server._replica_id = "test"
    server._snapshot_reader = None
    server._snapshot_generation = None
    server._snapshot_shadow = None
    server._shadow_cache = {}
    for attr in ("_models_gauge", "_snapshot_gauge", "_reload_counter"):
        setattr(server, attr, MagicMock())
    server.loads = []

    def load(name, alias, mv):
        server.loads.append((name, alias, str(mv.version), mv.tags.get("framework")))
        if fail_versions and str(mv.version) in fail_versions:
            raise OSError("artifact store unreachable")
        return f"model:{name}:{mv.version}"

    server._load_by_flavour = load
    return server


@pytest.fixture(autouse=True)
def _aliases(monkeypatch):
    monkeypatch.setattr(rs_app, "_get_serve_aliases_for", lambda name: ["Production", "Canary"])


def _entry(version: str) -> dict:
    return {"model": f"old:{version}", "version": version, "run_id": None}


def test_models_are_loaded_by_version_never_by_alias():
    """The alias can move again between the snapshot and the download; the version cannot."""
    server = _server()
    server._apply_snapshot(_snapshot(1, {"jpcp": {"Production": "7"}}))

    assert server.loads == [("jpcp", None, "7", "sklearn")]
    assert server._hot[("jpcp", "Production")]["version"] == "7"
    assert server._snapshot_generation == 1


def test_only_what_moved_is_reloaded():
    server = _server({("jpcp", "Production"): _entry("7"), ("mack", "Production"): _entry("3")})

    server._apply_snapshot(_snapshot(2, {"jpcp": {"Production": "7"}, "mack": {"Production": "4"}}))

    assert server.loads == [("mack", None, "4", "sklearn")]
    assert server._hot[("jpcp", "Production")]["model"] == "old:7"  # untouched, same object


def test_an_alias_the_snapshot_no_longer_has_is_unloaded():
    server = _server({("jpcp", "Production"): _entry("7"), ("jpcp", "Canary"): _entry("8")})

    server._apply_snapshot(_snapshot(3, {"jpcp": {"Production": "7"}}))

    assert ("jpcp", "Canary") not in server._hot


def test_a_failed_load_keeps_the_last_known_good_version():
    server = _server({("jpcp", "Production"): _entry("7")}, fail_versions={"8"})

    server._apply_snapshot(_snapshot(4, {"jpcp": {"Production": "8"}}))

    assert server._hot[("jpcp", "Production")]["version"] == "7"


def test_a_moved_model_drops_its_pinned_version_cache():
    server = _server({("jpcp", "Production"): _entry("7")})
    server._version_cache[("jpcp", "5")] = _entry("5")
    server._version_cache[("mack", "1")] = _entry("1")

    server._apply_snapshot(_snapshot(5, {"jpcp": {"Production": "8"}, "mack": {}}))

    assert list(server._version_cache) == [("mack", "1")]


def test_shadow_targets_come_from_the_snapshot_without_a_database_read(monkeypatch):
    server = _server()
    server._apply_snapshot(
        _snapshot(
            6, {"jpcp": {"Production": "7"}}, shadow={"jpcp": {"model": "JPCP", "alias": "Staging"}}
        )
    )

    import examlops.platform_db as pdb

    monkeypatch.setattr(pdb, "get_db", MagicMock(side_effect=AssertionError("db read")))
    assert server._shadow_target("JPCP") == "Staging"
    assert server._shadow_target("mack") is None


def test_the_same_generation_is_not_applied_twice():
    server = _server()
    reader = MagicMock()
    reader.newest.return_value = _snapshot(7, {"jpcp": {"Production": "7"}})
    server._snapshot_reader = reader

    assert server._apply_newest_snapshot() is True
    assert server._apply_newest_snapshot() is True
    assert len(server.loads) == 1


def test_without_a_snapshot_the_replica_falls_back_to_mlflow():
    server = _server()
    reader = MagicMock()
    reader.newest.return_value = None
    server._snapshot_reader = reader
    assert server._apply_newest_snapshot() is False


# ─── the reader ──────────────────────────────────────────────────────────────


@pytest.fixture()
def reader(tmp_path, monkeypatch):
    r = rs_snapshot.SnapshotReader(cache_path=tmp_path / "snap.json")
    return r


def test_the_highest_generation_wins_across_sources(reader, monkeypatch):
    monkeypatch.setattr(reader, "_from_kv", lambda: _snapshot(9, {"a": {"Production": "2"}}))
    monkeypatch.setattr(reader, "_from_db", lambda: _snapshot(10, {"a": {"Production": "3"}}))

    assert reader.newest()["generation"] == 10
    assert reader.source == "db"


def test_a_snapshot_whose_digest_does_not_match_is_refused(reader, monkeypatch):
    forged = _snapshot(11, {"a": {"Production": "2"}})
    forged["models"]["a"]["aliases"]["Production"]["version"] = "666"
    monkeypatch.setattr(reader, "_from_kv", lambda: None)
    monkeypatch.setattr(reader, "_from_db", lambda: forged)

    assert reader.newest() is None


def test_with_every_source_down_the_last_known_good_file_is_served(reader, monkeypatch):
    """Static stability: a replica restarting during an outage serves what it served."""
    monkeypatch.setattr(reader, "_from_kv", lambda: None)
    monkeypatch.setattr(reader, "_from_db", lambda: _snapshot(12, {"a": {"Production": "4"}}))
    reader.newest()  # persisted

    def down():
        raise ConnectionError("database unreachable")

    restarted = rs_snapshot.SnapshotReader(cache_path=reader.cache_path)
    monkeypatch.setattr(restarted, "_from_kv", down)
    monkeypatch.setattr(restarted, "_from_db", down)

    assert restarted.newest()["generation"] == 12
    assert restarted.source == "cache"


def test_an_old_file_does_not_stand_in_when_the_platform_has_no_snapshot(reader, monkeypatch):
    reader.cache_path.write_text(json.dumps(_snapshot(13, {"a": {"Production": "1"}})))
    monkeypatch.setattr(reader, "_from_kv", lambda: None)
    monkeypatch.setattr(reader, "_from_db", lambda: None)  # reachable, and says: none published

    assert reader.newest() is None


def test_the_database_path_reads_what_the_control_plane_published(reader, monkeypatch):
    from examlops import serving_snapshot

    body = _snapshot(0, {"jpcp": {"Production": "7"}})
    generation, _ = serving_snapshot.publish(body)
    monkeypatch.setattr(reader, "_from_kv", lambda: None)

    got = reader.newest()

    assert got["generation"] == generation
    assert got["models"]["jpcp"]["aliases"]["Production"]["version"] == "7"


# ─── the router takes traffic splits from the snapshot too ───────────────────


@pytest.fixture()
def router(monkeypatch):
    from serving.inference_pipeline import app as ip_app

    monkeypatch.setattr(ip_app, "_snapshot_state", {"reader": None, "snapshot": None, "next": 0.0})
    monkeypatch.setattr(ip_app, "_traffic_rules", {})
    return ip_app


def _with_traffic(generation: int, traffic: dict) -> dict:
    s = _snapshot(generation, {"jpcp": {"Production": "7"}})
    s["traffic"] = traffic
    s["digest"] = digest_of({k: s[k] for k in ("models", "traffic", "shadow")})
    return s


def test_the_router_uses_the_snapshots_split_not_its_own_table_read(router):
    from examlops import serving_snapshot
    from examlops.data import serving

    serving.set_traffic_rules("JPCP", {"Production": 50, "Canary": 50}, updated_by="a")
    serving_snapshot.publish(
        _with_traffic(0, {"jpcp": {"model": "JPCP", "rules": {"Production": 90, "Canary": 10}}})
    )

    assert router._get_split("JPCP") == {"Production": 90, "Canary": 10}


def test_a_model_without_a_split_in_the_snapshot_has_none(router):
    from examlops import serving_snapshot
    from examlops.data import serving

    serving.set_traffic_rules("MACK", {"Production": 50, "Canary": 50}, updated_by="a")
    serving_snapshot.publish(_with_traffic(0, {}))

    assert router._get_split("MACK") is None


def test_without_a_snapshot_the_router_reads_the_table_as_before(router):
    from examlops.data import serving

    serving.set_traffic_rules("JPCP", {"Production": 70, "Canary": 30}, updated_by="a")

    assert router._get_split("JPCP") == {"Production": 70, "Canary": 30}


def test_the_database_poll_reads_the_body_only_for_a_new_generation(reader, monkeypatch):
    from examlops import serving_snapshot

    serving_snapshot.publish(_snapshot(0, {"jpcp": {"Production": "7"}}))
    monkeypatch.setattr(reader, "_from_kv", lambda: None)
    reader.newest()
    bodies = []
    real = serving_snapshot.latest
    monkeypatch.setattr(serving_snapshot, "latest", lambda: bodies.append(1) or real())

    reader.newest()
    reader.newest()
    assert bodies == []  # same generation: only MAX(generation) was read

    serving_snapshot.publish(_snapshot(0, {"jpcp": {"Production": "8"}}))
    assert reader.newest()["models"]["jpcp"]["aliases"]["Production"]["version"] == "8"
    assert bodies == [1]
