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
    "  exa governance catalogue --subcategories\n\n"
    "  exa governance features\n\n"
    "  exa governance validate\n\n"
    "  exa governance report\n\n"
    "  exa governance report --model JPCP\n\n"
    "  exa governance crosswalk"
)


@app.command("catalogue", epilog=_EXAMPLES)
def catalogue(
    subcategories: bool = typer.Option(
        False,
        "--subcategories",
        help="List all AI RMF 1.0 subcategories and the platform controls mapped to each",
    ),
) -> None:
    """List the versioned NIST AI RMF control catalogue (R1)."""
    from examlops.governance import catalogue_version, load_catalogue, load_subcategories

    controls = load_catalogue()
    if subcategories:
        subs = load_subcategories()
        rows = [
            {
                "id": s.id,
                "function": s.function,
                "title": s.title,
                "controls": [c.id for c in controls if s.id in c.nist_subcategories],
            }
            for s in subs
        ]
        if _output.json_mode:
            _output.print_json({"version": catalogue_version(), "subcategories": rows})
            return
        mapped = sum(1 for r in rows if r["controls"])
        _output.info(
            f"NIST AI RMF 1.0 — {len(rows)} subcategories, {mapped} reached by a platform "
            "control (the rest need organisational evidence)"
        )
        _output.print_table(
            "AI RMF Subcategories",
            ["ID", "Function", "Title", "Platform controls"],
            [
                [r["id"], r["function"], r["title"], ", ".join(r["controls"]) or "organisational"]
                for r in rows
            ],
        )
        return
    if _output.json_mode:
        _output.print_json(
            {
                "version": catalogue_version(),
                "controls": [
                    {
                        "id": c.id,
                        "function": c.function,
                        "description": c.description,
                        "aliases": c.aliases,
                        "nist_subcategories": c.nist_subcategories,
                        "evidence": c.evidence,
                    }
                    for c in controls
                ],
            }
        )
        return
    _output.info(f"NIST AI RMF catalogue v{catalogue_version()} — {len(controls)} controls")
    _output.print_table(
        "Controls",
        ["ID", "Function", "Description", "Evidence"],
        [[c.id, c.function, c.description, ", ".join(c.evidence)] for c in controls],
    )


@app.command("features", epilog=_EXAMPLES)
def features_cmd() -> None:
    """Per-feature declarations: which controls each feature serves and what evidence it emits."""
    from examlops.governance import load_features

    feats = load_features()
    if _output.json_mode:
        _output.print_json(
            [
                {
                    "id": f.id,
                    "name": f.name,
                    "module": f.module,
                    "emits": f.emits,
                    "controls": f.controls,
                }
                for f in feats
            ]
        )
        return
    _output.print_table(
        "Governance feature declarations",
        ["Feature", "Name", "Module", "Emits", "Controls"],
        [[f.id, f.name, f.module, ", ".join(f.emits), ", ".join(f.controls)] for f in feats],
    )


@app.command("validate")
def validate() -> None:
    """Validate the feature→control mapping (CI gate, R2). Exit 1 on any error."""
    from examlops.governance import validate_mapping

    errors = validate_mapping()
    if _output.json_mode:
        # One document: the verdict and the problems together (it used to print the list and
        # then an ok()/error() document after it).
        problems = [{"control": e.control_id, "problem": e.problem} for e in errors]
        _output.print_json({"ok": not errors, "errors": problems})
        if errors:
            raise typer.Exit(1)
        return
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
        f"{s['satisfied']} satisfied, "
        f"{s['partial']} partial, {s['gap']} gap"
    )
    fw = rep.framework_summary
    _output.info(
        f"AI RMF 1.0 framework — {fw['platform_evidenced']}/{fw['subcategories']} subcategories "
        f"reached by a platform control ({fw['satisfied']} satisfied, {fw['partial']} partial, "
        f"{fw['gap']} gap); {fw['organisational']} need organisational evidence"
    )
    _output.print_table(
        "Control Coverage",
        [
            "Control",
            "Function",
            "Status",
            "Features",
            "EU AI Act",
            "ISO 42001",
            "Missing",
            "Insufficient",
        ],
        [
            [
                c.control.id,
                c.control.function,
                c.status,
                ", ".join(c.features) or "—",
                c.control.eu_ai_act or "—",
                c.control.iso_42001 or "—",
                ", ".join(c.missing_evidence) or "—",
                # Present but failed its integrity check, so it does not count (ADR 0110).
                ", ".join(c.insufficient_evidence) or "—",
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
        ["Control", "AI RMF subcategories", "EU AI Act", "ISO/IEC 42001"],
        [
            [
                r["control"],
                ", ".join(r["nist_subcategories"]) or "—",
                r["eu_ai_act"] or "—",
                r["iso_42001"] or "—",
            ]
            for r in rows
        ],
    )
