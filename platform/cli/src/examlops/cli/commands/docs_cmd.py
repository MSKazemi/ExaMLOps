"""``exa docs`` — generate the full command reference from the live CLI tree.

Walks the actual Typer/Click command tree and emits Markdown (or JSON), so the reference can
never drift from the implementation. Handy for contributors and for feeding an LLM a complete,
accurate description of everything the CLI can do.
"""

from __future__ import annotations

import re
from typing import Any

import click
import typer

from examlops.cli import _output

_EXAMPLES = (
    "Examples:\n\n"
    "  [dim]# Print the full command reference as Markdown[/dim]\n"
    "  exa docs\n\n"
    "  [dim]# Write it to a file[/dim]\n"
    "  exa docs --out docs/reference/cli-generated.md\n\n"
    "  [dim]# Machine-readable command tree[/dim]\n"
    "  exa --json docs"
)

_MARKUP = re.compile(r"\[/?[a-z ]+\]")

# Framework-auto options that add noise to a hand-read reference (they repeat on every
# command). Kept out of the generated docs; real features like ``--version`` stay in.
_META_OPTS = {"--help", "--install-completion", "--show-completion"}


def _clean(text: str) -> str:
    return _MARKUP.sub("", text or "").strip()


def _root_group() -> click.Command:
    import typer.main

    from examlops.cli.main import app

    return typer.main.get_command(app)


def _walk(cmd: click.Command, path: list[str]) -> dict[str, Any]:
    node: dict[str, Any] = {
        "name": " ".join(path),
        "help": _clean(cmd.help or cmd.get_short_help_str() or ""),
    }
    ctx = click.Context(cmd, info_name=path[-1])
    options = []
    for param in cmd.get_params(ctx):
        # NB: don't use ``isinstance(param, click.Option)`` — Typer's ``TyperOption``
        # subclasses ``click.Parameter`` (not ``click.Option``) in current Typer/Click,
        # so an isinstance check silently drops every flag from the reference. Filter on
        # the version-stable ``param_type_name`` discriminator instead.
        if getattr(param, "param_type_name", None) != "option":
            continue
        if getattr(param, "hidden", False):
            continue
        opts = list(param.opts)
        # Skip framework-auto meta options — noise repeated on every command.
        if any(o in _META_OPTS for o in opts):
            continue
        options.append(
            {
                "opts": ", ".join(opts),
                "help": _clean(getattr(param, "help", "") or ""),
            }
        )
    if options:
        node["options"] = options

    subcommands = getattr(cmd, "commands", None)
    if subcommands:
        node["subcommands"] = [
            _walk(sub, [*path, name]) for name, sub in sorted(subcommands.items())
        ]
    return node


def _render_md(node: dict[str, Any], level: int = 1) -> list[str]:
    lines = [f"{'#' * min(level, 6)} `{node['name']}`", ""]
    if node.get("help"):
        lines += [node["help"], ""]
    for opt in node.get("options", []):
        suffix = f" — {opt['help']}" if opt["help"] else ""
        lines.append(f"- `{opt['opts']}`{suffix}")
    if node.get("options"):
        lines.append("")
    for sub in node.get("subcommands", []):
        lines += _render_md(sub, level + 1)
    return lines


def docs(
    out: str = typer.Option("", "--out", help="Write Markdown to this file instead of stdout"),
) -> None:
    """Generate the full command reference from the live CLI tree."""
    tree = _walk(_root_group(), ["exa"])

    if _output.json_mode:
        _output.print_json(tree)
        return

    markdown = "\n".join(_render_md(tree)).rstrip() + "\n"
    if out:
        from pathlib import Path

        path = Path(out)
        try:
            if path.parent and not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown)
        except OSError as e:
            _output.error(f"Could not write command reference to {out}: {e}")
            return
        _output.ok(f"Wrote command reference to {out}")
        return
    # Print raw so it can be piped/redirected without Rich styling.
    print(markdown)
