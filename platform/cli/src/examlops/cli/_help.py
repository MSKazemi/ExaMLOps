"""Grouped, best-practice ``exa`` help — command panels + ordering.

The ``exa`` CLI has grown to ~60 top-level commands. A single flat "Commands" list is hard
to scan, so we split commands into titled panels (Typer's ``rich_help_panel``) grouped by
MLOps concern and render them in a deliberate, lifecycle-ordered sequence.

Two moving parts, both reused by the root app and the large sub-groups:

* :func:`make_ordered_group` — a ``TyperGroup`` subclass factory whose ``list_commands``
  yields command names ordered by *panel* first, then by original registration order.
  Typer builds its help panels by iterating ``group.list_commands(ctx)`` and rendering
  panels in first-seen order, so controlling that iteration order controls the panel order.
  Registration order alone can't: Typer lists all leaf commands before groups, and most
  panels are group-only, so a group-only panel could never precede a leaf-containing one.

* :func:`assign_panels` — sets ``.rich_help_panel`` on every registered command/group from a
  single declarative ``(title, [names])`` spec, so panels are declared in one place rather
  than sprinkled across ~60 registration call sites.

The spec is the single source of truth: to add a command to a panel, add its name to the
right list. ``tests/unit/test_cli_help_panels.py`` fails loudly if a command is left out.
"""

from __future__ import annotations

import difflib
from typing import Any

import click
import typer
from typer.core import TyperGroup
from typer.main import get_command_name

# Panel spec: an ordered list of (panel_title, [command_names]). Ordered => display order.
PanelSpec = list[tuple[str, list[str]]]

# typer >= 0.27 vendors click as `typer._click`, so a Typer group raises THAT module's
# UsageError and takes THAT module's Context — unrelated to the `click` package's classes.
# Catch both, and type the click objects in the overrides below as `Any`, so the fallback keeps
# working and type-checks whichever typer is installed.
_vendored_click = getattr(typer, "_click", None)
_USAGE_ERRORS: tuple[type[Exception], ...] = (click.UsageError,) + (
    (_vendored_click.exceptions.UsageError,) if _vendored_click is not None else ()
)


def _effective_name(info: object) -> str:
    """The command/group name Typer will render.

    A ``@app.command()`` without an explicit name leaves ``CommandInfo.name`` as ``None``
    until Typer builds the click command, deriving it from the callback (``func_name`` →
    ``func-name``). Resolve it the same way so panel assignment matches the rendered name.
    """
    name = getattr(info, "name", None)
    if name:
        return name
    callback = getattr(info, "callback", None)
    if callback is not None and getattr(callback, "__name__", None):
        return get_command_name(callback.__name__)
    return ""


class SuggestGroup(TyperGroup):
    """Typer group that adds fuzzy 'Did you mean …' suggestions on unknown commands.

    Modern Click (>=8.2) already suggests near-misses; this is a graceful fallback for
    older Click and never doubles up Click's own suggestion.
    """

    def resolve_command(self, ctx: Any, args: list[str]) -> tuple[str | None, Any, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except _USAGE_ERRORS as exc:
            message = getattr(exc, "message", "") or ""
            if "did you mean" not in message.lower():
                typed = args[0] if args else ""
                matches = difflib.get_close_matches(typed, self.list_commands(ctx), n=3, cutoff=0.5)
                if matches:
                    hint = ", ".join(repr(m) for m in matches)
                    exc.message = f"{message} Did you mean {hint}?"  # type: ignore[attr-defined]
            raise


def make_ordered_group(
    panels: PanelSpec, *, base: type[TyperGroup] = TyperGroup
) -> type[TyperGroup]:
    """Return a ``TyperGroup`` subclass that orders help by the ``panels`` spec.

    ``list_commands`` is overridden to yield names in the exact order they appear in
    ``panels`` (panel order, then within-panel order). Because Typer builds help panels by
    iterating ``list_commands`` and renders them in first-seen order, this controls both the
    panel order and the order of commands inside each panel from a single declarative spec.
    Commands not in the spec (e.g. third-party plugins) sort last, preserving their original
    order. Only help *display* order changes — command resolution, ``exa docs`` (which sorts
    independently), and scripting are unaffected.
    """
    flat_order = {name: i for i, name in enumerate(n for _, names in panels for n in names)}
    unmapped = len(flat_order)

    class _OrderedGroup(base):  # type: ignore[valid-type, misc]
        def list_commands(self, ctx: Any) -> list[str]:
            names = super().list_commands(ctx)
            original = {name: i for i, name in enumerate(names)}
            return sorted(names, key=lambda n: (flat_order.get(n, unmapped), original[n]))

    _OrderedGroup.__name__ = f"Ordered{base.__name__}"
    _OrderedGroup.__qualname__ = _OrderedGroup.__name__
    return _OrderedGroup


def assign_panels(app: object, panels: PanelSpec) -> None:
    """Set ``rich_help_panel`` on each registered command/group of ``app`` from ``panels``.

    ``app`` is a ``typer.Typer`` instance. Idempotent and resilient: names in the spec that
    aren't registered are skipped silently (a command may be conditionally registered), and
    registered names absent from the spec are simply left unpaneled (the test guard catches
    those so nothing regresses into the default panel unnoticed).
    """
    name_to_panel: dict[str, str] = {}
    for title, names in panels:
        for name in names:
            name_to_panel[name] = title

    for info in [*app.registered_commands, *app.registered_groups]:  # type: ignore[attr-defined]
        panel = name_to_panel.get(_effective_name(info))
        if panel is not None:
            info.rich_help_panel = panel
