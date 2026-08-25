"""`exa ask` streaming — the wire contract between the CLI and the Skipper bridge.

The bridge (`skipper/oai_compat.py`) has served SSE since it was written, but `exa ask` always
asked for `stream: false`, so a long answer was silence and then a wall of text — and against the
120 s timeout, indistinguishable from a hang. These tests pin the streaming path against a stub,
which is what makes them useful now: they need no LLM backend, so they hold even while Skipper has
none (see the note's T11).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from examlops.cli import _client  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _token(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}]}


def _stop(*, hitl: bool = False, action_id: str | None = None) -> dict:
    choice: dict = {"index": 0, "delta": {}, "finish_reason": "stop"}
    if hitl:
        choice["hitl_required"] = True
        choice["action_id"] = action_id
    return {"choices": [choice]}


class _FakeResponse:
    """Stands in for urllib's HTTPResponse: a context manager that iterates raw lines."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._lines = [ln + b"\n" for c in chunks for ln in c.split(b"\n")[:-1]]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self):
        return iter(self._lines)


@pytest.fixture
def stub_stream(monkeypatch):
    """Install a fake urlopen serving a fixed SSE script; returns the captured request."""
    captured: dict = {}

    def _install(frames: list[dict], trailer: bytes = b"data: [DONE]\n\n"):
        body = b"".join(_sse(f) for f in frames) + trailer

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["accept"] = req.headers.get("Accept")
            captured["timeout"] = timeout
            return _FakeResponse([body])

        monkeypatch.setattr(_client.urllib.request, "urlopen", fake_urlopen)
        return captured

    return _install


class TestTheStreamIsActuallyRequested:
    def test_stream_flag_reaches_the_bridge(self, stub_stream):
        captured = stub_stream([_token("ok"), _stop()])
        result = runner.invoke(app, ["ask", "--stream", "hello"])
        assert result.exit_code == 0, result.output
        # The whole defect was this field being False while the server offered SSE.
        assert captured["body"]["stream"] is True
        assert captured["accept"] == "text/event-stream"
        assert captured["url"].endswith("/v1/chat/completions")

    def test_no_stream_keeps_the_blocking_path(self, monkeypatch):
        seen: dict = {}

        def fake_post(url, body, token="", timeout=10.0):
            seen["body"] = body
            return {"choices": [{"message": {"content": "whole answer"}, "finish_reason": "stop"}]}

        monkeypatch.setattr(_client, "post", fake_post)
        result = runner.invoke(app, ["ask", "--no-stream", "hello"])
        assert result.exit_code == 0, result.output
        assert seen["body"]["stream"] is False
        assert "whole answer" in result.output

    def test_json_mode_never_streams(self, monkeypatch):
        """--json must stay one parseable object, so it must not take the streaming path."""
        monkeypatch.setattr(
            _client,
            "post",
            lambda *a, **k: {"choices": [{"message": {"content": "hi"}}]},
        )
        monkeypatch.setattr(
            _client,
            "post_sse",
            lambda *a, **k: pytest.fail("json mode must not stream"),
        )
        result = runner.invoke(app, ["--json", "ask", "hello"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["answer"] == "hi"

    def test_auto_default_does_not_stream_when_piped(self, monkeypatch):
        """A piped consumer wants the whole answer; only a terminal benefits from tokens."""
        seen: dict = {}

        def fake_post(url, body, token="", timeout=10.0):
            seen["stream"] = body["stream"]
            return {"choices": [{"message": {"content": "hi"}}]}

        monkeypatch.setattr(_client, "post", fake_post)
        monkeypatch.setattr("examlops.cli.commands.ask_cmd._is_terminal", lambda: False)
        result = runner.invoke(app, ["ask", "hello"])
        assert result.exit_code == 0, result.output
        assert seen["stream"] is False

    def test_auto_default_streams_at_a_terminal(self, stub_stream, monkeypatch):
        captured = stub_stream([_token("hi"), _stop()])
        monkeypatch.setattr("examlops.cli.commands.ask_cmd._is_terminal", lambda: True)
        result = runner.invoke(app, ["ask", "hello"])
        assert result.exit_code == 0, result.output
        assert captured["body"]["stream"] is True


class TestWhatTheUserSees:
    def test_tokens_are_assembled_in_order(self, stub_stream):
        stub_stream([_token("Two models "), _token("are "), _token("drifting."), _stop()])
        result = runner.invoke(app, ["ask", "--stream", "what is drifting?"])
        assert result.exit_code == 0, result.output
        assert "Two models are drifting." in result.output

    def test_tool_calls_are_shown_while_the_agent_works(self, stub_stream):
        """The tool loop is the slow part; these frames are the only sign of life during it."""
        stub_stream(
            [
                {
                    "ki_event": {
                        "type": "tool_call",
                        "tool_name": "drift_status",
                        "message": "drift_status",
                    }
                },
                _token("JPCP is critical."),
                _stop(),
            ]
        )
        result = runner.invoke(app, ["ask", "--stream", "why?"])
        assert result.exit_code == 0, result.output
        assert "drift_status" in result.output
        assert "JPCP is critical." in result.output

    def test_hitl_flag_on_the_final_chunk_still_prints_the_hint(self, stub_stream):
        stub_stream([_token("Ready to retrain."), _stop(hitl=True, action_id="act.stream")])
        result = runner.invoke(app, ["ask", "--stream", "retrain jpcp", "--session", "s1"])
        assert result.exit_code == 0, result.output
        assert "approval" in result.output.lower()
        assert "--session s1" in result.output
        assert "--approve act.stream" in result.output

    def test_answer_is_not_printed_twice(self, stub_stream):
        stub_stream([_token("unique-answer-token"), _stop()])
        result = runner.invoke(app, ["ask", "--stream", "hello"])
        assert result.output.count("unique-answer-token") == 1

    def test_model_output_is_not_treated_as_markup(self, stub_stream):
        """A bracket in an answer is text. Rich would otherwise eat it as a style tag."""
        stub_stream([_token("see [warn] in the log"), _stop()])
        result = runner.invoke(app, ["ask", "--stream", "hello"])
        assert result.exit_code == 0, result.output
        assert "[warn]" in result.output

    def test_empty_stream_warns_rather_than_printing_nothing(self, stub_stream):
        stub_stream([_stop()])
        result = runner.invoke(app, ["ask", "--stream", "hello"])
        assert result.exit_code == 0, result.output
        assert "empty answer" in result.output.lower()


class TestTheStreamIsParsedDefensively:
    def test_done_sentinel_ends_iteration(self, stub_stream):
        stub_stream(
            [_token("before"), _stop()],
            trailer=b"data: [DONE]\n\ndata: " + json.dumps(_token("after")).encode() + b"\n\n",
        )
        result = runner.invoke(app, ["ask", "--stream", "hello"])
        assert "before" in result.output
        assert "after" not in result.output

    def test_non_json_frames_do_not_abort_the_stream(self, stub_stream):
        stub_stream([_token("kept")], trailer=b"data: not json\n\n: keep-alive\n\ndata: [DONE]\n\n")
        result = runner.invoke(app, ["ask", "--stream", "hello"])
        assert result.exit_code == 0, result.output
        assert "kept" in result.output

    def test_unreachable_agent_degrades_with_a_hint(self, monkeypatch):
        def boom(*a, **k):
            raise _client.ClientError("connection refused")
            yield  # pragma: no cover - generator marker

        monkeypatch.setattr(_client, "post_sse", boom)
        result = runner.invoke(app, ["ask", "--stream", "hello"])
        assert "make skipper-server" in result.output

    def test_silent_stream_is_reported_as_silence_not_as_a_generic_timeout(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(_client.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(_client.ClientError, match="went silent"):
            list(_client.post_sse("http://x/v1/chat/completions", {}, timeout=7.0))

    def test_a_200_that_is_not_json_is_an_error_not_a_traceback(self, monkeypatch):
        """Found while proving the streaming path: a proxy's HTML error page, an SSE stream sent
        to the blocking endpoint, or a captive portal all reached the user as a raw JSONDecodeError
        traceback from inside the stdlib. This affects every `exa` command, not just `ask`."""

        class _Body:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def read(self):
                return b"<html><body>502 Bad Gateway</body></html>"

        monkeypatch.setattr(_client.urllib.request, "urlopen", lambda req, timeout=None: _Body())
        with pytest.raises(_client.ClientError, match="not JSON"):
            _client.post("http://x/api", {})
