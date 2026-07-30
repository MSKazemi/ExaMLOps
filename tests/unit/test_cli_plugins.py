from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

import typer  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from examlops.cli import _plugins  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


class _FakeEP:
    def __init__(self, name, value, loader):
        self.name = name
        self.value = value
        self._loader = loader

    def load(self):
        return self._loader()


def test_discover_loads_good_plugin(monkeypatch):
    plugin_app = typer.Typer()

    monkeypatch.setattr(
        _plugins,
        "_entry_points",
        lambda: [_FakeEP("myteam", "my_pkg.cli:app", lambda: plugin_app)],
    )
    infos = _plugins.discover()
    assert len(infos) == 1
    assert infos[0].ok and infos[0].obj is plugin_app


def test_discover_captures_broken_plugin(monkeypatch):
    def boom():
        raise ImportError("no module named foo")

    monkeypatch.setattr(_plugins, "_entry_points", lambda: [_FakeEP("bad", "broken:app", boom)])
    infos = _plugins.discover()
    assert infos[0].ok is False
    assert "no module named foo" in infos[0].error


def test_register_attaches_typer_and_rejects_non_typer(monkeypatch):
    good = typer.Typer()
    monkeypatch.setattr(
        _plugins,
        "_entry_points",
        lambda: [
            _FakeEP("good", "pkg:app", lambda: good),
            _FakeEP("notyper", "pkg:thing", lambda: object()),
        ],
    )
    target = typer.Typer()
    infos = _plugins.register(target)
    by_name = {i.name: i for i in infos}
    assert by_name["good"].ok is True
    assert by_name["notyper"].ok is False
    assert "not a typer.Typer" in by_name["notyper"].error


def test_plugins_command_empty(monkeypatch):
    monkeypatch.setattr(_plugins, "_entry_points", lambda: [])
    result = runner.invoke(app, ["plugins"])
    assert result.exit_code == 0, result.output
    assert "No plugins installed" in result.output
    # Regression (2026-07-30): the "add a plugin" hint embeds a literal TOML
    # `[project.entry-points."examlops.cli_plugins"]` snippet. Rich would otherwise
    # eat the `[...]` as an invalid markup tag and render nothing ("Add one via  in
    # a package."). `_output.hint` now escapes the message, so the snippet appears.
    assert '[project.entry-points."examlops.cli_plugins"]' in result.output


def test_plugins_command_json(monkeypatch):
    monkeypatch.setattr(
        _plugins,
        "_entry_points",
        lambda: [_FakeEP("myteam", "my_pkg.cli:app", lambda: typer.Typer())],
    )
    result = runner.invoke(app, ["--json", "plugins"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["name"] == "myteam"
    assert payload[0]["ok"] is True


def test_registered_plugin_subcommand_is_callable(monkeypatch):
    # A plugin that adds `exa demo hello` should be invocable through the root app.
    plugin_app = typer.Typer()

    @plugin_app.command()
    def hello():
        typer.echo("hello-from-plugin")

    monkeypatch.setattr(
        _plugins, "_entry_points", lambda: [_FakeEP("demo", "pkg:app", lambda: plugin_app)]
    )
    # Rebuild a fresh root app with the patched discovery so the plugin attaches.
    import importlib

    import examlops.cli.main as main_mod

    importlib.reload(main_mod)
    result = runner.invoke(main_mod.app, ["demo", "hello"])
    assert result.exit_code == 0, result.output
    assert "hello-from-plugin" in result.output
