"""The CLI side of the site feature profile (ADR 0128).

A root command whose module is disabled at this site is *absent* from ``exa --help`` and from
every tree walk that lists commands (``exa docs``, the dashboard's CLI Console catalog), and
invoking it by name explains which module owns it and how to enable it — instead of an
"unknown command" that would read as a broken install. Commands no module owns (third-party
plugins) are always available: the site profile governs the platform's own surface.

The profile is resolved on each lookup rather than cached, so ``exa modules enable X`` takes
effect on the very next command. It is two environment reads and one small TOML file.
"""

from __future__ import annotations

from typing import Any

from typer.core import TyperCommand

from examlops.cli._help import SuggestGroup

# Exit code for "this feature is switched off here" — distinct from 1 (the command failed) so a
# script can tell a disabled module from a real error.
EXIT_MODULE_DISABLED = 3


def _profile() -> Any | None:
    try:
        from examlops.lifecycle.modules import resolve

        return resolve()
    except Exception:  # noqa: BLE001 — a broken profile must never take the CLI down with it
        return None


def disabled_module(name: str) -> str | None:
    """The module that disables root command ``name`` at this site, or ``None`` if it is on."""
    from examlops.lifecycle.modules import module_for_command

    owner = module_for_command(name)
    if owner is None:
        return None
    profile = _profile()
    if profile is None or profile.is_enabled(owner):
        return None
    return owner


def _disabled_stub(name: str, owner: str) -> Any:
    """A hidden command that refuses, whatever arguments follow it.

    Built as a `TyperCommand`, not a `click.Command`: from typer 0.27 the group runs on typer's
    vendored click (`typer._click`), and a stub from the `click` package would belong to another
    class hierarchy than the group invoking it. `TyperCommand` always matches the group. The
    extra arguments need no parameter to absorb them: `ignore_unknown_options` and
    `allow_extra_args` leave them unparsed.
    """

    def _refuse() -> None:
        from examlops.cli import _output
        from examlops.lifecycle.modules import module

        _output.error(
            f"`exa {name}` belongs to the '{owner}' module ({module(owner).title}), which is "
            "disabled at this site.",
            exit_code=EXIT_MODULE_DISABLED,
            hint=f"exa modules enable {owner}   ·   exa modules list",
        )

    return TyperCommand(
        name,
        callback=_refuse,
        help=f"Disabled at this site (module '{owner}').",
        hidden=True,
        add_help_option=False,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )


class ModuleGatedGroup(SuggestGroup):
    """Root group that hides and refuses commands of modules disabled by the site profile.

    The context and command types are `Any` because typer >= 0.27 passes its vendored click's
    classes (see `_help.SuggestGroup`).
    """

    def list_commands(self, ctx: Any) -> list[str]:
        names = super().list_commands(ctx)
        return [n for n in names if disabled_module(n) is None]

    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        cmd = super().get_command(ctx, cmd_name)
        if cmd is None:
            return None
        owner = disabled_module(cmd_name)
        if owner is not None:
            return _disabled_stub(cmd_name, owner)
        from examlops.cli._policy_hook import install  # ADR 0079 d2: every mutation decides

        return install(cmd, cmd_name)
