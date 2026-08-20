"""`exa ask` must not confuse "the agent is down" with "the agent's LLM failed".

Both used to print "Could not reach the Skipper agent … Start it with: make skipper-server".
That advice is actively wrong in the second case — the agent is running and answering; it is the
LLM backend that is broken — and it sends the operator to restart a healthy service. The two are
different findings and must not look alike.
"""

from __future__ import annotations

import json

import pytest

from examlops.cli import _client


class _FakeHTTPError(Exception):
    """Stands in for urllib's HTTPError: has a code, headers and a readable body."""

    def __init__(self, code: int, body: str):
        super().__init__(f"HTTP {code}")
        self.code = code
        self._body = body.encode()
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._body


def test_a_5xx_surfaces_the_servers_own_message():
    """An upstream LLM failure must reach the operator, not be replaced by a generic string."""
    body = json.dumps({"error": {"message": "Error code: 401 - invalid subscription key"}})
    with pytest.raises(_client.ClientError) as exc:
        _client._raise_http(_FakeHTTPError(500, body), "http://agent/v1/chat/completions")
    assert "invalid subscription key" in str(exc.value)
    assert exc.value.status == 500


def test_a_5xx_without_a_parsable_body_still_says_something_useful():
    with pytest.raises(_client.ClientError) as exc:
        _client._raise_http(_FakeHTTPError(502, "<html>bad gateway</html>"), "http://agent/x")
    assert "Server error 502" in str(exc.value)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (json.dumps({"detail": "fastapi style"}), "fastapi style"),
        (json.dumps({"message": "bare message"}), "bare message"),
        (json.dumps({"error": {"message": "openai style"}}), "openai style"),
        (json.dumps({"error": "string error"}), "string error"),
        ("not json at all", ""),
        (json.dumps(["a", "list"]), ""),
    ],
)
def test_detail_extraction_covers_the_shapes_we_actually_receive(body, expected):
    assert _client._extract_detail(body) == expected


def test_transport_failure_has_no_status_and_an_http_error_does():
    """The status field is what lets `exa ask` tell the two apart — it must stay populated."""
    with pytest.raises(_client.ClientError) as http_exc:
        _client._raise_http(_FakeHTTPError(500, "{}"), "http://agent/x")
    assert http_exc.value.status == 500
    # a transport failure is constructed without a status
    assert _client.ClientError("connection refused").status is None
