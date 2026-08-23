"""`exa chat` is a launcher, and the thing worth guarding is that it launches correctly.

It deliberately does not implement a REPL: the terminal client is kube-q (`kq`), adopted
unforked from PyPI, and the platform adapts to it through the agent's OpenAI-compatible
bridge. That decision only pays off if the launcher does the one job the Makefile cannot —
resolve *this* CLI's configuration, so ``exa -c lxp chat`` reaches the lxp agent without
anyone exporting a variable.

It also has to be honest when the client is missing. The error names an install command, and
an install command that does not resolve is worse than no hint at all — hence the packaging
check at the bottom: the `chat` extra has to exist and has to name kube-q.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli.main import app  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
runner = CliRunner()


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("AGENT_API_KEY", raising=False)


def test_missing_client_names_an_install_command(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: None)
    result = runner.invoke(app, ["chat"])
    assert "not installed" in result.output
    assert "examlops[chat]" in result.output


def test_the_advertised_install_command_actually_resolves():
    """The hint promises `examlops[chat]`; that extra has to exist and name the client."""
    pyproject = tomllib.loads((REPO / "platform" / "cli" / "pyproject.toml").read_text())
    extras = pyproject["project"]["optional-dependencies"]
    assert "chat" in extras, "exa chat's error message points at an extra that does not exist"
    assert any("kube-q" in dep for dep in extras["chat"]), extras["chat"]


def test_it_launches_the_client_against_the_configured_agent(monkeypatch, tmp_path):
    """The whole point over `make skipper-chat`: the URL comes from the CLI's own config."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: "/fake/kq")
    seen: dict[str, list[str]] = {}

    def fake_call(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr("subprocess.call", fake_call)
    runner.invoke(app, ["chat"])

    argv = seen["argv"]
    assert argv[0] == "/fake/kq"
    assert "--url" in argv
    url = argv[argv.index("--url") + 1]
    assert url.startswith("http")
    assert not url.endswith("/"), "a trailing slash doubles the path in kq's request"
    assert "--api-key" not in argv, "no token configured — none should be passed"


def test_a_configured_token_is_forwarded(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_API_KEY", "s3cret")
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: "/fake/kq")
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr("subprocess.call", lambda argv: seen.setdefault("argv", argv) and 0)
    runner.invoke(app, ["chat"])
    argv = seen["argv"]
    assert argv[argv.index("--api-key") + 1] == "s3cret"


def test_extra_arguments_reach_the_client(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: "/fake/kq")
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr("subprocess.call", lambda argv: seen.setdefault("argv", argv) and 0)
    runner.invoke(app, ["chat", "--", "--resume", "last"])
    assert seen["argv"][-2:] == ["--resume", "last"]


def test_json_mode_refuses_instead_of_opening_a_repl(monkeypatch, tmp_path):
    """`exa --json chat` in a script would hang forever waiting on a terminal."""
    _isolate(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr("subprocess.call", lambda argv: called.append(argv) or 0)
    result = runner.invoke(app, ["--json", "chat"])
    assert not called, "json mode launched an interactive client"
    assert "interactive" in result.output
