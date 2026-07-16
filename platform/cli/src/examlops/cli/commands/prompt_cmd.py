"""``exa prompt`` — prompt registry: versioned templates + labels (Next-Gen 40 · B1, ADR 0009).

Immutable versions, moving labels (dev/staging/prod), audited label moves, rollback.
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.platform_db import (
    create_prompt_version,
    get_prompt_by_label,
    get_prompt_version,
    list_prompt_labels,
    list_prompt_names,
    list_prompt_versions,
    set_prompt_label,
    write_audit_event,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EX_CREATE = (
    "Examples:\n\n"
    '  exa prompt create triage --template "Classify: {text}"\n\n'
    '  exa prompt create triage --template "Classify: {text}" --label prod'
)
_EX_LABEL = "Examples:\n\n  exa prompt label triage prod 3"
_EX_ROLLBACK = "Examples:\n\n  exa prompt rollback triage prod 2"
_EX_DIFF = "Examples:\n\n  exa prompt diff triage 2 3"


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"


@app.command("create", epilog=_EX_CREATE)
def create(
    name: str = typer.Argument(..., help="Prompt name"),
    template: str = typer.Option(..., "--template", "-t", help="Prompt template with {vars}"),
    label: str | None = typer.Option(
        None, "--label", "-l", help="Also point this label at the new version"
    ),
) -> None:
    """Create a new immutable prompt version (spec R1)."""
    from examlops.prompts import declared_variables

    variables = declared_variables(template)
    version = create_prompt_version(name, template, variables=variables, actor=_actor())
    write_audit_event(
        "exa-prompt", _actor(), "prompt_create", name, {"version": version, "variables": variables}
    )
    if label:
        set_prompt_label(name, label, version)
        write_audit_event(
            "exa-prompt", _actor(), "prompt_label", name, {"label": label, "version": version}
        )
    if _output.json_mode:
        _output.print_json(
            {"name": name, "version": version, "variables": variables, "label": label}
        )
        return
    _output.ok(f"Created [bold]{name}[/bold] v{version} (vars: {', '.join(variables) or 'none'})")
    if label:
        _output.info(f"Label '{label}' → v{version}")


@app.command("list", epilog="Examples:\n\n  exa prompt list\n\n  exa prompt list triage")
def list_prompts(
    name: str | None = typer.Argument(None, help="Show versions of one prompt (else list names)"),
) -> None:
    """List prompt names, or the versions + labels of one prompt."""
    if name is None:
        names = list_prompt_names()
        if _output.json_mode:
            _output.print_json(names)
        elif not names:
            _output.info("No prompts registered.")
        else:
            _output.print_table("Prompts", ["Name"], [[n] for n in names])
        return
    versions = list_prompt_versions(name)
    labels = {r["label"]: r["version"] for r in list_prompt_labels(name)}
    label_of = {v: [] for v in {x["version"] for x in versions}}
    for lab, ver in labels.items():
        label_of.setdefault(ver, []).append(lab)
    if _output.json_mode:
        _output.print_json({"versions": versions, "labels": labels})
        return
    _output.print_table(
        f"Prompt {name}",
        ["Version", "Labels", "Variables", "Created"],
        [
            [
                str(v["version"]),
                ", ".join(label_of.get(v["version"], [])) or "-",
                v["variables"] or "[]",
                (v["created_at"] or "")[:19],
            ]
            for v in versions
        ],
    )


@app.command(
    "show", epilog="Examples:\n\n  exa prompt show triage 3\n\n  exa prompt show triage@prod"
)
def show(
    target: str = typer.Argument(..., help="name@label or 'name <version>'"),
    version: int | None = typer.Argument(None),
) -> None:
    """Show a prompt version's template (by version or name@label)."""
    if "@" in target and version is None:
        name, label = target.split("@", 1)
        row = get_prompt_by_label(name, label)
    else:
        name = target
        if version is None:
            _output.error("Provide a version number or use name@label.")
        row = get_prompt_version(name, int(version))
    if row is None:
        _output.error(f"Prompt '{target}' not found.")
    if _output.json_mode:
        _output.print_json(row)
        return
    _output.print_record(row)


@app.command("diff", epilog=_EX_DIFF)
def diff(
    name: str = typer.Argument(..., help="Prompt name"),
    ver_a: int = typer.Argument(..., help="Baseline version"),
    ver_b: int = typer.Argument(..., help="Comparison version"),
) -> None:
    """Show a line diff between two prompt versions (spec R3)."""
    import difflib

    a = get_prompt_version(name, ver_a)
    b = get_prompt_version(name, ver_b)
    if a is None or b is None:
        _output.error(f"Both versions must exist for {name}.")
    lines = list(
        difflib.unified_diff(
            a["template"].splitlines(),
            b["template"].splitlines(),
            fromfile=f"{name}@v{ver_a}",
            tofile=f"{name}@v{ver_b}",
            lineterm="",
        )
    )
    if _output.json_mode:
        _output.print_json({"diff": lines})
        return
    if not lines:
        _output.info("Templates are identical.")
        return
    for line in lines:
        _output.detail(line)


@app.command("label", epilog=_EX_LABEL)
def label(
    name: str = typer.Argument(..., help="Prompt name"),
    label_name: str = typer.Argument(..., metavar="LABEL", help="Label (dev/staging/prod/…)"),
    version: int = typer.Argument(..., help="Version to point the label at"),
) -> None:
    """Move a label to a version — audited (spec R8/R9)."""
    if get_prompt_version(name, version) is None:
        _output.error(f"{name} v{version} does not exist.")
    set_prompt_label(name, label_name, version)
    write_audit_event(
        "exa-prompt", _actor(), "prompt_label", name, {"label": label_name, "version": version}
    )
    _output.ok(f"[bold]{name}[/bold]@{label_name} → v{version}")


@app.command("rollback", epilog=_EX_ROLLBACK)
def rollback(
    name: str = typer.Argument(..., help="Prompt name"),
    label_name: str = typer.Argument(..., metavar="LABEL", help="Label to roll back"),
    to_version: int = typer.Argument(..., help="Prior version to point the label back at"),
) -> None:
    """Roll a label back to a prior version without deleting history (spec R10)."""
    if get_prompt_version(name, to_version) is None:
        _output.error(f"{name} v{to_version} does not exist.")
    set_prompt_label(name, label_name, to_version)
    write_audit_event(
        "exa-prompt",
        _actor(),
        "prompt_rollback",
        name,
        {"label": label_name, "to_version": to_version},
    )
    _output.ok(f"Rolled back [bold]{name}[/bold]@{label_name} → v{to_version} (history intact)")
