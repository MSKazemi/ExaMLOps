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
    with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
        url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
    )):
        with pytest.raises(ClientError, match="Not found"):
            get("http://localhost:18002/approvals/MISSING")

def test_get_raises_client_error_on_connection_error():
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        with pytest.raises(ClientError, match="unreachable"):
            get("http://localhost:18002/health")
