"""D1 — `exa compliance`: EU AI Act compliance tooling (ADR 0012).

Risk classification, Annex-IV technical-file generation from live evidence, Art. 12
logging conformance, and a conformity state machine. Every surface shows the non-legal-
advice disclaimer. Tenant-scoped (D6), audited (D4).
"""

from __future__ import annotations

import os

import typer

from examlops.cli import _output
from examlops.data.audit import write_audit_event

app = typer.Typer(
    help="EU AI Act compliance — risk classification, Annex-IV file, Art.12 logging",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa compliance classify JPCP --risk-tier high --purpose 'HPC job triage' "
    "--context 'internal ops'\n\n"
    "  exa compliance technical-file JPCP --out jpcp_annex_iv.md\n\n"
    "  exa compliance art12 JPCP\n\n"
    "  exa compliance status JPCP\n\n"
    "  exa compliance declare JPCP --state documented"
)


def _disclaimer() -> None:
    from examlops.compliance import DISCLAIMER

    _output.warning(DISCLAIMER)


@app.command("classify", epilog=_EXAMPLES)
def classify(
    model: str = typer.Argument(..., help="Model name"),
    risk_tier: str = typer.Option(..., "--risk-tier", help="prohibited|high|limited|minimal"),
    purpose: str = typer.Option(..., "--purpose", help="Intended purpose"),
    context: str = typer.Option("", "--context", help="Deployment context"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
) -> None:
    """Record a system's EU AI Act risk classification (R1)."""
    from examlops.compliance import classify_system

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    try:
        classify_system(model, risk_tier, purpose, context, actor, tenant=tenant)
    except ValueError as e:
        _output.error(str(e))
        return
    _output.ok(f"{model} classified as {risk_tier} risk ({purpose})")
    _disclaimer()


@app.command("technical-file")
def technical_file(
    model: str = typer.Argument(..., help="Model name"),
    out: str = typer.Option(None, "--out", help="Write the Annex-IV Markdown to this file"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Generate the Annex-IV technical file from live evidence, flagging gaps (R3/R4/R5)."""
    from examlops.compliance import generate_technical_file
    from examlops.data.governance import save_technical_file

    doc = generate_technical_file(model, tenant=tenant)
    md = doc.to_markdown()
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    version = save_technical_file(model, md, tenant=tenant, gaps=doc.gaps, generated_by=actor)
    if out:
        with open(out, "w") as fh:
            fh.write(md)
        _output.ok(f"Wrote technical file v{version} ({doc.gaps} gap(s)) to {out}")
        if doc.gaps:
            _output.warning(f"{doc.gaps} section(s) flagged as missing evidence.")
        return
    if _output.json_mode:
        _output.print_json(
            {
                "model": model,
                "version": version,
                "gaps": doc.gaps,
                "sections": [
                    {"title": s.title, "annex_iv": s.annex_iv, "present": s.present}
                    for s in doc.sections
                ],
            }
        )
        return
    _output.info(md)


@app.command("art12")
def art12(
    model: str = typer.Argument(..., help="Model name"),
) -> None:
    """Check Art. 12 record-keeping coverage in the immutable audit trail (R7)."""
    from examlops.compliance import check_art12_logging

    cov = check_art12_logging(model)
    if _output.json_mode:
        _output.print_json(cov)
        return
    _output.print_table(
        f"Art. 12 Logging Coverage — {model}",
        ["Event Type", "Present"],
        [[k, "yes" if v else "NO"] for k, v in cov["coverage"].items()],
    )
    _output.info(f"Coverage: {cov['coverage_pct']:.0%} · {cov['total_events']} total audit events")
    if cov["uncovered"]:
        _output.warning(f"Uncovered event types: {', '.join(cov['uncovered'])}")


@app.command("status")
def status(
    model: str = typer.Argument(None, help="Model name (omit to list all in-scope systems)"),
    tenant: str = typer.Option(None, "--tenant", help="Tenant scope"),
) -> None:
    """Show compliance classification + conformity state."""
    from examlops.data.governance import get_compliance_system, list_compliance_systems

    if model:
        sys = get_compliance_system(model)
        if not sys:
            _output.info(f"{model} is not registered as an in-scope system.")
            return
        if _output.json_mode:
            _output.print_json(sys)
            return
        _output.print_record(
            {
                "model": sys["model"],
                "in_scope": bool(sys["in_scope"]),
                "risk_tier": sys["risk_tier"] or "UNCLASSIFIED",
                "intended_purpose": sys["intended_purpose"] or "—",
                "conformity_state": sys["conformity_state"],
            }
        )
        _disclaimer()
        return
    rows = list_compliance_systems(tenant=tenant)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No systems registered in scope.")
        return
    _output.print_table(
        "Compliance Systems",
        ["Model", "Tenant", "Risk Tier", "Conformity"],
        [
            [r["model"], r["tenant"], r["risk_tier"] or "UNCLASSIFIED", r["conformity_state"]]
            for r in rows
        ],
    )


@app.command("declare")
def declare(
    model: str = typer.Argument(..., help="Model name"),
    state: str = typer.Option(..., "--state", help="draft|documented|assessed|declared"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Advance the conformity state machine with transition validation (R8)."""
    from examlops.compliance import set_conformity_state

    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    try:
        set_conformity_state(model, state, actor, tenant=tenant)
    except ValueError as e:
        _output.error(str(e))
        return
    _output.ok(f"{model} conformity state → {state}")
    _disclaimer()


@app.command("declaration")
def declaration(
    model: str = typer.Argument(..., help="Model name"),
    issued_at: str = typer.Option(
        ...,
        "--issued-at",
        help="Place and date of issue, e.g. 'Julich, 2026-09-02' (Annex V(8))",
    ),
    provider: str = typer.Option(None, "--provider", help="Provider legal name (Annex V(2))"),
    provider_address: str = typer.Option(
        None, "--provider-address", help="Provider address (Annex V(2))"
    ),
    signatory: str = typer.Option(None, "--signatory", help="Name of the signatory (Annex V(8))"),
    signatory_function: str = typer.Option(
        None, "--signatory-function", help="Function of the signatory (Annex V(8))"
    ),
    standard: list[str] = typer.Option(
        None, "--standard", help="Harmonised standard or common specification (repeatable)"
    ),
    notified_body: str = typer.Option(
        None, "--notified-body", help="Notified body name + identification number (Annex V(7))"
    ),
    personal_data: bool = typer.Option(
        False,
        "--personal-data/--no-personal-data",
        help="Whether the system processes personal data (Annex V(5))",
    ),
    out: str = typer.Option(None, "--out", help="Write the declaration Markdown to this file"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Generate the Annex-V EU Declaration of Conformity (ADR 0012 clause 4).

    Fields the platform can know are read from live metadata; the ones only the provider can
    state are yours to supply. Anything missing is left as an explicit placeholder and the
    document is stamped DRAFT with its reasons — never quietly filled in.
    """
    from examlops.compliance import generate_declaration
    from examlops.data.governance import DECLARATION, save_technical_file

    doc = generate_declaration(
        model,
        tenant=tenant,
        issued_at=issued_at,
        provider=provider,
        provider_address=provider_address,
        signatory=signatory,
        signatory_function=signatory_function,
        standards=list(standard or []),
        notified_body=notified_body,
        processes_personal_data=personal_data,
    )
    md = doc.to_markdown()
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    version = save_technical_file(
        model,
        md,
        tenant=tenant,
        gaps=len(doc.blockers),
        generated_by=actor,
        kind=DECLARATION,
    )
    write_audit_event(
        "exa-compliance",
        actor,
        "conformity_declaration_generated",
        model,
        {"version": version, "draft": doc.draft, "blockers": doc.blockers},
    )
    if out:
        with open(out, "w") as fh:
            fh.write(md)
        _output.ok(f"Wrote {'DRAFT ' if doc.draft else ''}declaration v{version} to {out}")
    elif _output.json_mode:
        _output.print_json(
            {
                "model": model,
                "version": version,
                "draft": doc.draft,
                "blockers": doc.blockers,
                "conformity_state": doc.conformity_state,
            }
        )
        return
    else:
        _output.detail(md)
    for blocker in doc.blockers:
        _output.warning(blocker)
    _disclaimer()


@app.command("framework")
def framework() -> None:
    """Show the control→article→evidence mapping (shared with D2)."""
    from examlops.compliance import FRAMEWORK

    if _output.json_mode:
        _output.print_json(FRAMEWORK)
        return
    _output.print_table(
        "Control → Article → Evidence",
        ["Control", "EU AI Act", "Evidence"],
        [[e["control"], e["article"], e["evidence"]] for e in FRAMEWORK],
    )
