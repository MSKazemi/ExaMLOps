"""``exa explain`` — plain-language help for any command, with examples.

A discoverability aid: ``exa explain`` lists the top-level commands with one-line
descriptions; ``exa explain <command> [subcommand …]`` prints what a command does plus its
copy-paste examples — without the visual noise of full ``--help`` output.
"""

from __future__ import annotations

import re

import click
import typer

from examlops.cli import _output

_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# List every top-level command[/dim]\n"
    "  exa explain\n\n"
    "  [dim]# Explain a command[/dim]\n"
    "  exa explain drift\n\n"
    "  [dim]# Explain a nested subcommand[/dim]\n"
    "  exa explain serve reload"
)

# Rich markup used in epilog example strings — strip it for clean plain output.
_MARKUP = re.compile(r"\[/?[a-z ]+\]")


def _root_group() -> click.Command:
    """Return the Click group for the whole `exa` app (lazy to avoid import cycles)."""
    import typer.main

    from examlops.cli.main import app

    return typer.main.get_command(app)


def _normalize(path: list[str]) -> list[str]:
    """Accept the command the way a person actually writes it.

    ``exa explain exa status`` and ``explain_command("exa status")`` used to answer *"unknown
    command"* about a command that plainly exists — the leading ``exa`` was matched as if it were
    a subcommand name. The MCP tool made it worse by echoing the canonical form back as
    ``"command": "exa status"``: the string it prints was the string it rejects, so an agent
    following its own output got an error. Options are dropped for the same reason — ``--help``
    is not a command name, so ``exa --help`` means "list the top-level commands".
    """
    parts = [p for p in path if p and not p.startswith("-")]
    if parts and parts[0] == "exa":
        parts = parts[1:]
    return parts


def _resolve(path: list[str]) -> click.Command | None:
    node: click.Command | None = _root_group()
    for part in _normalize(path):
        commands = getattr(node, "commands", None)
        if not commands or part not in commands:
            return None
        node = commands[part]
    return node


def _clean(text: str) -> str:
    return _MARKUP.sub("", text).strip()


def explain(
    command: list[str] = typer.Argument(
        None, help="Command path to explain, e.g. 'drift' or 'serve reload'. Omit to list all."
    ),
) -> None:
    """Explain what a command does, in plain language, with examples."""
    path = _normalize(list(command or []))
    node = _resolve(path)

    if node is None:
        _output.error(
            f"Unknown command: {' '.join(path)!r}",
            hint="Run 'exa explain' to list all commands.",
        )
        return

    commands = getattr(node, "commands", None)
    label = " ".join(["exa", *path]) if path else "exa"

    # A group: list its subcommands.
    if commands:
        rows = [
            [name, _clean(sub.get_short_help_str() or (sub.help or "").split("\n")[0])]
            for name, sub in sorted(commands.items())
        ]
        if _output.json_mode:
            _output.print_json(
                {"command": label, "subcommands": [{"name": r[0], "summary": r[1]} for r in rows]}
            )
            return
        _output.print_table(f"{label} — subcommands", ["Command", "What it does"], rows)
        _output.hint(f"Explain one with: exa explain {' '.join([*path, '<name>']).strip()}")
        return

    # A leaf command: describe it and show its examples.
    help_text = _clean(node.help or node.get_short_help_str() or "")
    examples = _extract_examples(getattr(node, "epilog", None))
    if _output.json_mode:
        _output.print_json({"command": label, "description": help_text, "examples": examples})
        return
    _output.console.print(f"[bold cyan]{label}[/bold cyan]")
    if help_text:
        _output.console.print(f"  {help_text}")
    if examples:
        _output.console.print("\n[bold]Examples[/bold]")
        for ex in examples:
            _output.console.print(f"  [green]{ex}[/green]")
    _output.hint(f"Full options: exa {' '.join(path)} --help")


def _extract_examples(epilog: object) -> list[str]:
    if not epilog:
        return []
    lines = [_clean(line) for line in str(epilog).splitlines()]
    return [ln for ln in lines if ln.startswith("exa ")]
