"""The guard that stops a unit test from measuring whatever is running on this laptop.

Two launcher tests in ``test_agent_status_cmd.py`` were green for months only because a Skipper
agent happened to be listening on :18004 here. They asserted the right thing and proved nothing:
on a machine without the agent they would have failed, and on a machine running a *different*
agent they would still have passed. The autouse fixture in ``tests/unit/conftest.py`` turns that
class of accident into a loud failure.

These tests exist because the fixture is itself untested code that runs before every unit test in
the repo: if it stopped firing, nothing else would notice, and if it fired too widely it would
break the tests that legitimately open sockets.
"""

from __future__ import annotations

import socket

import pytest


def test_reaching_a_platform_service_port_on_this_host_fails_loudly():
    with pytest.raises(AssertionError) as caught:
        socket.socket().connect(("127.0.0.1", 18004))
    assert "the Skipper agent" in str(caught.value)
    assert "stub the client" in str(caught.value), "the failure must say what to do instead"


def test_connect_ex_is_covered_too():
    """``connect_ex`` returns an errno instead of raising, so a probe written with it would
    otherwise slip past the guard and quietly report whatever the machine is running."""
    with pytest.raises(AssertionError):
        socket.socket().connect_ex(("localhost", 18099))


def test_a_socket_the_test_opened_itself_is_left_alone():
    """The narrowness is the point. ``test_vlm_serving_engine`` starts its own HTTPServer and
    ``test_datastore_reachability`` probes a port it closed — both must keep working."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket() as client:
            client.connect(server.getsockname())  # no exception


def test_a_far_host_on_a_platform_port_is_not_the_platform():
    """Only *this* host's service ports are the accident. ``agent.test:18004`` is a name that
    does not resolve, which is exactly how the launcher tests pin the URL without reaching it."""
    with pytest.raises(OSError):  # DNS failure, not the guard's AssertionError
        socket.socket().connect(("agent.test", 18004))
