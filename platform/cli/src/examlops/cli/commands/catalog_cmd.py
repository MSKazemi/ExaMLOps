"""``exa catalog`` — the Model Catalog: what you could start from (ADR 0158).

Deliberately filed in the **Models & Registry** help panel, next to ``exa models``: operators
will look for one where the other lives, and panel adjacency plus explicit wording is the first
defense against the conflation ADR 0158 decision 4 exists to prevent.

* ``exa catalog list`` / ``show`` — browse curated, provenance-tracked model *definitions*.
* ``exa catalog publish`` — add one, behind the publish-time trust gate (admin).
* ``exa catalog pull`` — materialize one into a project as a per-model YAML (admin).

``pull`` never trains and never serves; it creates no registry version and no alias. What it
produces is an ordinary, editable model definition with no training run yet.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

import typer
import yaml

from examlops.catalog import (
    CatalogEntryError,
    CatalogPullError,
    get_entry,
    list_entries,
    publish_entry,
    pull_entry,
)
from examlops.cli import _output
from examlops.data.audit import write_audit_event

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Model Catalog — curated model definitions you can start from (ADR 0158)",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EXAMPLES = (
    "Examples:\n\n"
    "  # what could I start from?\n"
    "  exa catalog list\n\n"
    "  exa catalog list --kind recipe --evaluated-only\n\n"
    "  exa catalog show distilbert-classifier-base\n\n"
    "  exa catalog show distilbert-classifier-base@2 --output json\n\n"
    "  # add an entry (curated: an unpinned source is refused here, not flagged later)\n"
    "  exa catalog publish ./entries/distilbert-classifier-base.yaml --sign\n\n"
    "  # start a project from one — writes a model YAML, trains nothing\n"
    "  exa catalog pull distilbert-classifier-base --project research --dry-run\n\n"
    "  exa catalog pull distilbert-classifier-base --project research --as TextClass"
)

_NOT_THE_REGISTRY = (
    "A catalog entry is a starting point, not a registry version: nothing was trained, "
    "registered or served. `exa models` still answers what we produced."
)


class EntryKind(StrEnum):
    base_model = "base_model"
    recipe = "recipe"


class TrustTier(StrEnum):
    T1_signed = "T1_signed"
    T1_unsigned = "T1_unsigned"


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


def _trust_marker(tier: str) -> str:
    return "signed" if tier == "T1_signed" else "UNSIGNED"


def _resolve(entry: str):
    """The entry, or a named exit — never a silent fallback to something else."""
    try:
        found = get_entry(entry)
    except CatalogEntryError as exc:
        _output.error(str(exc))
    if found is None:
        _output.error(
            f"No catalog entry {entry!r}. List what is available: exa catalog list",
            hint="the catalog is curated, so an entry exists only once someone published it "
            "(`exa catalog publish <entry.yaml>`) — it is never created by a training run",
        )
    return found


@app.command("list", epilog=_EXAMPLES)
def catalog_list(
    kind: EntryKind | None = typer.Option(None, "--kind", help="base_model or recipe"),
    license_id: str | None = typer.Option(None, "--license", help="SPDX identifier, e.g. mit"),
    trust_tier: TrustTier | None = typer.Option(
        None, "--trust-tier", help="T1_signed or T1_unsigned — never hidden behind a generic OK"
    ),
    evaluated_only: bool = typer.Option(
        False, "--evaluated-only", help="only entries that point at an eval summary"
    ),
    all_versions: bool = typer.Option(
        False, "--all-versions", help="every catalog_version, not just the newest of each entry"
    ),
) -> None:
    """Browse the catalog — curated model definitions you could start from."""
    entries = list_entries(
        kind=kind.value if kind else None,
        license_id=license_id,
        trust_tier=trust_tier.value if trust_tier else None,
        evaluated_only=evaluated_only,
        all_versions=all_versions,
    )
    if _output.json_mode:
        _output.print_json([e.to_dict() for e in entries])
        return
    if not entries:
        _output.warning(
            "The catalog is empty. Publish an entry: exa catalog publish <entry.yaml> "
            "(the catalog is curated — it is not populated by training runs)"
        )
        return
    _output.print_table(
        "Model catalog — what you could start from",
        ["Entry", "Ver", "Kind", "License", "Trust", "Eval", "Resource", "Source"],
        [
            [
                e.name,
                str(e.catalog_version),
                e.kind,
                e.license,
                _trust_marker(e.trust_tier),
                e.eval_suite or "unevaluated",
                e.resource_hint or "—",
                f"{e.source_kind}:{e.source_ref}",
            ]
            for e in entries
        ],
    )
    _output.info(_NOT_THE_REGISTRY)


@app.command("show", epilog=_EXAMPLES)
def catalog_show(
    entry: str = typer.Argument(..., help="Catalog entry, as name or name@catalog_version"),
) -> None:
    """Show one catalog entry (default: its newest catalog_version)."""
    found = _resolve(entry)
    if _output.json_mode:
        _output.print_json(found.to_dict())
        return
    _output.print_record(
        {
            "Entry": found.ref,
            "Kind": found.kind,
            "Entry hash": found.entry_hash,
            "Source": f"{found.source_kind}: {found.source_ref}",
            "License": found.license,
            "Trust tier": f"{found.trust_tier} ({_trust_marker(found.trust_tier)})",
            "Signature": found.supplychain_ref or "— (no signature on record)",
            "Eval summary": found.eval_suite or "— unevaluated",
            "Resource hint": found.resource_hint or "—",
            "Recipe template": found.model_yaml_template or "— (bare base model)",
            "Description": found.description or "—",
            "Published": f"{found.published_at or '—'} by {found.published_by or '—'}",
        }
    )
    if not found.signed:
        _output.warning(
            "unsigned source — this entry comes from a registered source with no "
            "examlops.supplychain signature. It is offered, never equal-trust."
        )
    if not found.evaluated:
        _output.warning("unevaluated — this entry points at no eval suite result.")
    _output.info(_NOT_THE_REGISTRY)


@app.command("publish", epilog=_EXAMPLES)
def catalog_publish(
    path: Path = typer.Argument(..., help="Path to the entry YAML file to publish"),
    sign: bool = typer.Option(
        False, "--sign", help="sign the entry with examlops.supplychain (the only signer)"
    ),
) -> None:
    """Publish a catalog entry. An unpinned source is refused here, never flagged later."""
    if not path.is_file():
        _output.error(f"No such entry file: {path}")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        _output.error(f"{path} is not valid YAML: {exc}")

    try:
        created, entry = publish_entry(doc, actor=_actor(), sign=sign)
    except CatalogEntryError as exc:
        # Every reason, in the refusal itself: a curator fixing an entry should not have to
        # re-run under --verbose to find out what was wrong with it.
        _output.error(
            "Refused to publish this catalog entry:\n"
            + "\n".join(f"  - {problem}" for problem in exc.problems)
        )

    write_audit_event(
        "cli",
        _actor(),
        "catalog_publish",
        entry.name,
        {
            "catalog_version": entry.catalog_version,
            "entry_hash": entry.entry_hash,
            "trust_tier": entry.trust_tier,
            "source": f"{entry.source_kind}:{entry.source_ref}",
            "license": entry.license,
            "created": created,
        },
    )
    if _output.json_mode:
        _output.print_json({**entry.to_dict(), "created": created})
        return
    if created:
        _output.ok(f"Published catalog entry {entry.ref} ({entry.entry_hash})")
    else:
        _output.info(
            f"Catalog entry {entry.ref} already holds this exact content — nothing published "
            "(an immutable entry is never rewritten; a change publishes the next version)"
        )
    if not entry.signed:
        _output.warning(
            "trust_tier=T1_unsigned — published WITHOUT a signature. `--json` output carries "
            "trust_tier so a CI gate can refuse it."
        )


@app.command("pull", epilog=_EXAMPLES)
def catalog_pull(
    entry: str = typer.Argument(..., help="Catalog entry, as name or name@catalog_version"),
    project: str = typer.Option(..., "--project", help="Project to pull the entry into"),
    as_: str | None = typer.Option(
        None, "--as", help="Model name to materialize as (default: the entry name)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="preview the rendered YAML and the lineage edge; write nothing"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Materialize a catalog entry into a project. Trains nothing, serves nothing."""
    found = _resolve(entry)
    if not dry_run and not _output.confirm(
        f"Pull {found.ref} ({_trust_marker(found.trust_tier)}) into project '{project}'?",
        auto_yes=yes,
    ):
        _output.info("Cancelled.")
        return

    try:
        result = pull_entry(
            found,
            project,
            model_name=as_,
            actor=_actor(),
            dry_run=dry_run,
        )
    except CatalogPullError as exc:
        _output.error(str(exc))

    if not dry_run:
        write_audit_event(
            "cli",
            _actor(),
            "catalog_pull",
            found.name,
            {
                "catalog_version": found.catalog_version,
                "entry_hash": found.entry_hash,
                "project": project,
                "model_name": result.model_name,
                "path": result.path,
                "trust_tier": found.trust_tier,
            },
        )

    if _output.json_mode:
        _output.print_json(result.to_dict())
        return
    if dry_run:
        _output.info(f"Dry run — nothing written. {result.path} would contain:")
        typer.echo(result.rendered)
        _output.info(f"lineage edge: {result.lineage_edge}")
        return
    _output.ok(f"Pulled {found.ref} into project '{project}' as model '{result.model_name}'")
    _output.detail(f"  model definition: {result.path}")
    _output.detail(f"  lineage edge:     {result.lineage_edge}")
    _output.detail(f"  project resource: project_resources(kind=model, ref={result.model_name})")
    if result.unsigned_source:
        _output.warning(
            "unsigned source — the materialized YAML carries an explicit flag. Review it "
            "before training or serving anything from it."
        )
    if not result.evaluated:
        _output.warning("unevaluated entry — no eval suite result is on record for it.")
    _output.info(
        f"Nothing was trained, registered or served. Next: exa pipeline run --model "
        f"{result.model_name}"
    )
