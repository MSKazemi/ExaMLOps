from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from exa_agent import cli


# ── Original tests (updated for tuple return) ─────────────────────────────────

def test_slash_help_lists_commands(capsys):
    handled, _ = cli.handle_slash("/help", state=cli.CliState())
    assert handled is True
    out = capsys.readouterr().out
    assert "/new" in out and "/tools" in out and "/resume" in out


def test_slash_new_changes_thread():
    state = cli.CliState(thread_id="old")
    cli.handle_slash("/new", state=state)
    assert state.thread_id != "old"


def test_non_slash_returns_false():
    handled, _ = cli.handle_slash("what models exist?", state=cli.CliState())
    assert handled is False


def test_format_interrupt_summary():
    text = cli.format_interrupt({"action": "trigger_retrain", "summary": "Trigger retrain of JPCP"})
    assert "trigger_retrain" in text and "Trigger retrain of JPCP" in text


# ── Feature 7: _extract_text and colour helper ────────────────────────────────

def test_extract_text_string():
    assert cli._extract_text("hello") == "hello"


def test_extract_text_list_filters_thinking():
    content = [
        {"type": "thinking", "thinking": "internal reasoning..."},
        {"type": "text", "text": "visible answer"},
    ]
    assert cli._extract_text(content) == "visible answer"


def test_extract_text_empty_list():
    assert cli._extract_text([]) == ""


def test_c_returns_plain_text_when_color_off():
    with patch.object(cli, "_USE_COLOR", False):
        assert cli._c(cli._C.RED, "hello") == "hello"


def test_c_wraps_in_ansi_when_color_on():
    with patch.object(cli, "_USE_COLOR", True):
        result = cli._c(cli._C.RED, "hello")
        assert result.startswith("\033[")
        assert "hello" in result
        assert result.endswith("\033[0m")


# ── Feature 5: Token cost calculation ─────────────────────────────────────────

def test_cost_per_1m_has_opus():
    assert "claude-opus-4-8" in cli._COST_PER_1M
    in_price, out_price = cli._COST_PER_1M["claude-opus-4-8"]
    assert in_price == 5.0 and out_price == 25.0


def test_cost_formula():
    in_p, out_p = cli._COST_PER_1M["claude-opus-4-8"]
    cost = (1000 * in_p + 500 * out_p) / 1_000_000
    assert abs(cost - 0.01750) < 1e-6


# ── Feature 1: /history ───────────────────────────────────────────────────────

def _make_fake_graph(messages):
    state_mock = MagicMock()
    state_mock.values = {"messages": messages}
    graph = MagicMock()
    graph.get_state.return_value = state_mock
    return graph


def test_history_command_prints_messages(capsys):
    msgs = [HumanMessage(content="Hello"), AIMessage(content="World")]
    graph = _make_fake_graph(msgs)
    state = cli.CliState(thread_id="t1")
    handled, _ = cli.handle_slash("/history 5", state=state, graph=graph)
    assert handled
    out = capsys.readouterr().out
    assert "Hello" in out
    assert "World" in out


def test_history_defaults_to_10(capsys):
    msgs = [HumanMessage(content=f"msg{i}") for i in range(15)]
    graph = _make_fake_graph(msgs)
    state = cli.CliState(thread_id="t2")
    cli.handle_slash("/history", state=state, graph=graph)
    out = capsys.readouterr().out
    # Should show "showing last 10"
    assert "last 10" in out


def test_history_no_graph(capsys):
    handled, _ = cli.handle_slash("/history", state=cli.CliState(), graph=None)
    assert handled
    out = capsys.readouterr().out
    assert "no conversation" in out


# ── Feature 2: /export ────────────────────────────────────────────────────────

def test_export_writes_markdown(tmp_path):
    msgs = [HumanMessage(content="Ask"), AIMessage(content="Answer")]
    graph = _make_fake_graph(msgs)
    out_file = str(tmp_path / "out.md")
    state = cli.CliState(thread_id="t3")
    cli.handle_slash(f"/export {out_file}", state=state, graph=graph)
    content = Path(out_file).read_text()
    assert "Ask" in content and "Answer" in content
    assert "ExaMLOps Thread" in content


def test_export_default_filename(tmp_path, capsys):
    graph = _make_fake_graph([])
    state = cli.CliState(thread_id="tX")
    # Export with no arg — should write to exa-tX.md in cwd; just check no crash
    with patch("exa_agent.cli.Path") as MockPath:
        MockPath.return_value.write_text = MagicMock()
        cli.handle_slash("/export", state=state, graph=graph)
    out = capsys.readouterr().out
    assert "Exported" in out


# ── Feature 3: /grep ──────────────────────────────────────────────────────────

def test_grep_finds_match(capsys):
    msgs = [HumanMessage(content="drift is critical"), AIMessage(content="model JPCP needs attention")]
    graph = _make_fake_graph(msgs)
    state = cli.CliState()
    cli.handle_slash("/grep critical", state=state, graph=graph)
    out = capsys.readouterr().out
    assert "drift is critical" in out


def test_grep_no_match(capsys):
    msgs = [HumanMessage(content="everything is fine")]
    graph = _make_fake_graph(msgs)
    state = cli.CliState()
    cli.handle_slash("/grep xyz_not_found", state=state, graph=graph)
    out = capsys.readouterr().out
    assert "No matches" in out


def test_grep_case_insensitive(capsys):
    msgs = [HumanMessage(content="JPCP model is loaded")]
    graph = _make_fake_graph(msgs)
    state = cli.CliState()
    cli.handle_slash("/grep jpcp", state=state, graph=graph)
    out = capsys.readouterr().out
    assert "JPCP" in out


def test_grep_no_pattern(capsys):
    handled, _ = cli.handle_slash("/grep", state=cli.CliState())
    assert handled
    out = capsys.readouterr().out
    assert "Usage" in out


# ── Feature 4: /watch command parsing ────────────────────────────────────────

def test_watch_bad_args(capsys):
    handled, _ = cli.handle_slash("/watch notanumber query", state=cli.CliState())
    assert handled
    out = capsys.readouterr().out
    assert "Usage" in out


def test_watch_missing_query(capsys):
    handled, _ = cli.handle_slash("/watch 5", state=cli.CliState())
    assert handled
    out = capsys.readouterr().out
    assert "Usage" in out


# ── Feature 10: startup brief ─────────────────────────────────────────────────

def test_startup_brief_prints_services(capsys):
    with patch("exa_agent.cli.httpx.Client") as MockClient:
        resp = MagicMock()
        resp.is_success = True
        MockClient.return_value.__enter__.return_value.get.return_value = resp
        cli._startup_brief()
    out = capsys.readouterr().out
    assert "Services:" in out
    assert "UP" in out
