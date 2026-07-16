"""``exa data`` — dataset versioning & reproducibility (Next-Gen 40 · A1, ADR 0003).

Create, list, diff, and checkout immutable dataset revisions. Revisions are resolved
by ``pipelines.datasets.versioning`` (lakeFS when configured, else a deterministic
content hash) and recorded in ``platform_db.dataset_revisions``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import typer

from examlops.cli import _output
from examlops.platform_db import (
    get_dataset_revision,
    get_dataset_revisions,
    init_db,
    record_data_quality_check,
    record_dataset_revision,
    write_audit_event,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EX_SNAPSHOT = (
    "Examples:\n\n"
    "  exa data snapshot FData --path ./data/FData\n\n"
    "  exa data snapshot FData --backend minio --path ./cache/fdata.parquet"
)
_EX_LIST = "Examples:\n\n  exa data list FData\n\n  exa --json data list FData"
_EX_DIFF = "Examples:\n\n  exa data diff FData <revA> <revB>"
_EX_CHECKOUT = "Examples:\n\n  exa data checkout FData <rev> --path ./data/FData"
_EX_VALIDATE = (
    "Examples:\n\n"
    "  exa data validate FData --path ./data/FData\n\n"
    "  exa data validate FData --path ./data/FData --revision <rev>"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"


def _load_versioning() -> Any:
    """Import ``pipelines.datasets.versioning`` with a repo-root sys.path bootstrap.

    The CLI normally shells out to the generator, so the ``pipelines`` package may not
    be importable in-process; ensure the repo root (and modelzoo) are on the path.
    """
    repo_root = Path(__file__).resolve().parents[6]
    for p in (str(repo_root), str(repo_root / "modelzoo")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pipelines.datasets import versioning  # noqa: PLC0415 - lazy, path-bootstrapped

    return versioning


def _short(rev: str) -> str:
    return rev if len(rev) <= 14 else f"{rev[:12]}…"


@app.command("snapshot", epilog=_EX_SNAPSHOT)
def snapshot(
    dataset: str = typer.Argument(..., help="Dataset name (e.g. FData)"),
    backend: str | None = typer.Option(
        None, "--backend", "-b", help="Storage backend (zenodo|minio|dataplane)"
    ),
    path: str | None = typer.Option(
        None, "--path", "-p", help="Local file/dir of already-materialised parquet to hash"
    ),
) -> None:
    """Resolve the current dataset state to a revision and record it (spec R8)."""
    init_db()
    versioning = _load_versioning()
    rev = versioning.resolve_revision(backend, dataset, data_path=path)
    files = versioning.discover_files(path)
    row_count = versioning.count_rows(files) if files else None
    record_dataset_revision(
        rev,
        row_count=row_count,
        byte_count=rev.byte_count,
        actor=_actor(),
    )
    write_audit_event(
        "exa-data",
        _actor(),
        "dataset_snapshot",
        dataset,
        {"backend": rev.backend, "revision_id": rev.revision_id, "kind": rev.kind},
    )
    if _output.json_mode:
        _output.print_json(
            {
                "dataset": dataset,
                "backend": rev.backend,
                "revision_id": rev.revision_id,
                "kind": rev.kind,
                "schema_hash": rev.schema_hash,
                "row_count": row_count,
                "byte_count": rev.byte_count,
            }
        )
        return
    if not rev.is_known:
        _output.warning(
            f"Could not resolve concrete data for [bold]{dataset}[/bold] — recorded 'unknown'. "
            "Pass --path to a materialised parquet file/dir, or configure lakeFS."
        )
    _output.ok(f"Snapshot recorded: [bold]{dataset}[/bold] @ [cyan]{rev.revision_id}[/cyan]")


@app.command("list", epilog=_EX_LIST)
def list_revisions(
    dataset: str = typer.Argument(..., help="Dataset name"),
    backend: str | None = typer.Option(None, "--backend", "-b", help="Filter by backend"),
) -> None:
    """List recorded revisions newest-first, with linked runs (spec R9)."""
    init_db()
    rows = get_dataset_revisions(dataset, backend)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info(f"No recorded revisions for [bold]{dataset}[/bold].")
        return
    _output.print_table(
        f"Dataset revisions — {dataset}",
        ["Revision", "Kind", "Backend", "Rows", "Run", "Actor", "Created"],
        [
            [
                _short(r["revision_id"]),
                r["kind"],
                r["backend"],
                str(r["row_count"]) if r["row_count"] is not None else "-",
                r["mlflow_run_id"] or "-",
                r["actor"] or "-",
                (r["created_at"] or "")[:19],
            ]
            for r in rows
        ],
    )


def _require_rev(dataset: str, revision_id: str) -> dict[str, Any]:
    row = get_dataset_revision(dataset, revision_id)
    if row is None:
        _output.error(f"Revision [bold]{revision_id}[/bold] not found for {dataset}.")
    return row  # type: ignore[return-value]


@app.command("diff", epilog=_EX_DIFF)
def diff(
    dataset: str = typer.Argument(..., help="Dataset name"),
    rev_a: str = typer.Argument(..., metavar="REV_A", help="Baseline revision id"),
    rev_b: str = typer.Argument(..., metavar="REV_B", help="Comparison revision id"),
) -> None:
    """Report row-count / schema / size deltas between two revisions (spec R10)."""
    init_db()
    a = _require_rev(dataset, rev_a)
    b = _require_rev(dataset, rev_b)
    ra, rb = a["row_count"], b["row_count"]
    row_delta = (rb - ra) if (ra is not None and rb is not None) else None
    ba, bb = a["byte_count"], b["byte_count"]
    byte_delta = (bb - ba) if (ba is not None and bb is not None) else None
    schema_changed = a["schema_hash"] != b["schema_hash"]
    result = {
        "dataset": dataset,
        "rev_a": rev_a,
        "rev_b": rev_b,
        "row_count_a": ra,
        "row_count_b": rb,
        "row_count_delta": row_delta,
        "byte_count_delta": byte_delta,
        "schema_changed": schema_changed,
    }
    if _output.json_mode:
        _output.print_json(result)
        return
    _output.print_table(
        f"Dataset diff — {dataset}",
        ["Metric", _short(rev_a), _short(rev_b), "Δ"],
        [
            ["rows", str(ra), str(rb), "-" if row_delta is None else f"{row_delta:+d}"],
            ["bytes", str(ba), str(bb), "-" if byte_delta is None else f"{byte_delta:+d}"],
            [
                "schema",
                a["schema_hash"][:12] or "-",
                b["schema_hash"][:12] or "-",
                "changed" if schema_changed else "same",
            ],
        ],
    )


@app.command("checkout", epilog=_EX_CHECKOUT)
def checkout(
    dataset: str = typer.Argument(..., help="Dataset name"),
    revision_id: str = typer.Argument(..., metavar="REV", help="Revision id to materialise"),
    path: str | None = typer.Option(
        None, "--path", "-p", help="Local file/dir to verify against a content revision"
    ),
) -> None:
    """Materialise / verify the exact pinned data, or exit non-zero (spec R11)."""
    init_db()
    row = _require_rev(dataset, revision_id)
    if row["kind"] == "lakefs":
        # lakeFS checkout is operator infrastructure; report the pinned URI to use.
        _output.ok(f"lakeFS revision [cyan]{revision_id}[/cyan] → {row['uri'] or 'lakefs://'}")
        return
    if not path:
        _output.error(
            "Content revisions verify against local data — pass --path to the materialised parquet.",
            hint="exa data checkout FData <rev> --path ./data/FData",
        )
    versioning = _load_versioning()
    files = versioning.discover_files(path)
    try:
        actual, _, _ = versioning.content_revision(files)
    except FileNotFoundError:
        _output.error(f"No parquet files found under {path} to verify.")
        return
    if actual != revision_id:
        _output.error(
            f"Data at {path} does not match revision {revision_id} (computed {_short(actual)}).",
            exit_code=1,
        )
    _output.ok(f"Verified: {path} matches [cyan]{revision_id}[/cyan]")


@app.command("validate", epilog=_EX_VALIDATE)
def validate(
    dataset: str = typer.Argument(..., help="Dataset name (must have a contract)"),
    path: str = typer.Option(..., "--path", "-p", help="Local parquet file/dir to validate"),
    revision: str | None = typer.Option(None, "--revision", help="A1 revision id for provenance"),
) -> None:
    """Validate a dataset against its data contract; exit non-zero on error violations (spec R11)."""
    init_db()
    repo_root = Path(__file__).resolve().parents[6]
    for p in (str(repo_root), str(repo_root / "modelzoo")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pipelines.contracts import load_contract  # noqa: PLC0415

    contract = load_contract(dataset)
    if contract is None:
        _output.error(
            f"No data contract found for [bold]{dataset}[/bold] "
            f"(expected pipelines/contracts/{dataset.lower()}.py)."
        )
        return
    files = _load_versioning().discover_files(path)
    if not files:
        _output.error(f"No parquet files found under {path}.")
        return
    import pandas as pd  # noqa: PLC0415

    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    result = contract.validate(df)
    record_data_quality_check(dataset, result, revision=revision, stage="validate", actor=_actor())
    write_audit_event(
        "exa-data",
        _actor(),
        "data_validate",
        dataset,
        {"passed": result.passed, "score": result.score, "errors": len(result.errors)},
    )
    if _output.json_mode:
        _output.print_json(
            {
                "dataset": dataset,
                "passed": result.passed,
                "score": result.score,
                "checks": result.checks,
            }
        )
    else:
        _output.print_table(
            f"Data contract — {dataset} (v{contract.version})",
            ["Check", "Severity", "Result", "Observed"],
            [
                [c["name"], c["severity"], "✓" if c["passed"] else "✗", str(c["observed"])[:48]]
                for c in result.checks
            ],
        )
        _output.info(f"Quality score: {result.score:.2%}")
    if not result.passed:
        _output.error(
            f"Data contract FAILED for {dataset}: {len(result.errors)} error-severity violation(s).",
            exit_code=1,
        )
