"""``exa prompt`` — prompt registry: versioned templates + labels (Next-Gen 40 · B1, ADR 0009).

Immutable versions, moving labels (dev/staging/prod), audited label moves, rollback.
"""

from __future__ import annotations

import os
from typing import Any

import typer

from examlops.cli import _output
from examlops.data.audit import write_audit_event
from examlops.data.prompts import (
    create_prompt_version,
    get_prompt_by_label,
    get_prompt_version,
    list_prompt_labels,
    list_prompt_names,
    list_prompt_versions,
    set_prompt_label,
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
    _output.ok(f"Created {name} v{version} (vars: {', '.join(variables) or 'none'})")
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
    label_of: dict[Any, list[str]] = {v: [] for v in {x["version"] for x in versions}}
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


# ── C3 regression gate on label moves (ADR 0009 clause 4) ────────────────────
#
# The clause says a label move "can be gated by the eval regression check (C3) **exactly like
# model promotion**", so this reuses `run_eval_gate` rather than growing a second gate.
#
# **The subject is `prompt:<name>`, not `<name>`.** `eval_gates` is keyed by a free-form string
# shared with models, so a prompt called `jpcp` would otherwise inherit the *model* jpcp's gate
# and be judged against scores that are not about it — a gate that fires on the wrong evidence
# is worse than no gate. Configure one with
# `exa eval gate set prompt:<name> --suite <suite> --metric …`.
#
# **Only the labels in `EXAMLOPS_PROMPT_GATE_LABELS` are gated (default `prod`).** Gating every
# label would deadlock the registry: the gate reads its baseline from a *labelled* version, so
# with `dev` gated there is no way to establish the baseline the gate needs. `dev`/`staging` are
# where a candidate is staged in order to be evaluated; `prod` is the move that changes what
# callers get, and is the analogue of the model alias promotion this clause points at.
_DEFAULT_GATED_LABELS = "prod"


def _gated_labels() -> set[str]:
    raw = os.getenv("EXAMLOPS_PROMPT_GATE_LABELS", _DEFAULT_GATED_LABELS)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _gate_label_move(name: str, label_name: str, version: int, *, force: bool) -> None:
    """Refuse a gated label move when the C3 gate fails. No configured gate ⇒ a no-op."""
    if label_name.strip().lower() not in _gated_labels():
        return
    from examlops.evaluation.gate import run_eval_gate

    subject = f"prompt:{name}"
    try:
        result = run_eval_gate(subject, str(version))
    except Exception:  # noqa: BLE001 - a broken gate must not strand a prompt release
        return
    if result is None or result.passed:
        return
    failing = [m.name for m in result.metrics if m.failed]
    detail = {"label": label_name, "version": version, "failing_metrics": failing}
    if not force:
        write_audit_event("exa-prompt", _actor(), "prompt_label_blocked_by_gate", name, detail)
        _output.error(
            f"Eval gate FAILED for {name} v{version}: {', '.join(failing)}. "
            "Use --force to override (audited).",
        )
    write_audit_event(
        "exa-prompt", _actor(), "eval_gate_override", name, {**detail, "forced": True}
    )
    _output.warning(f"Eval gate FAILED but --force set; overriding: {', '.join(failing)}")


def _emit_label_lineage(name: str, label_name: str, version: int, *, rollback: bool) -> None:
    """A2 lineage for a prompt label move (ADR 0009 clause 5). Fail-open.

    **Emitted on the label move, not per request.** A prompt version is an input to every
    gateway call that resolves it, and emitting there would put one lineage event on the graph
    per inference — the per-request lineage the platform deliberately does not do (see
    `docs/guides/lineage.md`). A label move is the *release*: low-volume, decision-shaped, and
    the thing an operator asks about when a prompt changed what production says. It mirrors the
    promotion event an alias move already emits for models.
    """
    try:
        from examlops.lineage import deployment_node, emit_lineage, prompt_node

        emit_lineage(
            "COMPLETE",
            job=f"prompt-label:{name}",
            run_id=f"prompt-{name}-{label_name}-v{version}",
            inputs=[prompt_node(name, version)],
            outputs=[deployment_node(f"prompt/{name}@{label_name}")],
            facets={"label": label_name, "rollback": rollback},
        )
    except Exception:  # noqa: BLE001 - the label has moved; bookkeeping must not report failure
        pass


@app.command("label", epilog=_EX_LABEL)
def label(
    name: str = typer.Argument(..., help="Prompt name"),
    label_name: str = typer.Argument(..., metavar="LABEL", help="Label (dev/staging/prod/…)"),
    version: int = typer.Argument(..., help="Version to point the label at"),
    force: bool = typer.Option(
        False, "--force", help="Move the label even if the C3 eval gate fails (audited)"
    ),
) -> None:
    """Move a label to a version — audited (spec R8/R9) and C3-gated (ADR 0009 clause 4)."""
    if get_prompt_version(name, version) is None:
        _output.error(f"{name} v{version} does not exist.")
    _gate_label_move(name, label_name, version, force=force)
    set_prompt_label(name, label_name, version)
    _emit_label_lineage(name, label_name, version, rollback=False)
    write_audit_event(
        "exa-prompt", _actor(), "prompt_label", name, {"label": label_name, "version": version}
    )
    _output.ok(f"{name}@{label_name} → v{version}")


@app.command("rollback", epilog=_EX_ROLLBACK)
def rollback(
    name: str = typer.Argument(..., help="Prompt name"),
    label_name: str = typer.Argument(..., metavar="LABEL", help="Label to roll back"),
    to_version: int = typer.Argument(..., help="Prior version to point the label back at"),
) -> None:
    """Roll a label back to a prior version without deleting history (spec R10).

    **Deliberately not gated by C3.** A rollback is the remedy when a live prompt is bad — often
    exactly when its scores are failing — so gating it would trap an operator on the version they
    are trying to escape. Moving *forward* is what the gate exists to hold.
    """
    if get_prompt_version(name, to_version) is None:
        _output.error(f"{name} v{to_version} does not exist.")
    set_prompt_label(name, label_name, to_version)
    _emit_label_lineage(name, label_name, to_version, rollback=True)
    write_audit_event(
        "exa-prompt",
        _actor(),
        "prompt_rollback",
        name,
        {"label": label_name, "to_version": to_version},
    )
    _output.ok(f"Rolled back {name}@{label_name} → v{to_version} (history intact)")


_EX_MIGRATE = (
    "Examples:\n\n"
    "  exa prompt migrate --to mlflow --dry-run     # what would move\n\n"
    "  exa prompt migrate --to mlflow               # copy versions + labels into MLflow\n\n"
    "  EXAMLOPS_PROMPT_BACKEND=mlflow exa prompt list"
)


@app.command("backend", epilog=_EX_MIGRATE)
def backend() -> None:
    """Show which registry holds prompts: platform_db (default) or the MLflow Prompt Registry."""
    from examlops.data.prompts import prompt_backend

    try:
        name = prompt_backend()
    except ValueError as exc:
        _output.error(str(exc))
    where = (
        os.getenv("EXAMLOPS_PROMPT_MLFLOW_URI") or os.getenv("MLFLOW_TRACKING_URI") or "(unset)"
        if name == "mlflow"
        else "platform.db"
    )
    if _output.json_mode:
        _output.print_json({"backend": name, "location": where})
        return
    _output.ok(f"Prompt backend: {name} ({where}) — set EXAMLOPS_PROMPT_BACKEND to change it")


@app.command("migrate", epilog=_EX_MIGRATE)
def migrate(
    to: str = typer.Option("mlflow", "--to", help="Destination backend: mlflow | platform_db"),
    source: str = typer.Option("platform_db", "--from", help="Source backend"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would move; write nothing"),
) -> None:
    """Copy every prompt (all versions in order, then labels) to another backend (ADR 0009).

    Version numbers are preserved; a prompt that already exists at the destination is skipped,
    because merging into it would renumber its history.
    """
    from examlops.prompts import migrate_prompts
    from examlops.prompts.mlflow_backend import PromptBackendError

    if not dry_run and not _output.confirm(f"Copy every prompt from {source} to {to}?"):
        raise typer.Exit(1)
    try:
        report = migrate_prompts(to=to, source=source, dry_run=dry_run)
    except (ValueError, RuntimeError, PromptBackendError) as exc:
        _output.error(str(exc))
    if not dry_run:
        write_audit_event(
            "exa-prompt",
            _actor(),
            "prompt_migrate",
            to,
            {k: report[k] for k in ("source", "destination", "migrated", "versions", "labels")},
        )
    if _output.json_mode:
        _output.print_json(report)
        return
    verb = "Would copy" if dry_run else "Copied"
    _output.ok(
        f"{verb} {len(report['migrated'])} prompt(s), {report['versions']} version(s), "
        f"{report['labels']} label(s) from {source} to {to}."
    )
    for s in report["skipped"]:
        _output.warning(f"Skipped {s['name']}: {s['reason']}")
