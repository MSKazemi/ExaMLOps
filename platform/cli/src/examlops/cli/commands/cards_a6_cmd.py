"""A6 — `exa cards`: Croissant dataset cards + structured model cards (ADR 0037).

Machine-readable governance artifacts auto-populated from live platform data, feeding D1
compliance and D2 evidence. Missing model-card fields render as "not provided" — never
fabricated. Cards are versioned + audited (D4).
"""

from __future__ import annotations

import json
import os

import typer

from examlops.cli import _output

app = typer.Typer(
    help="Cards — Croissant dataset metadata + structured model cards (A6)",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  exa cards dataset FData\n\n"
    "  exa cards dataset FData --revision <rev> --out fdata_croissant.json\n\n"
    "  exa cards model JPCP\n\n"
    "  exa cards model JPCP --out jpcp_card.md\n\n"
    "  exa cards completeness JPCP"
)


@app.command("dataset", epilog=_EXAMPLES)
def dataset_card(
    dataset: str = typer.Argument(..., help="Dataset name (e.g. FData)"),
    revision: str = typer.Option(None, "--revision", help="Dataset revision (A1)"),
    license_: str = typer.Option("CC-BY-4.0", "--license", help="Dataset license"),
    out: str = typer.Option(None, "--out", help="Write the Croissant JSON to this file"),
) -> None:
    """Emit + validate a Croissant JSON-LD dataset card (R1/R2)."""
    from examlops.cards import croissant_record, validate_croissant
    from examlops.data.registry import save_dataset_card
    from examlops.usecase import dataset_schema

    # The dataset's column schema is use-case content — read it from the active pack (ADR 0094).
    record = croissant_record(
        dataset, revision=revision, license=license_, schema=dataset_schema(dataset)
    )
    errors = validate_croissant(record)
    if errors:
        for e in errors:
            _output.error(f"Croissant validation: {e}")
        raise typer.Exit(1)
    version = save_dataset_card(dataset, json.dumps(record), revision=revision)
    if out:
        with open(out, "w") as fh:
            json.dump(record, fh, indent=2)
        _output.ok(f"Wrote validated Croissant card v{version} for {dataset} to {out}")
        return
    if _output.json_mode:
        _output.print_json(record)
        return
    _output.ok(f"Croissant card v{version} for {dataset} — valid.")
    _output.info(json.dumps(record, indent=2))


@app.command("model")
def model_card(
    model: str = typer.Argument(..., help="Model name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope (D6)"),
    out: str = typer.Option(None, "--out", help="Write the card Markdown to this file"),
    save: bool = typer.Option(True, "--save/--no-save", help="Persist a versioned card"),
) -> None:
    """Build a structured model card from live data — gaps as 'not provided' (R3/R4)."""
    from examlops.cards import build_model_card
    from examlops.data.registry import save_model_card

    card = build_model_card(model, tenant=tenant)
    if save:
        actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
        save_model_card(
            model, json.dumps(card.as_dict()), card.completeness, tenant=tenant, created_by=actor
        )
    if out:
        with open(out, "w") as fh:
            fh.write(card.to_markdown())
        _output.ok(f"Wrote model card ({card.completeness:.0%} complete) to {out}")
        return
    if _output.json_mode:
        _output.print_json(card.as_dict())
        return
    _output.info(card.to_markdown())
    _output.info(f"Completeness: {card.completeness:.0%}")


@app.command("export")
def export(
    subject: str = typer.Argument(..., help="Model name, or dataset name with --dataset"),
    dataset: bool = typer.Option(False, "--dataset", help="Export a dataset (Croissant) card"),
    out: str = typer.Option(None, "--out", help="Write the publishable card to this file"),
    force: bool = typer.Option(False, "--force", help="Publish despite a secret finding (audited)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Export a card for publication with PII, locations and internal fields scrubbed (clause 4).

    Internal fields are dropped, PII and site-specific locations are redacted, and a detected
    **secret blocks the export** — redacting it would hide that a credential reached a generated
    artifact at all. `--force` overrides that, audited, because the scanner is a regex heuristic
    and can be wrong; the dropped and redacted parts are not overridable, because they are not
    judgement calls.
    """
    from examlops.cards import build_model_card, croissant_record, export_card
    from examlops.data.audit import write_audit_event

    if dataset:
        card = croissant_record(subject)
    else:
        card = build_model_card(subject, tenant=tenant).as_dict()

    result = export_card(card)
    actor = os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
    blocked = bool(result.secret_findings) and not force

    write_audit_event(
        "exa-cards",
        actor,
        "card_export_blocked" if blocked else "card_exported",
        subject,
        {
            "kind": "dataset" if dataset else "model",
            "removed_fields": result.removed_fields,
            "redactions": result.redactions,
            "secret_rules": [f["rule"] for f in result.secret_findings],
            "forced": bool(result.secret_findings) and force,
        },
    )

    if blocked:
        rules = ", ".join(sorted({f["rule"] for f in result.secret_findings}))
        _output.error(
            f"Refusing to export {subject}: the card contains likely secret(s) [{rules}]. "
            "Fix the source data; use --force to publish anyway (audited).",
        )

    payload = json.dumps(result.content, indent=2, default=str)
    if out:
        with open(out, "w") as fh:
            fh.write(payload)
        _output.ok(f"Wrote publishable card for {subject} to {out}")
    elif _output.json_mode:
        _output.print_json(
            {
                "subject": subject,
                "card": result.content,
                "removed_fields": result.removed_fields,
                "redactions": result.redactions,
                "secret_findings": [f["rule"] for f in result.secret_findings],
            }
        )
        return
    else:
        _output.detail(payload)

    for removed in result.removed_fields:
        _output.info(f"removed internal field: {removed}")
    for redaction in result.redactions:
        _output.info(f"redacted: {redaction}")
    if result.secret_findings:
        _output.warning(
            f"exported despite {len(result.secret_findings)} secret finding(s) (--force, audited)"
        )


@app.command("completeness")
def completeness(
    model: str = typer.Argument(..., help="Model name"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    require: float = typer.Option(
        None, "--require", help="Exit 1 if completeness below this fraction (0..1)"
    ),
) -> None:
    """Score model-card completeness (0..1) — the D5/C3 promotion gate signal (R6)."""
    from examlops.cards import card_completeness

    score = card_completeness(model, tenant=tenant)
    if _output.json_mode:
        _output.print_json({"model": model, "completeness": score})
    else:
        _output.info(f"Model-card completeness for {model}: {score:.0%}")
    if require is not None and score < require:
        _output.error(f"Completeness {score:.0%} below required {require:.0%}")
