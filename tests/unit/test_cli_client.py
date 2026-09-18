from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli._client import ClientError, get, post


def _mock_response(body: dict, status: int = 200):
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = json.dumps(body).encode()
    resp.status = status
    return resp


def test_get_returns_dict():
    resp = _mock_response({"ok": True})
    with patch("urllib.request.urlopen", return_value=resp):
        result = get("http://localhost:18002/health")
    assert result == {"ok": True}


def test_post_sends_json_body():
    resp = _mock_response({"id": "abc"})
    with patch("urllib.request.urlopen", return_value=resp) as mock_open:
        result = post("http://localhost:18002/approve/JPCP", {}, token="tok")
    assert result == {"id": "abc"}
    req = mock_open.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer tok"


def test_get_raises_client_error_on_http_error():
    import urllib.error

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
        ),
    ):
        with pytest.raises(ClientError, match="Not found"):
            get("http://localhost:18002/approvals/MISSING")


def test_get_raises_client_error_on_connection_error():
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        with pytest.raises(ClientError, match="unreachable"):
            get("http://localhost:18002/health")


def test_get_wraps_read_timeout_as_client_error():
    # A bare read-phase socket TimeoutError (not wrapped in URLError) must surface as a
    # ClientError so callers (CLI, MCP tools) degrade gracefully instead of crashing.
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(ClientError, match="Timed out"):
            get("http://localhost:9/slow")


# ── the server's reason must survive the trip (T58 column 1) ────────────────────────────────
#
# `_raise_http` reads the error body and then uses it for 409, 422 and 5xx only. Every other
# code fell through to a bare "HTTP {code} from {url}". 400 is the one that matters: the control
# plane answers an unknown model or dataset with `HTTPException(400, "... Supported: [...]")`,
# which is exactly the information needed to retry — and is exactly what was discarded. An agent
# that is told only "HTTP 400" cannot self-correct, and the operator reading its answer is told
# a number instead of a reason.


def _http_error(code: int, body: str, url: str = "http://localhost:18002/retrain"):
    import io
    import urllib.error

    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body.encode()))


def test_a_400_carries_the_servers_explanation_not_just_the_number():
    body = json.dumps(
        {"detail": "Dataset 'NotADataset' not supported by JPCP. Supported: ['PM100Dataset']"}
    )
    with patch("urllib.request.urlopen", side_effect=_http_error(400, body)):
        with pytest.raises(ClientError) as exc:
            post("http://localhost:18002/retrain", {}, token="tok")
    assert "not supported by JPCP" in str(exc.value)
    assert "PM100Dataset" in str(exc.value)
    assert exc.value.status == 400


def test_an_unmapped_code_still_prefers_the_servers_message():
    with patch(
        "urllib.request.urlopen",
        side_effect=_http_error(418, json.dumps({"detail": "I am a teapot"})),
    ):
        with pytest.raises(ClientError) as exc:
            post("http://localhost:18002/retrain", {}, token="tok")
    assert "I am a teapot" in str(exc.value)


def test_the_status_code_is_still_reported_alongside_the_reason():
    # The reason replaces the bare number, it does not hide it: an operator grepping for the
    # code, and any caller branching on it, must both still work.
    with patch(
        "urllib.request.urlopen", side_effect=_http_error(400, json.dumps({"detail": "nope"}))
    ):
        with pytest.raises(ClientError) as exc:
            post("http://localhost:18002/retrain", {}, token="tok")
    assert "400" in str(exc.value)
    assert exc.value.status == 400


def test_a_body_with_nothing_useful_falls_back_to_the_old_message():
    with patch("urllib.request.urlopen", side_effect=_http_error(400, "<html>gateway</html>")):
        with pytest.raises(ClientError) as exc:
            post("http://localhost:18002/retrain", {}, token="tok")
    assert str(exc.value) == "HTTP 400 from http://localhost:18002/retrain"


def test_a_401_names_the_token_of_the_service_that_refused(monkeypatch):
    """Live pass 2: this helper serves every command, and its 401 named Skipper's `AGENT_API_KEY`
    whatever had answered — so `exa dataplane pull --remote`, which needs
    `EXAMLOPS_DATAPLANE_TOKEN`, pointed operators at a variable it does not read."""
    from examlops.cli import _client

    monkeypatch.setenv("EXAMLOPS_DATAPLANE_URL", "http://localhost:18010")
    with patch("urllib.request.urlopen", side_effect=_http_error(401, "")):
        with pytest.raises(ClientError) as exc:
            post("http://localhost:18010/sources/pm100/pull", {}, token="tok")
    message = str(exc.value)
    assert "EXAMLOPS_DATAPLANE_TOKEN" in message
    assert "exa config set dataplane_token" in message
    assert "AGENT_API_KEY" not in message and "agent_token" not in message
    assert exc.value.status == 401

    # …and the agent still gets its own, from the same table.
    monkeypatch.setenv("AGENT_URL", "http://localhost:18004")
    with patch("urllib.request.urlopen", side_effect=_http_error(401, "")):
        with pytest.raises(ClientError) as agent_exc:
            post("http://localhost:18004/v1/chat/completions", {}, token="tok")
    assert "AGENT_API_KEY" in str(agent_exc.value)

    # A URL belonging to no configured service still says something useful, and nothing wrong.
    assert _client._auth_hint("http://elsewhere.invalid/x") == ""


def test_the_401_hint_survives_an_unreadable_config(monkeypatch):
    """A config this CLI cannot read must not replace the authentication error with its own."""

    def _broken():
        raise RuntimeError("config is a directory")

    monkeypatch.setattr("examlops.cli._config.load_config", _broken)
    with patch("urllib.request.urlopen", side_effect=_http_error(401, "")):
        with pytest.raises(ClientError) as exc:
            post("http://localhost:18010/sources/pm100/pull", {}, token="tok")
    assert "Authentication required" in str(exc.value)
