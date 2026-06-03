from exa_agent import cli


def test_slash_help_lists_commands(capsys):
    handled = cli.handle_slash("/help", state=cli.CliState())
    assert handled is True
    out = capsys.readouterr().out
    assert "/new" in out and "/tools" in out and "/resume" in out


def test_slash_new_changes_thread():
    state = cli.CliState(thread_id="old")
    cli.handle_slash("/new", state=state)
    assert state.thread_id != "old"


def test_non_slash_returns_false():
    assert cli.handle_slash("what models exist?", state=cli.CliState()) is False


def test_format_interrupt_summary():
    text = cli.format_interrupt({"action": "trigger_retrain", "summary": "Trigger retrain of JPCP"})
    assert "trigger_retrain" in text and "Trigger retrain of JPCP" in text
