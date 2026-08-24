import os
import sys

# platform/services/control_plane  (parent of the tests/ dir)
#
# The service runs with its own directory as the working directory, so `app.py` imports
# `model_meta` and `metrics` as top-level modules. Its tests import them the same way, which
# only resolves if that directory is on sys.path. Without this the suite did not even
# collect — and nothing noticed, because until 2026-08-20 no gate ran it. Same idiom as
# platform/services/agent/tests/conftest.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --- this suite may not probe a service running on the developer's machine ------------------
#
# `GET /status` fans out to MLflow, Prefect, Ray Serve and the dashboard, and four tests call it
# for reasons that have nothing to do with those peers (the approval count, the reported probe
# address). They reached whatever was listening on this host: the failure branch on a bare laptop,
# the success branch with the stack up, and up to 15 seconds per probe against a port that DROPs
# rather than refuses. `test_status_runs_concurrently` already shows the isolated form — it patches
# `urllib.request.urlopen` — but nothing made that the rule, so three later tests did not.
#
# Deliberately a copy of the guard in tests/unit/conftest.py rather than a shared import: the
# control-plane CI job installs fastapi, uvicorn, prometheus_client, pyyaml and pytest and no
# `examlops` at all, because this service does not depend on the platform package. A shared helper
# would make its own test suite the one thing that does.
#
# Neither fixture requests `monkeypatch`: an autouse conftest fixture that does pulls monkeypatch
# earlier in setup order for every test in the suite, which pushes its teardown *later* than any
# fixture that assumed the environment was already restored. That inverted ordering errored a
# dashboard settings test when its copy of this guard was written.

import socket  # noqa: E402

import pytest  # noqa: E402

_PEER_URL_VARS = ("MLFLOW_TRACKING_URI", "PREFECT_API_URL", "RAY_SERVE_URL", "DASHBOARD_URL")
_DEAD = "http://127.0.0.1:1"

_LIVE_PORTS = {
    14200: "Prefect",
    15000: "MLflow",
    18001: "Ray Serve",
    18002: "another control plane",
    18004: "the Skipper agent",
    18099: "the dashboard",
}
_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


@pytest.fixture(autouse=True)
def _peers_point_nowhere():
    """Every peer the service probes answers "refused", instantly and everywhere.

    Set before the `cp` fixtures reload `app`, because the module reads these into constants at
    import time. Port 1 refuses immediately, so the failure path is the one that runs on every
    machine and the suite stops paying connection timeouts to reach it.
    """
    saved = {var: os.environ.get(var) for var in _PEER_URL_VARS}
    for var in _PEER_URL_VARS:
        os.environ[var] = _DEAD
    try:
        yield
    finally:
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


class LiveServiceContacted(BaseException):
    """Raised when a test reaches a platform service port on this host.

    A ``BaseException``, not an ``Exception``, and that is the whole point: every service probe in
    this repo wraps its socket call in ``except Exception``, so an ``AssertionError`` raised here
    was caught by the code under test and became "the service is down" — guard silent, test green,
    machine still measured. ``BaseException`` passes through those handlers the way
    ``KeyboardInterrupt`` does. Each suite keeps its own copy on purpose (see this file's header).
    """


@pytest.fixture(autouse=True)
def _no_live_services():
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address):
        try:
            host, port = address[0], address[1]
        except (TypeError, IndexError, KeyError):
            return
        if port in _LIVE_PORTS and str(host) in _LOCAL:
            raise LiveServiceContacted(
                f"this test connected to {_LIVE_PORTS[port]} at {host}:{port}. Whether that "
                "service is running is a property of this machine, not of the control plane — "
                "patch `urllib.request.urlopen` (see test_status_runs_concurrently) or leave the "
                "peer URLs pointing at the dead port this suite already sets."
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
