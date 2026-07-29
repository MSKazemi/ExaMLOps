"""``exa data synth`` — synthetic data generation (Next-Gen 40 · A7, ADR 0042).

Fit a generator to a real A1 dataset revision, generate provenance-flagged synthetic
records, and gate them on fidelity + privacy so synthetic data can never pass as real
(spec R1–R6). SDV is an optional ``examlops[synth]`` extra; without it a pure-python
Gaussian-copula fallback keeps every subcommand — including the release gate — working
offline.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from examlops.cli import _output
from examlops.data import init_db
from examlops.data.audit import write_audit_event
from examlops.data.data_assets import record_dataset_revision, record_synthetic_dataset

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EX_FIT = (
    "Examples:\n\n"
    "  exa data synth fit FData --path ./data/FData\n\n"
    "  exa data synth fit FData --path ./data/FData --method ctgan"
)
_EX_GENERATE = (
    "Examples:\n\n"
    "  exa data synth generate FData --path ./data/FData --rows 1000 --out ./data/FData_synth\n\n"
    "  exa data synth generate FData --path ./data/FData --rows 500 --min-privacy 0.6"
)
_EX_EVALUATE = (
    "Examples:\n\n"
    "  exa data synth evaluate FData --real ./data/FData --synthetic ./data/FData_synth"
)


def _actor() -> str:
    import os  # noqa: PLC0415

    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"


def _versioning() -> Any:
    """Import ``pipelines.datasets.versioning`` with a repo-root sys.path bootstrap."""
    repo_root = Path(__file__).resolve().parents[6]
    for p in (str(repo_root), str(repo_root / "modelzoo")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pipelines.datasets import versioning  # noqa: PLC0415

    return versioning


def _read_frame(path: str) -> Any:
    """Load a parquet file/dir into a single dataframe, or error with exit 1."""
    files = _versioning().discover_files(path)
    if not files:
        _output.error(f"No parquet files found under {path}.", exit_code=1)
    import pandas as pd  # noqa: PLC0415

    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def _validate_method(method: str) -> None:
    from examlops.synth import METHODS  # noqa: PLC0415

    if method not in METHODS:
        _output.error(
            f"Unknown --method {method!r}; expected one of: {', '.join(METHODS)}.", exit_code=1
        )


@app.command("fit", epilog=_EX_FIT)
def fit(
    dataset: str = typer.Argument(..., help="Dataset name (e.g. FData)"),
    path: str = typer.Option(..., "--path", "-p", help="Local parquet file/dir of real data"),
    method: str = typer.Option(
        "gaussian_copula", "--method", "-m", help="gaussian_copula | ctgan | tvae"
    ),
    seed: int = typer.Option(0, "--seed", help="Deterministic seed"),
) -> None:
    """Fit a generator to real data and report what it learned (spec R1 smoke-check)."""
    init_db()
    _validate_method(method)
    from examlops.synth import synth_fit  # noqa: PLC0415

    real = _read_frame(path)
    synth = synth_fit(dataset, method, data=real, seed=seed)
    payload = {
        "dataset": dataset,
        "method": method,
        "backend": synth.backend,
        "columns": len(synth.columns),
        "numeric": synth.numeric_cols,
        "categorical": synth.categorical_cols,
        "other": synth.other_cols,
    }
    if _output.json_mode:
        _output.print_json(payload)
        return
    _output.ok(
        f"Fitted [bold]{method}[/bold] on [bold]{dataset}[/bold] "
        f"([cyan]{synth.backend}[/cyan] backend, {len(synth.columns)} columns)."
    )
    if synth.backend == "fallback":
        _output.info("SDV not installed — using the pure-python copula fallback.")


@app.command("generate", epilog=_EX_GENERATE)
def generate(
    dataset: str = typer.Argument(..., help="Dataset name (e.g. FData)"),
    path: str = typer.Option(..., "--path", "-p", help="Local parquet file/dir of real data"),
    rows: int = typer.Option(..., "--rows", "-n", help="Number of synthetic rows to generate"),
    method: str = typer.Option(
        "gaussian_copula", "--method", "-m", help="gaussian_copula | ctgan | tvae"
    ),
    seed: int = typer.Option(0, "--seed", help="Deterministic seed"),
    min_fidelity: float = typer.Option(0.6, "--min-fidelity", help="Fidelity release floor"),
    min_privacy: float = typer.Option(0.5, "--min-privacy", help="Privacy release floor"),
    out: str | None = typer.Option(
        None, "--out", "-o", help="Directory to write the released synthetic parquet"
    ),
    force: bool = typer.Option(
        False, "--force", help="Record even if the gate blocks (still flagged, never as real)"
    ),
) -> None:
    """Generate, gate, and record a provenance-flagged synthetic dataset (spec R1–R4)."""
    init_db()
    if rows <= 0:
        _output.error("--rows must be positive.", exit_code=1)
    _validate_method(method)
    from examlops.synth import (  # noqa: PLC0415, E501
        GateThresholds,
        synth_evaluate,
        synth_fit,
        synth_generate,
    )

    real = _read_frame(path)
    versioning = _versioning()
    source_rev = versioning.resolve_revision(None, dataset, data_path=path)

    synth = synth_fit(source_rev.revision_id, method, data=real, seed=seed)
    result = synth_generate(synth, rows, seed=seed)
    gate = synth_evaluate(
        real,
        result.data,
        thresholds=GateThresholds(min_fidelity=min_fidelity, min_privacy=min_privacy),
    )
    released = bool(gate["released"]) or force

    # Record the gate outcome (provenance) regardless of pass/fail (spec R2/R3).
    record_synthetic_dataset(
        result.revision_id,
        dataset,
        source_revision=source_rev.revision_id,
        method=method,
        params=result.params,
        n_rows=result.n_rows,
        fidelity_score=gate["fidelity"]["score"],
        privacy_score=gate["privacy"]["score"],
        released=released,
        reasons=gate["reasons"],
        actor=_actor(),
    )

    if not gate["released"] and not force:
        write_audit_event(
            "exa-data",
            _actor(),
            "synth_blocked",
            dataset,
            {"revision_id": result.revision_id, "reasons": gate["reasons"], "method": method},
        )
        if _output.json_mode:
            _output.print_json({"dataset": dataset, "released": False, **_scores(gate)})
        else:
            for r in gate["reasons"]:
                _output.warning(r)
        _output.error(
            f"Synthetic dataset BLOCKED for {dataset} — failed the fidelity/privacy gate.",
            exit_code=1,
        )
        return

    # Released (or forced): materialise, record as a synthetic A1 revision + A2 lineage.
    uri = ""
    if out:
        out_dir = Path(out)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{dataset}_synth_{result.revision_id[:12]}.parquet"
        result.data.to_parquet(out_path, index=False)
        uri = str(out_path)

    rev = versioning.DatasetRevision(
        backend="synthetic",
        dataset=dataset,
        revision_id=result.revision_id,
        kind="synthetic",
        uri=uri,
        schema_hash=getattr(source_rev, "schema_hash", ""),
    )
    record_dataset_revision(
        rev,
        row_count=result.n_rows,
        actor=_actor(),
        synthetic=True,
        source_revision=source_rev.revision_id,
        generator=method,
    )
    _emit_lineage(dataset, source_rev.revision_id, result.revision_id, result.params)
    write_audit_event(
        "exa-data",
        _actor(),
        "synth_generated",
        dataset,
        {
            "revision_id": result.revision_id,
            "source_revision": source_rev.revision_id,
            "method": method,
            "rows": result.n_rows,
            "released": released,
            "forced": force and not gate["released"],
        },
    )

    if _output.json_mode:
        _output.print_json(
            {
                "dataset": dataset,
                "revision_id": result.revision_id,
                "source_revision": source_rev.revision_id,
                "synthetic": True,
                "released": released,
                "rows": result.n_rows,
                "uri": uri,
                **_scores(gate),
            }
        )
        return
    _output.ok(
        f"Synthetic [bold]{dataset}[/bold] @ [cyan]{result.revision_id[:12]}…[/cyan] "
        f"({result.n_rows} rows, flagged synthetic)."
    )
    _output.info(
        f"fidelity={gate['fidelity']['score']:.3f}  privacy={gate['privacy']['score']:.3f}"
    )
    if force and not gate["released"]:
        _output.warning(
            "Gate FAILED but recorded under --force (flagged synthetic, never as real)."
        )
    if uri:
        _output.detail(f"Written to {uri}")


@app.command("evaluate", epilog=_EX_EVALUATE)
def evaluate(
    dataset: str = typer.Argument(..., help="Dataset name (for the audit record)"),
    real: str = typer.Option(..., "--real", help="Local parquet file/dir of the real data"),
    synthetic: str = typer.Option(
        ..., "--synthetic", help="Local parquet file/dir of the synthetic data"
    ),
    min_fidelity: float = typer.Option(0.6, "--min-fidelity", help="Fidelity release floor"),
    min_privacy: float = typer.Option(0.5, "--min-privacy", help="Privacy release floor"),
) -> None:
    """Score fidelity + privacy of an existing synthetic set and apply the gate (spec R2/R3)."""
    init_db()
    from examlops.synth import GateThresholds, synth_evaluate  # noqa: PLC0415

    real_df = _read_frame(real)
    synth_df = _read_frame(synthetic)
    gate = synth_evaluate(
        real_df,
        synth_df,
        thresholds=GateThresholds(min_fidelity=min_fidelity, min_privacy=min_privacy),
    )
    write_audit_event(
        "exa-data",
        _actor(),
        "synth_evaluate",
        dataset,
        {"released": gate["released"], **_scores(gate)},
    )
    if _output.json_mode:
        _output.print_json({"dataset": dataset, "released": gate["released"], **gate})
    else:
        _output.print_record(
            {
                "Dataset": dataset,
                "Fidelity": f"{gate['fidelity']['score']:.3f}",
                "Privacy": f"{gate['privacy']['score']:.3f}",
                "Released": "✓" if gate["released"] else "✗",
            }
        )
        for r in gate["reasons"]:
            _output.warning(r)
    if not gate["released"]:
        _output.error(f"Gate FAILED for {dataset}: fidelity/privacy below threshold.", exit_code=1)


def _scores(gate: dict) -> dict[str, float]:
    return {
        "fidelity": gate["fidelity"]["score"],
        "privacy": gate["privacy"]["score"],
    }


def _emit_lineage(dataset: str, source_rev: str, synth_rev: str, params: dict) -> None:
    """Best-effort A2 lineage edge: real source revision → synthetic revision (spec R4)."""
    try:
        from examlops.lineage import dataset_node, emit_lineage  # noqa: PLC0415

        emit_lineage(
            "COMPLETE",
            job=f"synth:{dataset}",
            run_id=synth_rev,
            inputs=[dataset_node(dataset, source_rev)],
            outputs=[dataset_node(dataset, synth_rev)],
            facets={"generator": params},
            dataset_revision=synth_rev,
        )
    except Exception:  # lineage is bookkeeping — never fail the command on it
        pass
