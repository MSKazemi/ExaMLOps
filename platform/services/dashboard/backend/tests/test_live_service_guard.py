"""The guard that stops a dashboard test from measuring whatever is running on this laptop.

The dashboard's copy of the live-service guard (``conftest.py``) has run before every test in this
suite since it was added, and until now nothing tested it. That is the wrong way round: a
guard that silently stopped firing would take the whole protection with it and no test would go
red. The root suite and the control plane each carry the same three-plus-one checks; this file is
the third copy, deliberate for the same reason the fixture itself is copied — the dashboard is a
separate app with its own conftest and no shared import.
"""

from __future__ import annotations

import socket

import pytest


def _a_guarded_port(ports: dict[int, str]) -> int:
    assert ports, "the guard derives its port list from settings; an empty list guards nothing"
    return sorted(ports)[0]


def test_reaching_a_platform_service_port_on_this_host_fails_loudly(guarded_ports):
    port = _a_guarded_port(guarded_ports)
    with pytest.raises(BaseException) as caught:  # noqa: B017, PT011
        socket.socket().connect(("127.0.0.1", port))
    assert str(port) in str(caught.value)
    assert "property of this machine" in str(caught.value)
    assert "mock the client" in str(caught.value), "the failure must say what to do instead"


def test_connect_ex_is_covered_too(guarded_ports):
    """``connect_ex`` returns an errno rather than raising, so a probe written with it would
    otherwise slip past the guard and quietly report whatever this machine is running."""
    with pytest.raises(BaseException) as caught:  # noqa: B017, PT011
        socket.socket().connect_ex(("localhost", _a_guarded_port(guarded_ports)))
    assert type(caught.value).__name__ == "LiveServiceContacted"


def test_a_socket_the_test_opened_itself_is_left_alone():
    """The narrowness is the point: only the platform's own service ports are the accident."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket() as client:
            client.connect(server.getsockname())  # no exception


def test_the_guard_survives_the_except_exception_every_probe_is_written_with(guarded_ports):
    """Why the guard raises a ``BaseException``.

    ``routers/health.py`` pings twelve services and swallows everything each call can throw — that
    is what a health probe is. While the guard raised an ``AssertionError`` it was caught by the
    probe under test and turned into "the service is down": guard silent, test green, and the
    verdict was still whatever happened to be listening on the developer's machine.
    """
    port = _a_guarded_port(guarded_ports)

    def probe_the_way_this_repo_writes_probes() -> bool:
        try:
            socket.socket().connect(("127.0.0.1", port))
            return True
        except Exception:  # noqa: BLE001 — this breadth is the point of the test
            return False

    with pytest.raises(BaseException) as caught:  # noqa: B017, PT011
        probe_the_way_this_repo_writes_probes()
    assert type(caught.value).__name__ == "LiveServiceContacted"
    assert not isinstance(caught.value, Exception)
