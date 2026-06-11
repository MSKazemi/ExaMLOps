from exa_agent import cli


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
