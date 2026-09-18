"""The model server publishes its gauges again and again, not once (Ray 2.55).

Ray exports a gauge only for the report interval in which it was set, so a gauge set at load time
disappeared from Prometheus seconds later: on a healthy install ``RayServeNoModelsLoaded`` fired
through its ``absent()`` arm, and ``ServingSnapshotLagging`` had no applied generation to read.
``tests/integration/test_serving_metrics_live.py`` shows the Ray behaviour; this pins what the
replica republishes from its own state.
"""

from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from serving.ray_serving import app as rs_app  # noqa: E402


def _server(hot: dict, generation: int | None = None):
    server = object.__new__(rs_app.MultiModelServer.func_or_class)
    server._cache_lock = threading.RLock()
    server._hot = dict(hot)
    server._version_cache = OrderedDict()
    server._replica_id = "r1"
    server._snapshot_generation = generation
    for attr in ("_models_gauge", "_snapshot_gauge", "_version_gauge"):
        setattr(server, attr, MagicMock())
    return server


def test_every_gauge_is_published_from_the_current_state():
    server = _server(
        {
            ("jpcp", "Production"): {"model": object(), "version": "18"},
            ("jpcp", "Staging"): {"model": object(), "version": "22"},
        },
        generation=7,
    )
    server._publish_gauges()
    server._models_gauge.set.assert_called_once_with(2, tags={"replica": "r1"})
    server._snapshot_gauge.set.assert_called_once_with(7, tags={"replica": "r1"})
    versions = {
        (c.kwargs["tags"]["model_name"], c.kwargs["tags"]["alias"]): c.args[0]
        for c in server._version_gauge.set.mock_calls
    }
    # Published with no prediction having run: the version gauge used to exist only under traffic.
    assert versions == {("jpcp", "Production"): 18.0, ("jpcp", "Staging"): 22.0}


def test_an_empty_hot_set_is_published_as_zero_not_left_out():
    server = _server({})
    server._publish_gauges()
    server._models_gauge.set.assert_called_once_with(0, tags={"replica": "r1"})
    # Published as 0, not withheld. Withholding it reads as "nothing to claim", which is true of
    # the replica and false of the monitoring: `min()` over no series is an empty vector, so
    # `ServingSnapshotLagging` could not fire for a replica that had applied nothing at all.
    server._snapshot_gauge.set.assert_called_once_with(0, tags={"replica": "r1"})


def test_a_non_numeric_version_is_skipped_not_fatal():
    server = _server({("m", "Production"): {"model": object(), "version": None}})
    server._publish_gauges()
    server._version_gauge.set.assert_not_called()
    server._models_gauge.set.assert_called_once()


def test_the_refresh_is_on_by_default_and_faster_than_a_scrape():
    assert 0 < rs_app.GAUGE_REFRESH_SECONDS <= 10


def test_a_replica_that_has_applied_no_snapshot_publishes_zero_not_nothing():
    """`ServingSnapshotLagging` is `max(published) - min(applied) > 0`, and `min()` over **no**
    series is an empty vector — so the expression is empty and the alert cannot fire.

    A replica that has never applied a snapshot is the most-behind replica there can be: it is
    serving from its fallback registry while a snapshot exists. Publishing `0` makes that state
    representable, and the subtraction then yields the full published generation.
    """
    server = _server({}, generation=None)

    server._publish_gauges()

    calls = [c for c in server._snapshot_gauge.set.call_args_list]
    assert calls, "a replica with no snapshot published no applied-generation at all"
    assert calls[0].args[0] == 0
    assert calls[0].kwargs["tags"] == {"replica": "r1"}
