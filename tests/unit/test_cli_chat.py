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


def _isolate(monkeypatch, tmp_path, *, reachable=True):
    """Isolate the config **and the agent**.

    Every launch test here passed for months against whatever happened to be listening on
    :18004 — a real agent on the developer's laptop made them green, and its absence would have
    made them red for a reason that has nothing to do with the launcher. Stub the probe.
    """
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    from examlops.cli import _client

    def fake_get(url, token=""):
        if not reachable:
            raise _client.ClientError("Connection refused")
        return {
            "backend": "ollama",
            "model": "llama3.1:8b",
            "ok": True,
            "memory": {"enabled": True, "active": True},
        }

    monkeypatch.setattr(_client, "get", fake_get)


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


def test_an_agent_that_is_not_running_is_named_before_the_repl_opens(monkeypatch, tmp_path):
    """kq answers a refused connection with an offline REPL and three retries per message.

    Mohsen ran `exa chat` against an agent that was never started, met a Kubernetes banner,
    typed one question and waited out four timeouts before learning nothing was listening. The
    launcher knows which agent it meant, so it says so first and does not open the client.
    """
    _isolate(monkeypatch, tmp_path, reachable=False)
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: "/fake/kq")
    launched = []
    monkeypatch.setattr("subprocess.call", lambda argv: launched.append(argv) or 0)

    result = runner.invoke(app, ["chat"])

    assert not launched, "opened a chat client against an agent that is not there"
    assert "Could not reach the Skipper agent" in result.output
    assert "make skipper-server" in result.output
    assert result.exit_code != 0


def test_the_client_is_dressed_as_examlops_not_as_a_kubernetes_copilot(monkeypatch, tmp_path):
    """kq is adopted unforked and greets you as "your AI co-pilot for Kubernetes".

    Correct for the client, wrong for an operator asking Skipper about drift and HPC jobs. The
    launcher supplies the identity instead of forking the client.
    """
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: "/fake/kq")
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr("subprocess.call", lambda argv: seen.setdefault("argv", argv) and 0)

    result = runner.invoke(app, ["chat"])

    argv = seen["argv"]
    assert argv[argv.index("--agent-name") + 1] == "Skipper"
    assert "--no-banner" in argv
    # and the launcher says what kq's banner cannot: which backend actually answered
    assert "Skipper" in result.output
    assert "llama3.1:8b" in result.output


def test_the_caller_can_override_the_identity(monkeypatch, tmp_path):
    """Anything after `--` wins; the defaults must not be appended twice."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: "/fake/kq")
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr("subprocess.call", lambda argv: seen.setdefault("argv", argv) and 0)

    runner.invoke(app, ["chat", "--", "--agent-name", "Bob"])

    argv = seen["argv"]
    assert argv.count("--agent-name") == 1
    assert argv[argv.index("--agent-name") + 1] == "Bob"
