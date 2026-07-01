"""Shared collection-time setup for tests/unit.

Import the real ``prometheus_client`` before any test module runs. Several bridge
tests (``test_drift_tracker``, ``test_dataplane_bridge``) install a stub
``prometheus_client`` *only if it is not already in ``sys.modules``* (to keep their
``importlib.reload`` of the bridge free of duplicate-metric errors). That stub lacks
``REGISTRY``/``generate_latest``, which breaks the metrics tests when a bridge test
happens to import first. Pre-importing the real module here makes the guard skip the
stub in every collection order — the same condition the full suite already runs under
(where ``test_control_plane_metrics`` imports it first) and is green.
"""

from __future__ import annotations

import prometheus_client  # noqa: F401  (imported for its sys.modules side effect)
