"""ADR 0017 clause 1 — the Redis online store in front of the durable table, degrading safely."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


class FakeRedis:
    """Faithful to the subset used: get / set(ex=) / pipeline().set().execute()."""

    def __init__(self, *, fail: bool = False) -> None:
        self.data: dict[str, str] = {}
        self.expiry: dict[str, int] = {}
        self.fail = fail

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        if ex is not None:
            self.expiry[key] = ex
        return True

    def pipeline(self):
        parent = self

        class _Pipe:
            def __init__(self):
                self.ops = []

            def set(self, key, value, ex=None):
                self.ops.append((key, value, ex))

            def execute(self):
                if parent.fail:
                    raise ConnectionError("redis down")
                for k, v, ex in self.ops:
                    parent.set(k, v, ex=ex)
                return [True] * len(self.ops)

        return _Pipe()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in (
        "EXAMLOPS_FEATURE_ONLINE_STORE",
        "EXAMLOPS_FEATURE_REDIS_URL",
        "EXAMLOPS_REDIS_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    from examlops.feature_store.online import reset_online_store
    from examlops.platform_db import init_db

    init_db()
    reset_online_store()
    yield
    reset_online_store()


def _seed(fs, value=1.0):
    fs.apply_view(fs.FeatureView("jobs", "job", ["x"], ttl_seconds=120))
    fs.ingest("jobs", "j1", "2026-09-01 10:00:00", {"x": value})


def _use_redis(monkeypatch, client):
    import examlops.feature_store.online as online

    fake_mod = types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=lambda url, **kw: client))
    monkeypatch.setitem(sys.modules, "redis", fake_mod)
    monkeypatch.setenv("EXAMLOPS_FEATURE_ONLINE_STORE", "redis")
    monkeypatch.setenv("EXAMLOPS_FEATURE_REDIS_URL", "redis://fake:6379/0")
    online.reset_online_store()
    return online


def test_default_store_is_the_durable_table():
    from examlops import feature_store as fs
    from examlops.feature_store.online import select_online_store

    _seed(fs)
    fs.materialize("jobs")
    assert select_online_store().backend == "db"
    assert fs.get_online_features("jobs", ["j1"]) == [{"x": 1.0}]


def test_materialize_mirrors_to_redis_with_the_view_ttl(monkeypatch):
    from examlops import feature_store as fs

    client = FakeRedis()
    _use_redis(monkeypatch, client)
    _seed(fs)
    out = fs.materialize_with_index("jobs")
    assert out["online"].backend == "redis" and out["online"].written == 1
    assert out["online"].error is None
    assert client.expiry == {"examlops:features:jobs:j1": 120}
    # The serving read now comes from redis: change the durable row and redis still answers.
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute("UPDATE online_features SET values_json='{\"x\": 99.0}'")
    assert fs.get_online_features("jobs", ["j1"]) == [{"x": 1.0}]


def test_a_redis_miss_or_outage_falls_back_to_the_durable_table(monkeypatch):
    from examlops import feature_store as fs

    client = FakeRedis()
    _use_redis(monkeypatch, client)
    _seed(fs)
    fs.materialize("jobs")
    client.data.clear()  # miss
    assert fs.get_online_features("jobs", ["j1"]) == [{"x": 1.0}]
    client.fail = True  # outage
    assert fs.get_online_features("jobs", ["j1"]) == [{"x": 1.0}]


def test_a_failed_mirror_is_reported_and_keeps_the_durable_materialization(monkeypatch):
    from examlops import feature_store as fs
    from examlops.platform_db import get_online_feature

    _use_redis(monkeypatch, FakeRedis(fail=True))
    _seed(fs)
    out = fs.materialize_with_index("jobs")
    assert out["rows"] == 1
    assert "ConnectionError" in out["online"].error
    assert get_online_feature("jobs", "j1") == {"x": 1.0}


def test_redis_without_a_url_degrades_and_says_why(monkeypatch):
    from examlops.feature_store.online import select_online_store, selection_note

    monkeypatch.setenv("EXAMLOPS_FEATURE_ONLINE_STORE", "redis")
    assert select_online_store().backend == "db"
    assert "unset" in selection_note()


def test_redis_without_the_package_degrades_and_says_why(monkeypatch):
    from examlops.feature_store.online import select_online_store, selection_note

    monkeypatch.setitem(sys.modules, "redis", None)  # import raises ImportError
    monkeypatch.setenv("EXAMLOPS_FEATURE_ONLINE_STORE", "redis")
    monkeypatch.setenv("EXAMLOPS_REDIS_URL", "redis://x:6379/0")
    assert select_online_store().backend == "db"
    assert "features-online" in selection_note()


def test_an_unknown_backend_name_degrades(monkeypatch):
    from examlops.feature_store.online import select_online_store, selection_note

    monkeypatch.setenv("EXAMLOPS_FEATURE_ONLINE_STORE", "memcached")
    assert select_online_store().backend == "db"
    assert "unknown" in selection_note()


class WriteRejectingRedis(FakeRedis):
    """Redis at ``maxmemory`` with ``noeviction``: SET is refused, GET and DEL still work."""

    def __init__(self) -> None:
        super().__init__()
        self.reject_writes = False

    def delete(self, *keys):
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    def pipeline(self):
        parent = self

        class _Pipe:
            def __init__(self):
                self.ops = []

            def set(self, key, value, ex=None):
                self.ops.append(("set", key, value, ex))

            def delete(self, *keys):
                self.ops.append(("del", keys, None, None))

            def execute(self):
                if parent.reject_writes and any(op[0] == "set" for op in self.ops):
                    raise RuntimeError("OOM command not allowed when used memory > 'maxmemory'")
                for kind, key, value, ex in self.ops:
                    if kind == "set":
                        parent.set(key, value, ex=ex)
                    else:
                        parent.delete(*key)
                return [True] * len(self.ops)

        return _Pipe()


def test_a_rejected_mirror_never_leaves_the_old_value_serving(monkeypatch):
    """The durable table moved on; Redis refused the update. A read that still hits the old Redis
    key would serve the superseded value: skew the Redis tier must never cause."""
    from examlops import feature_store as fs

    client = WriteRejectingRedis()
    _use_redis(monkeypatch, client)
    _seed(fs, value=1.0)
    fs.materialize("jobs")
    assert fs.get_online_features("jobs", ["j1"]) == [{"x": 1.0}]

    fs.ingest("jobs", "j1", "2026-09-02 10:00:00", {"x": 2.0})
    client.reject_writes = True
    out = fs.materialize_with_index("jobs")
    assert out["online"].error and "OOM" in out["online"].error
    assert out["online"].invalidated == 1
    assert fs.get_online_features("jobs", ["j1"]) == [{"x": 2.0}]


def _small_pages(monkeypatch, size=1):
    import functools

    from examlops.data import data_assets

    real = data_assets.iter_online_features
    monkeypatch.setattr(
        data_assets, "iter_online_features", functools.partial(real, page_size=size)
    )


def test_online_rows_are_paged_by_entity_not_read_whole():
    from examlops import feature_store as fs
    from examlops.data.data_assets import iter_online_features

    fs.apply_view(fs.FeatureView("jobs", "job", ["x"]))
    for i in range(5):
        fs.ingest("jobs", f"j{i}", "2026-09-01 10:00:00", {"x": float(i)})
    fs.materialize("jobs")
    pages = list(iter_online_features("jobs", page_size=2))
    assert [len(p) for p in pages] == [2, 2, 1]
    assert [r["entity_id"] for p in pages for r in p] == [f"j{i}" for i in range(5)]


def test_a_scheduled_mirror_never_loads_the_whole_view(monkeypatch):
    """The materializer mirrors every online row each cycle; one unbounded read of a view of
    384-dim vectors holds gigabytes in the control plane for one tick."""
    from examlops import feature_store as fs
    from examlops.data import data_assets

    client = WriteRejectingRedis()
    _use_redis(monkeypatch, client)
    fs.apply_view(fs.FeatureView("jobs", "job", ["x"], ttl_seconds=120))
    for i in range(3):
        fs.ingest("jobs", f"j{i}", "2026-09-01 10:00:00", {"x": float(i)})

    def _whole(view):
        raise AssertionError("list_online_features read the whole view")

    monkeypatch.setattr(data_assets, "list_online_features", _whole)
    _small_pages(monkeypatch)
    out = fs.materialize_with_index("jobs")
    assert out["online"].error is None and out["online"].written == 3


def test_pages_after_a_failed_mirror_page_are_invalidated_too(monkeypatch):
    """Page 1 mirrors, page 2 is refused. Page 3 was never rewritten either: left in place its
    old key would keep serving the value the durable table already replaced."""
    from examlops import feature_store as fs

    client = WriteRejectingRedis()
    _use_redis(monkeypatch, client)
    fs.apply_view(fs.FeatureView("jobs", "job", ["x"], ttl_seconds=120))
    for i in range(3):
        fs.ingest("jobs", f"j{i}", "2026-09-01 10:00:00", {"x": 1.0})
    fs.materialize("jobs")
    assert fs.get_online_features("jobs", ["j2"]) == [{"x": 1.0}]

    for i in range(3):
        fs.ingest("jobs", f"j{i}", "2026-09-02 10:00:00", {"x": 2.0})
    _small_pages(monkeypatch)
    calls = {"n": 0}
    real_pipeline = client.pipeline

    def pipeline():
        pipe = real_pipeline()
        real_execute = pipe.execute

        def execute():
            if any(op[0] == "set" for op in pipe.ops):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise RuntimeError("OOM command not allowed")
            return real_execute()

        pipe.execute = execute
        return pipe

    client.pipeline = pipeline
    out = fs.materialize_with_index("jobs")
    assert out["online"].written == 1 and "OOM" in out["online"].error
    assert out["online"].invalidated == 2
    assert fs.get_online_features("jobs", ["j0", "j1", "j2"]) == [{"x": 2.0}] * 3
