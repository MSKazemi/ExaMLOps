"""D2 — `exa governance`: NIST AI RMF control backbone (ADR 0027).

Load the versioned NIST AI RMF catalogue, validate the feature→control mapping (CI gate),
and generate an evidence-coverage report crosswalked to the EU AI Act (D1) + ISO/IEC 42001.
Evidence coverage, not certification. Audited (D4), tenant-scoped (D6).
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="AI governance — NIST AI RMF control coverage & crosswalk",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa governance catalogue\n\n"
    "  exa governance validate\n\n"
    "  exa governance report\n\n"
    "  exa governance report --model JPCP\n\n"
    "  exa governance crosswalk"
)


@app.command("catalogue", epilog=_EXAMPLES)
def catalogue() -> None:
    """List the versioned NIST AI RMF control catalogue (R1)."""
    from examlops.governance import catalogue_version, load_catalogue

    controls = load_catalogue()
    if _output.json_mode:
        _output.print_json(
            {
                "version": catalogue_version(),
                "controls": [
                    {"id": c.id, "function": c.function, "description": c.description}
                    for c in controls
                ],
            }
        )
        return
    _output.info(f"NIST AI RMF catalogue v{catalogue_version()} — {len(controls)} controls")
    _output.print_table(
        "Controls",
        ["ID", "Function", "Description"],
        [[c.id, c.function, c.description] for c in controls],
    )


@app.command("validate")
def validate() -> None:
    """Validate the feature→control mapping (CI gate, R2). Exit 1 on any error."""
    from examlops.governance import validate_mapping

    errors = validate_mapping()
    if _output.json_mode:
        _output.print_json([{"control": e.control_id, "problem": e.problem} for e in errors])
    if errors:
        for e in errors:
            _output.error(f"{e.control_id}: {e.problem}")
        raise typer.Exit(1)
    _output.ok("Governance mapping valid — all controls reference real evidence collectors.")


@app.command("report")
def report(
    model: str = typer.Option(None, "--model", help="One model (default: fleet-wide posture)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
) -> None:
    """Evidence-coverage report: satisfied / partial / gap per control (R3/R4)."""
    from examlops.governance import COVERAGE_DISCLAIMER, governance_report

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    rep = governance_report(tenant, model=model, persist_by=actor)
    if _output.json_mode:
        _output.print_json(rep.as_dict())
        return
    s = rep.summary
    _output.info(
        f"NIST AI RMF coverage v{rep.version} — "
        f"[green]{s['satisfied']} satisfied[/green], "
        f"[yellow]{s['partial']} partial[/yellow], [red]{s['gap']} gap[/red]"
    )
    _output.print_table(
        "Control Coverage",
        ["Control", "Function", "Status", "EU AI Act", "ISO 42001", "Missing"],
        [
            [
                c.control.id,
                c.control.function,
                c.status,
                c.control.eu_ai_act or "—",
                c.control.iso_42001 or "—",
                ", ".join(c.missing_evidence) or "—",
            ]
            for c in rep.controls
        ],
    )
    _output.warning(COVERAGE_DISCLAIMER)


@app.command("crosswalk")
def crosswalk_cmd() -> None:
    """Show the control → EU AI Act + ISO/IEC 42001 crosswalk (R5)."""
    from examlops.governance import crosswalk

    rows = crosswalk()
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        "Control Crosswalk",
        ["Control", "EU AI Act", "ISO/IEC 42001"],
        [[r["control"], r["eu_ai_act"] or "—", r["iso_42001"] or "—"] for r in rows],
    )
