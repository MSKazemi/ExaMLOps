"""Guard: `exa --help` groups every command into a titled panel, in a deliberate order.

These tests fail loudly when a new top-level command (or subcommand of the six large
groups) is added without being assigned to a help panel — which would silently drop it into
the default "Commands" box and undo the grouped-help UX. See ``examlops.cli._help``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli._help import _effective_name  # noqa: E402
from examlops.cli.commands import (  # noqa: E402
    drift,
    hpc_cmd,
    models,
    pipeline,
    project_cmd,
    serve,
)
from examlops.cli.main import _ROOT_PANELS, app  # noqa: E402

runner = CliRunner()


def _registered_names(typer_app) -> set[str]:
    return {
        _effective_name(info)
        for info in [*typer_app.registered_commands, *typer_app.registered_groups]
    }


def _spec_names(panels) -> list[str]:
    return [name for _, names in panels for name in names]


def test_root_every_command_is_paneled():
    # Every registered top-level command/group is covered by exactly one panel in the spec,
    # and the spec has no stale entries. (No CLI plugins are installed in CI.)
    registered = _registered_names(app)
    spec = _spec_names(_ROOT_PANELS)
    assert len(spec) == len(set(spec)), "duplicate command name in _ROOT_PANELS"
    assert registered == set(spec), (
        f"paneling drift — only in registry: {registered - set(spec)}; "
        f"only in spec: {set(spec) - registered}"
    )


def test_root_panels_render_in_order():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    positions = [result.output.find(title) for title, _ in _ROOT_PANELS]
    assert all(p >= 0 for p in positions), (
        f"missing panel title(s): {[t for (t, _), p in zip(_ROOT_PANELS, positions) if p < 0]}"
    )
    assert positions == sorted(positions), "root help panels are out of order"
    # No leftover default panel: every command lives in a named panel.
    assert result.output.count("─ Commands ─") == 0


def test_root_all_commands_present_in_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for name in _spec_names(_ROOT_PANELS):
        assert name in result.output, f"{name} missing from root help"


SUBGROUPS = [
    ("serve", serve),
    ("models", models),
    ("pipeline", pipeline),
    ("hpc", hpc_cmd),
    ("drift", drift),
    ("project", project_cmd),
]


def test_subgroups_every_command_is_paneled():
    # main.py assigns panels to these apps after all sub-typers are attached, so importing
    # examlops.cli.main (done above) is what wires them up.
    for label, mod in SUBGROUPS:
        registered = _registered_names(mod.app)
        spec = _spec_names(mod._PANELS)
        assert len(spec) == len(set(spec)), f"duplicate command name in {label} _PANELS"
        assert registered == set(spec), (
            f"{label}: paneling drift — only in registry: {registered - set(spec)}; "
            f"only in spec: {set(spec) - registered}"
        )


def test_subgroups_panels_render_in_order():
    for label, mod in SUBGROUPS:
        result = runner.invoke(app, [label, "--help"])
        assert result.exit_code == 0, result.output
        positions = [result.output.find(title) for title, _ in mod._PANELS]
        assert all(p >= 0 for p in positions), f"{label}: missing panel title(s)"
        assert positions == sorted(positions), f"{label}: help panels out of order"
        assert result.output.count("─ Commands ─") == 0, f"{label}: leftover default panel"


def test_fuzzy_suggest_still_works():
    # The ordered root group extends SuggestGroup — 'did you mean' must survive paneling.
    result = runner.invoke(app, ["modls"])
    assert result.exit_code != 0
    assert "did you mean" in result.output.lower()


def test_the_agentic_surface_is_findable_under_one_title():
    """`exa --help` must name the agent category, not merely contain its commands.

    The commands were always there — `ask`, `agentops`, `autopilot`, `mcp` — but spread over
    four panels whose titles were Getting Started, GenAI & LLMOps, Monitoring & Quality and
    Platform & Integrations. A reader looking for "the agentic parts" found no such words and
    reported the surface as missing. Nothing was missing; the category had no name, which reads
    the same as absence. This pins the name.
    """
    panels = dict(_ROOT_PANELS)
    assert "Agents & Automation" in panels, "the agent category lost its own panel title"
    assert {"ask", "agentops", "autopilot", "mcp"} <= set(panels["Agents & Automation"])

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Agents & Automation" in result.output


def test_explain_stays_out_of_the_agent_panel():
    """`exa explain` introspects the Click tree and calls no agent.

    Filing it under Agents & Automation would make the panel title untrue, and would send a
    reader debugging an unreachable agent to a command that never needed one.
    """
    panels = dict(_ROOT_PANELS)
    assert "explain" in panels["Getting Started"]
    assert "explain" not in panels["Agents & Automation"]
