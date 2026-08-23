"""The agent binds every interface by default, and its token covers one endpoint.

``AGENT_API_KEY`` gates ``POST /v1/chat/completions``. The WebSocket chat at
``/ws/chat/{thread_id}`` — the interface that actually runs tools — and the thread-history
endpoints have no gate at all, and ``AGENT_SERVER_HOST`` defaults to ``0.0.0.0``. An operator who
sets a key has reason to believe the service is protected; it is not.

Closing the gap changes service behaviour (the built-in browser UI cannot send a bearer header on a
WebSocket), so it is a decision, not a patch. What this guards is that the operator is *told*.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "platform/services/agent/agent_server.py"
_spec = importlib.util.spec_from_file_location("agent_server", _SRC)
agent_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_server)


def test_loopback_is_silent():
    for host in ("127.0.0.1", "::1", "localhost"):
        assert agent_server.exposure_warning(host, "") is None, host


def test_binding_every_interface_warns_even_with_a_key_set():
    warning = agent_server.exposure_warning("0.0.0.0", "s3cret")
    assert warning is not None
    # The point of the warning is precisely that the key is not enough.
    assert "only covers /v1/chat/completions" in warning
    assert "AGENT_SERVER_HOST=127.0.0.1" in warning


def test_binding_every_interface_without_a_key_says_so():
    warning = agent_server.exposure_warning("0.0.0.0", "")
    assert warning is not None and "(unset)" in warning


def test_a_named_interface_is_not_treated_as_loopback():
    assert agent_server.exposure_warning("10.0.0.5", "") is not None
