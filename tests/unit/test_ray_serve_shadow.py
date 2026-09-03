"""ADR 0024 clause 1 — the router mirrors a request, and the mirror cannot hurt production.

This clause is the reason the ADR exists and had no implementation: the scoreboard, the Welch
test and the CLI were all built, and `shadow_results` held only what a caller wrote by hand
because **nothing mirrored a request**.

The tests that matter here are not "does it record a row" — they are the isolation properties.
A shadow that can slow, break, or leak into the production response is worse than no shadow,
because it degrades the traffic it was added to observe.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from serving.ray_serving import app as rs_app  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.delenv("EXAMLOPS_POSTGRES_DSN", raising=False)
    from examlops.platform_db import init_db

    init_db()


def _server(max_inflight=16):
    cls = rs_app.MultiModelServer.func_or_class
    s = object.__new__(cls)
    s._cache_lock = threading.RLock()
    s._hot = {}
    s._version_cache = OrderedDict()
    s._version_cache_size = 8
    s._replica_id = "test"
    for attr in (
        "_req_counter",
        "_latency_hist",
        "_pred_value_hist",
        "_models_gauge",
        "_version_gauge",
        "_reload_counter",
        "_shadow_counter",
    ):
        setattr(s, attr, MagicMock())
    s._shadow_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shadow")
    s._shadow_max_inflight = max_inflight
    s._shadow_ttl = 30.0
    s._shadow_inflight = 0
    s._shadow_lock = threading.Lock()
    s._shadow_cache = {}
    return s


def _enable(model="JPCP", alias="Staging"):
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO shadow_config (model, shadow_alias, enabled) VALUES (?,?,1)",
            (model, alias),
        )


def _rows():
    from examlops.platform_db import get_db

    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM shadow_results ORDER BY id")]


def _settle(server, timeout=5.0):
    """Wait for in-flight shadow work — tests only; production never joins on this pool."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with server._shadow_lock:
            if server._shadow_inflight == 0:
                return
        time.sleep(0.01)
    raise AssertionError("shadow work did not finish")


# ── it mirrors ────────────────────────────────────────────────────────────────


def test_a_mirrored_request_records_a_comparison():
    s = _server()
    _enable()
    s._resolve = lambda m, a, v: {
        "model": MagicMock(predict=lambda x: [12.0]),
        "version": "2",
        "alias": "Staging",
        "run_id": "r",
    }
    s._mirror("JPCP", [[1.0]], 10.0)
    _settle(s)
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["production_pred"] == 10.0 and rows[0]["shadow_pred"] == 12.0
    assert rows[0]["diff_pct"] == pytest.approx(20.0)


def test_nothing_is_mirrored_when_shadow_is_not_enabled():
    """Off by default — a serving replica must not start doing double the inference because a
    table exists."""
    s = _server()
    s._resolve = lambda *a, **k: pytest.fail("resolved a shadow that was never enabled")
    s._mirror("JPCP", [[1.0]], 10.0)
    _settle(s)
    assert _rows() == []


def test_a_disabled_config_is_respected():
    from examlops.platform_db import get_db

    s = _server()
    _enable()
    with get_db() as conn:
        conn.execute("UPDATE shadow_config SET enabled=0 WHERE model='JPCP'")
    s._resolve = lambda *a, **k: pytest.fail("mirrored a disabled shadow")
    s._mirror("JPCP", [[1.0]], 10.0)
    _settle(s)
    assert _rows() == []


# ── it cannot hurt production — the properties clause 1 actually asks for ─────


def test_a_shadow_that_raises_does_not_propagate():
    """`_mirror` is called on the request thread. An exception escaping it would turn a healthy
    prediction into a 500 *because of the model being evaluated*."""
    s = _server()
    _enable()
    s._resolve = MagicMock(side_effect=RuntimeError("shadow model is broken"))
    s._mirror("JPCP", [[1.0]], 10.0)  # must not raise
    _settle(s)
    assert _rows() == []


def test_an_unreadable_config_looks_exactly_like_switched_off(monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", "/nonexistent/dir/p.db")
    s = _server()
    assert s._shadow_target("JPCP") is None


def test_mirroring_does_not_block_the_request_thread():
    """Asynchronous is the word clause 1 uses. If `_mirror` waited for the shadow, a shadow
    twice as slow as the champion would double every response time on the platform."""
    s = _server()
    _enable()
    started = threading.Event()
    release = threading.Event()

    def _slow(x):
        started.set()
        release.wait(5)
        return [1.0]

    s._resolve = lambda m, a, v: {
        "model": MagicMock(predict=_slow),
        "version": "2",
        "alias": "Staging",
        "run_id": "r",
    }
    t0 = time.time()
    s._mirror("JPCP", [[1.0]], 10.0)
    elapsed = time.time() - t0
    assert started.wait(5), "the shadow never ran"

    # The proof is structural, not a stopwatch: `_mirror` returned while the shadow is still
    # in flight and has not been released. A first attempt asserted `elapsed < 0.5` and failed
    # under the parallel suite on a saturated machine — a threshold tight enough to catch the
    # bug was also tight enough to catch a busy CPU. The shadow blocks for up to 5s, so a
    # `_mirror` that waited could not return in under 2s however loaded the box is.
    with s._shadow_lock:
        assert s._shadow_inflight == 1
    assert not release.is_set()
    assert elapsed < 2.0, f"_mirror waited for the shadow ({elapsed:.2f}s)"
    release.set()
    _settle(s)


def test_the_shadow_never_touches_the_prediction_pool():
    """Sharing `_predict_pool` would let a slow shadow starve inference of the threads that serve
    it, and the failure would look like production latency rather than like a shadow.

    Asserted on the pools themselves rather than by reading the source — a grep for
    `_predict_pool` also matches the comment explaining why it is not used.
    """
    s = _server()
    _enable()
    s._predict_pool = MagicMock()
    s._resolve = lambda m, a, v: {
        "model": MagicMock(predict=lambda x: [1.0]),
        "version": "2",
        "alias": "Staging",
        "run_id": "r",
    }
    s._mirror("JPCP", [[1.0]], 10.0)
    _settle(s)
    s._predict_pool.submit.assert_not_called()


def test_excess_work_is_dropped_rather_than_queued():
    """An unbounded queue turns a shadow that is merely slower than the champion into unbounded
    memory growth on a production replica."""
    s = _server(max_inflight=1)
    _enable()
    release = threading.Event()
    s._resolve = lambda m, a, v: {
        "model": MagicMock(predict=lambda x: release.wait(5) or [1.0]),
        "version": "2",
        "alias": "Staging",
        "run_id": "r",
    }
    s._mirror("JPCP", [[1.0]], 10.0)  # occupies the single slot
    for _ in range(5):
        s._mirror("JPCP", [[1.0]], 10.0)  # must be dropped, not queued
    with s._shadow_lock:
        assert s._shadow_inflight == 1
    release.set()
    _settle(s)


def test_a_drop_is_counted_rather_than_silent():
    """A scoreboard built only from the requests that happened to fit would misrepresent the
    comparison it exists to make."""
    s = _server(max_inflight=0)
    _enable()
    s._mirror("JPCP", [[1.0]], 10.0)
    statuses = [
        c.kwargs.get("tags", {}).get("status") for c in s._shadow_counter.inc.call_args_list
    ]
    assert "dropped" in statuses


# ── what it refuses to record ────────────────────────────────────────────────


def test_a_non_numeric_prediction_is_not_recorded():
    """`shadow_results` stores REAL columns and a percentage difference; a row with NULLs on
    both sides is one no comparison can ever use."""
    s = _server()
    _enable()
    s._resolve = lambda m, a, v: {
        "model": MagicMock(predict=lambda x: ["cat"]),
        "version": "2",
        "alias": "Staging",
        "run_id": "r",
    }
    s._mirror("JPCP", [[1.0]], "dog")
    _settle(s)
    assert _rows() == []


def test_a_zero_champion_prediction_records_without_a_diff():
    """Dividing by it would raise inside the shadow path; the pair is still worth keeping."""
    s = _server()
    _enable()
    s._resolve = lambda m, a, v: {
        "model": MagicMock(predict=lambda x: [5.0]),
        "version": "2",
        "alias": "Staging",
        "run_id": "r",
    }
    s._mirror("JPCP", [[1.0]], 0.0)
    _settle(s)
    rows = _rows()
    assert len(rows) == 1 and rows[0]["diff_pct"] is None


def test_the_config_lookup_is_cached_so_it_is_not_a_per_request_read():
    """Consulted on every request. A SQLite read per prediction would put the shadow feature's
    cost on the production path, which is what clause 1 forbids."""
    s = _server()
    _enable()
    assert s._shadow_target("JPCP") == "Staging"
    from examlops.platform_db import get_db

    with get_db() as conn:
        conn.execute("UPDATE shadow_config SET enabled=0 WHERE model='JPCP'")
    assert s._shadow_target("JPCP") == "Staging", "the cache was not used"
    s._shadow_ttl = -1  # expire it
    assert s._shadow_target("JPCP") is None, "the cache never expires"
