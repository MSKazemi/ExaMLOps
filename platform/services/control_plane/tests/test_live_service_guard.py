"""The suite's own guard has to be a copy, so it needs its own proof.

`tests/unit/conftest.py` carries the same rule for the platform suite, and this file is the reason
the duplication is safe to keep: if the copy here ever stops working — a pytest change to fixture
ordering, someone deleting the fixture as unused — these fail rather than the guard quietly
passing everything. The copy is deliberate; the control-plane CI job installs no `examlops`, so a
shared helper would give this service's tests a dependency the service itself does not have.
"""

from __future__ import annotations

import os
import socket

import pytest


def test_reaching_a_platform_service_port_on_this_host_fails_loudly():
    with pytest.raises(BaseException) as caught:  # noqa: B017, PT011
        with socket.socket() as s:
            s.settimeout(0.2)
            s.connect(("127.0.0.1", 18099))
    assert "the dashboard" in str(caught.value)
    assert "urlopen" in str(caught.value), "the message must name the way out"


def test_connect_ex_is_covered_too():
    """`connect_ex` returns an errno instead of raising, so an unguarded probe is silent."""
    with pytest.raises(BaseException) as caught:  # noqa: B017, PT011
        with socket.socket() as s:
            s.settimeout(0.2)
            s.connect_ex(("localhost", 15000))
    assert type(caught.value).__name__ == "LiveServiceContacted"


def test_a_socket_the_test_opened_itself_is_left_alone():
    """The rule is about the platform's ports on this host, not about sockets in general."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        with socket.socket() as client:
            client.settimeout(1.0)
            client.connect(server.getsockname())
    finally:
        server.close()


def test_the_peer_urls_this_suite_runs_under_point_at_a_dead_port():
    """`/status` fans out to these four; the values are what keep the fan-out off this machine."""
    for var in ("MLFLOW_TRACKING_URI", "PREFECT_API_URL", "RAY_SERVE_URL", "DASHBOARD_URL"):
        assert os.environ[var] == "http://127.0.0.1:1", f"{var} was left pointing at a real host"


def test_the_guard_survives_the_except_exception_every_probe_is_written_with():
    """The reason the guard raises a ``BaseException`` and not an ``AssertionError``.

    Service-probing code catches broadly — that is what a probe is. The control plane's own
    ``_ping`` is ``try: urlopen(...) except Exception: return False``, and with an
    ``AssertionError`` the guard fired *inside* that handler: the probe reported "service down",
    the test went green, and it had still contacted whatever was listening on this machine. So the
    guard was not enforcing over exactly the code most likely to need it.
    """

    def probe_the_way_this_repo_writes_probes() -> bool:
        try:
            socket.socket().connect(("127.0.0.1", 18099))
            return True
        except Exception:  # noqa: BLE001 — this breadth is the point of the test
            return False

    with pytest.raises(BaseException) as caught:  # noqa: B017, PT011
        probe_the_way_this_repo_writes_probes()
    assert type(caught.value).__name__ == "LiveServiceContacted"
    assert not isinstance(caught.value, Exception), (
        "an Exception subclass would be swallowed by the handler above and the guard would be "
        "silent for every probe in the codebase"
    )
