from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli import _output  # noqa: E402

# ── sparkline ─────────────────────────────────────────────────────────────────


def test_sparkline_empty_or_single_is_blank():
    assert _output.sparkline([]) == ""
    assert _output.sparkline([5]) == ""


def test_sparkline_flat_series_is_lowest_tick():
    out = _output.sparkline([3, 3, 3])
    assert out == "▁▁▁"


def test_sparkline_monotonic_rises():
    out = _output.sparkline([0, 1, 2, 3, 4, 5, 6, 7])
    assert out[0] == "▁"
    assert out[-1] == "█"
    assert len(out) == 8


def test_sparkline_ignores_none():
    out = _output.sparkline([1, None, 5])  # type: ignore[list-item]
    assert len(out) == 2


# ── bar ─────────────────────────────────────────────────────────────────────


def test_bar_full_and_empty():
    assert _output.bar(10, 10, width=10) == "█" * 10
    assert _output.bar(0, 10, width=10) == "░" * 10


def test_bar_half():
    out = _output.bar(5, 10, width=10)
    assert out.count("█") == 5
    assert out.count("░") == 5


def test_bar_clamps_over_max():
    assert _output.bar(50, 10, width=8) == "█" * 8


def test_bar_zero_max_is_empty_track():
    assert _output.bar(1, 0, width=6) == "░" * 6


# ── watch_loop ────────────────────────────────────────────────────────────────


def test_watch_loop_runs_render_then_exits_on_interrupt(monkeypatch):
    calls = {"render": 0}

    def fake_sleep(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", fake_sleep)

    def render():
        calls["render"] += 1

    # Should render once, then Ctrl-C on first sleep and return cleanly.
    _output.watch_loop(render, interval=5)
    assert calls["render"] == 1


def test_watch_loop_clamps_interval(monkeypatch):
    seen = {}

    def fake_sleep(seconds):
        seen["interval"] = seconds
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", fake_sleep)
    _output.watch_loop(lambda: None, interval=0)
    assert seen["interval"] == 1


# ── exa status --watch ────────────────────────────────────────────────────────


def test_status_watch_survives_unreachable_control_plane(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli import _client
    from examlops.cli.main import app

    def unreachable(*a, **k):
        raise _client.ClientError("connection refused")

    monkeypatch.setattr(_client, "get", unreachable)
    # First sleep call ends the loop.
    monkeypatch.setattr("time.sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))

    result = CliRunner().invoke(app, ["status", "--watch", "--interval", "2"])
    # Watch mode must NOT exit non-zero just because the control plane is down.
    assert result.exit_code == 0, result.output
    assert "unreachable" in result.output.lower()
    assert "stopped watching" in result.output.lower()
