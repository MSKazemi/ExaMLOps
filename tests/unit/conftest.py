"""Shared collection-time setup for tests/unit.

Import the real ``prometheus_client`` before any test module runs. Several bridge
tests (``test_drift_tracker``, ``test_seanerbus_bridge``) install a stub
``prometheus_client`` *only if it is not already in ``sys.modules``* (to keep their
``importlib.reload`` of the bridge free of duplicate-metric errors). That stub lacks
``REGISTRY``/``generate_latest``, which breaks the metrics tests when a bridge test
happens to import first. Pre-importing the real module here makes the guard skip the
stub in every collection order — the same condition the full suite already runs under
(where ``test_control_plane_metrics`` imports it first) and is green.
"""

from __future__ import annotations

import os
import socket

import prometheus_client  # noqa: F401  (imported for its sys.modules side effect)
import pytest

# --- unit tests may not talk to a running ExaMLOps service ------------------------------
#
# Two launcher tests in `test_agent_status_cmd.py` were green for months only because a real
# Skipper agent happened to be listening on :18004 on the developer's laptop. Nothing was wrong
# with the assertions; they simply measured whatever was running. The same test on a machine
# without the agent would have failed, and on a machine with a *different* agent would have
# passed while proving nothing.
#
# The rule is narrow on purpose. Unit tests legitimately open sockets — `test_vlm_serving_engine`
# starts its own HTTPServer on an ephemeral port, `test_datastore_reachability` probes a port it
# closed itself, and `test_cli_mcp` points at 127.0.0.1:1 precisely because nothing is there.
# What is never legitimate is reaching *the platform's own service ports on this host*: whether
# they answer is a property of the developer's machine, not of the code under test.

_LIVE_PORTS = {
    14200: "Prefect",
    15000: "MLflow",
    18001: "Ray Serve",
    18002: "the control plane",
    18004: "the Skipper agent",
    18010: "the dataplane service",
    18099: "the dashboard",
}
_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


class LiveServiceContacted(BaseException):
    """Raised when a unit test reaches a platform service port on this host.

    A ``BaseException``, not an ``Exception``, and that is the whole point. Every piece of
    service-probing code in this repo wraps its socket call in ``except Exception`` — that is what
    a health probe *is* — so an ``AssertionError`` raised by the guard was caught by the code under
    test and turned into "the service is down". The guard then reported nothing, the test passed,
    and it had still measured the developer's machine. Verified on the control plane's ``_ping``,
    whose ``except Exception`` swallowed the guard whole. ``BaseException`` passes straight through
    those handlers, the same way ``KeyboardInterrupt`` does, and pytest reports it as an error.
    """


@pytest.fixture(autouse=True)
def _no_live_services():
    # Restores by hand rather than through `monkeypatch`: an autouse conftest fixture that requests
    # `monkeypatch` pulls it earlier in setup order for every test, inverting teardown order against
    # any fixture that assumed monkeypatch had already run. The dashboard's copy of this guard did
    # exactly that and errored a settings test's teardown. A guard must not reorder its own suite.
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address):
        try:
            host, port = address[0], address[1]
        except (TypeError, IndexError, KeyError):
            return
        if port in _LIVE_PORTS and str(host) in _LOCAL:
            raise LiveServiceContacted(
                f"this unit test connected to {_LIVE_PORTS[port]} at {host}:{port}. "
                "Whether that service is running is a property of this machine, not of the code "
                "under test — stub the client (see tests/unit/test_cli_chat.py::_isolate) or "
                "point at a port nothing serves."
            )

    def guard(self, address, *a, **k):
        _check(address)
        return real_connect(self, address, *a, **k)

    def guard_ex(self, address, *a, **k):
        _check(address)
        return real_connect_ex(self, address, *a, **k)

    socket.socket.connect = guard
    socket.socket.connect_ex = guard_ex
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex


@pytest.fixture(autouse=True)
def _no_cloud_metadata_endpoint():
    """Keep pyarrow's S3 client (the AWS C++ SDK) off the EC2 metadata endpoint.

    The platform snapshot store is not anonymous without explicit keys (task 22b), so building it
    walks the AWS default credential chain; off-cloud the metadata-endpoint leg alone costs ~14 s of
    timeouts per construction, and its sockets are opened from C++, out of `_no_live_services`'
    reach. No unit test may wait for — or depend on — that endpoint. Restored by hand for the
    reason `_no_live_services` gives.
    """
    before = os.environ.get("AWS_EC2_METADATA_DISABLED")
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("AWS_EC2_METADATA_DISABLED", None)
        else:
            os.environ["AWS_EC2_METADATA_DISABLED"] = before


@pytest.fixture
def dead_services(monkeypatch):
    """Point every platform service URL at a port nothing serves.

    Tests that call a tool for its *degrade* path — "must return an envelope, not raise" — were
    reaching the developer's real endpoints, so what they exercised depended on which containers
    happened to be up: the error branch on a bare laptop, the success branch with the stack
    running. Neither is the claim under test. ``127.0.0.1:1`` refuses instantly, so the failure
    path is the one that runs, everywhere, and the suite stops paying connection timeouts for it.
    """
    for var in (
        "CONTROL_PLANE_URL",
        "RAY_SERVE_URL",
        "MLFLOW_TRACKING_URI",
        "PREFECT_API_URL",
        "DASHBOARD_URL",
        "AGENT_URL",
    ):
        monkeypatch.setenv(var, "http://127.0.0.1:1")
