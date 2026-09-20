"""Unit tests for DriftTracker in dataplane_bus_bridge.

Imports DriftTracker by stubbing out the heavy C-extension dependencies
(capnp, dataplane-bus, prometheus_client) before importing the bridge module,
following the same pattern used in test_dataplane_bus_bridge.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

# ── stub heavy dependencies so bridge imports succeed without dataplane-bus ─────


def _ensure_stubs():
    """Install minimal stubs for all bridge dependencies that are not present."""
    if "capnp" not in sys.modules:
        capnp_stub = types.ModuleType("capnp")
        capnp_stub.remove_import_hook = lambda: None  # type: ignore[attr-defined]
        capnp_stub.load = lambda *a, **kw: MagicMock()  # type: ignore[attr-defined]
        capnp_stub.run = lambda coro: coro  # type: ignore[attr-defined]
        sys.modules["capnp"] = capnp_stub

    if "dataplane_bus" not in sys.modules:
        sb = types.ModuleType("dataplane_bus")
        sb_client = types.ModuleType("dataplane_bus.client")
        sb_client.Connection = MagicMock()  # type: ignore[attr-defined]
        sb.client = sb_client  # type: ignore[attr-defined]
        sys.modules["dataplane_bus"] = sb
        sys.modules["dataplane_bus.client"] = sb_client

    if "dataplane_bus_client" not in sys.modules:
        sc = types.ModuleType("dataplane_bus_client")
        sc.Connection = MagicMock()  # type: ignore[attr-defined]
        sys.modules["dataplane_bus_client"] = sc

    if "prometheus_client" not in sys.modules:
        prom = types.ModuleType("prometheus_client")
        prom.Counter = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
        prom.Gauge = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
        prom.Histogram = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
        prom.generate_latest = MagicMock(return_value=b"")  # type: ignore[attr-defined]
        prom.CONTENT_TYPE_LATEST = "text/plain"  # type: ignore[attr-defined]
        sys.modules["prometheus_client"] = prom

    if "dataplane_bus_msgs" not in sys.modules:
        msgs = types.ModuleType("dataplane_bus_msgs")
        for cls_name in (
            "HpcJobV1",
            "HpcInferenceResV1",
            "RetrainReqV1",
            "RetrainResV1",
            "VectorReqV1",
            "VectorResV1",
        ):
            setattr(msgs, cls_name, MagicMock())
        sys.modules["dataplane_bus_msgs"] = msgs

    if "model_schema_registry" not in sys.modules:
        msr = types.ModuleType("model_schema_registry")
        mock_registry = MagicMock()
        mock_registry.return_value = MagicMock()
        msr.ModelSchemaRegistry = mock_registry  # type: ignore[attr-defined]
        sys.modules["model_schema_registry"] = msr


_ensure_stubs()

# Add platform/clients to path so `import dataplane_bus_bridge` resolves.
_clients_dir = os.path.join(os.path.dirname(__file__), "..", "..", "platform", "clients")
if _clients_dir not in sys.path:
    sys.path.insert(0, _clients_dir)

import dataplane_bus_bridge as bridge  # noqa: E402

# Pop the stub so test_model_schema_registry.py can import the real module later.
sys.modules.pop("model_schema_registry", None)

DriftTracker = bridge.DriftTracker


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_tracker(window: int = 5, threshold: float = 0.5, cooldown: int = 300) -> DriftTracker:
    return DriftTracker(window=window, threshold=threshold, cooldown=cooldown)


def _mock_async_client(status_code: int = 200):
    """Return a mock httpx.AsyncClient context manager that records POST calls."""
    response = MagicMock()
    response.status_code = status_code
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=response)
    return client


# ── tests ────────────────────────────────────────────────────────────────────


async def test_no_trigger_below_window():
    """record() with fewer than `window` entries must NOT schedule a retrain task."""
    tracker = _make_tracker(window=5)
    mock_client = _mock_async_client()

    with patch("dataplane_bus_bridge.httpx.AsyncClient", return_value=mock_client):
        for _ in range(4):  # one short of window=5
            tracker.record("JPCP", False)  # all failures
        await asyncio.sleep(0)  # drain event loop

    mock_client.post.assert_not_called()


async def test_no_trigger_below_threshold():
    """record() with error rate below threshold must NOT call retrain endpoint."""
    tracker = _make_tracker(window=5, threshold=0.5)
    mock_client = _mock_async_client()

    with patch("dataplane_bus_bridge.httpx.AsyncClient", return_value=mock_client):
        # 4 successes + 1 failure = 20% error rate < 50% threshold
        for _ in range(4):
            tracker.record("JPCP", True)
        tracker.record("JPCP", False)
        await asyncio.sleep(0)

    mock_client.post.assert_not_called()


async def test_triggers_on_threshold_breach():
    """Filling window with failures >= threshold triggers POST /retrain with correct payload."""
    tracker = _make_tracker(window=4, threshold=0.5)
    mock_client = _mock_async_client()

    with patch("dataplane_bus_bridge.httpx.AsyncClient", return_value=mock_client):
        # First fill with successes to populate bucket without triggering
        for _ in range(4):
            tracker.record("JPCP", True)
        # Now add 4 failures — each call after index 3 replaces oldest, keeping window=4
        # After recording all failures the bucket is all False → 100% error rate
        for _ in range(4):
            tracker.record("JPCP", False)
        await asyncio.sleep(0)

    assert mock_client.post.called
    call_kwargs = mock_client.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    assert payload["model_name"] == "JPCP"
    assert payload["dataset_name"] == "FDataDataset"
    assert payload["backend_name"] == "dataplane"
    assert payload["is_dummy"] is False


async def test_cooldown_prevents_double_trigger():
    """A second drift breach within cooldown must NOT fire a second POST /retrain."""
    tracker = _make_tracker(window=4, threshold=0.5, cooldown=300)
    mock_client = _mock_async_client()

    with patch("dataplane_bus_bridge.httpx.AsyncClient", return_value=mock_client):
        # First trigger: fill with failures
        for _ in range(4):
            tracker.record("JPCP", False)
        await asyncio.sleep(0)

        first_call_count = mock_client.post.call_count
        assert first_call_count == 1, "Expected exactly one retrain call after first breach"

        # Immediately trigger again (cooldown not expired)
        for _ in range(4):
            tracker.record("JPCP", False)
        await asyncio.sleep(0)

    assert mock_client.post.call_count == 1, "Cooldown should have blocked the second retrain call"


async def test_cooldown_expired_allows_retrigger():
    """After cooldown expires, a second drift breach MUST fire a second POST /retrain."""
    tracker = _make_tracker(window=4, threshold=0.5, cooldown=300)
    mock_client = _mock_async_client()

    with patch("dataplane_bus_bridge.httpx.AsyncClient", return_value=mock_client):
        # First trigger
        for _ in range(4):
            tracker.record("JPCP", False)
        await asyncio.sleep(0)

        assert mock_client.post.call_count == 1

        # Manually expire the cooldown
        tracker._last_retrain["JPCP"] = time.time() - 301

        # Second trigger
        for _ in range(4):
            tracker.record("JPCP", False)
        await asyncio.sleep(0)

    assert mock_client.post.call_count == 2, "Expired cooldown should allow the second retrain call"


async def test_window_is_rolling():
    """After initial failures fill window, replacing them all with successes drops error rate.

    Strategy: fill the window with failures (triggers once), then add `window` more
    successes.  Because coodown=9999 the intermediate mixed states cannot trigger
    another call.  After all successes the bucket is all-True and error_rate==0,
    so even if cooldown were expired no new trigger would fire.  The test verifies:
      1. Bucket length is capped at `window`.
      2. Final bucket contains only successes (error rate == 0).
      3. No additional POST beyond the single initial drift trigger.
    """
    window = 4
    tracker = _make_tracker(window=window, threshold=0.5, cooldown=9999)
    mock_client = _mock_async_client()

    with patch("dataplane_bus_bridge.httpx.AsyncClient", return_value=mock_client):
        # Fill window with failures → triggers retrain (cooldown=9999 blocks re-trigger)
        for _ in range(window):
            tracker.record("JPCP", False)
        await asyncio.sleep(0)

        first_count = mock_client.post.call_count
        assert first_count == 1, "Expected exactly one trigger after filling with failures"

        # Add `window` successes — intermediate mixed states still breach threshold
        # but the 9999-second cooldown prevents any additional POST calls.
        for _ in range(window):
            tracker.record("JPCP", True)
        await asyncio.sleep(0)

    # No new triggers because cooldown blocks them all during the mixed phase
    assert mock_client.post.call_count == first_count, (
        "Cooldown should block re-triggers during the rolling transition"
    )
    # Bucket is capped at `window` and now all successes
    assert len(tracker._results["JPCP"]) == window
    assert all(tracker._results["JPCP"]), "Bucket should contain only successes after rolling over"

    # Confirm error rate is now 0 (below threshold) — future trigger impossible on current bucket
    assert tracker._error_rate(tracker._results["JPCP"]) == 0.0


async def test_multiple_models_independent():
    """Drift for 'JPCP' and 'MACK' are tracked independently; both trigger retrain."""
    tracker = _make_tracker(window=4, threshold=0.5)
    mock_client = _mock_async_client()

    with patch("dataplane_bus_bridge.httpx.AsyncClient", return_value=mock_client):
        for _ in range(4):
            tracker.record("JPCP", False)
        for _ in range(4):
            tracker.record("MACK", False)
        await asyncio.sleep(0)

    assert mock_client.post.call_count == 2

    payloads = [call.kwargs.get("json") or call.args[1] for call in mock_client.post.call_args_list]
    model_names = {p["model_name"] for p in payloads}
    assert model_names == {"JPCP", "MACK"}, f"Expected retrain for both models, got: {model_names}"
