"""Implementation behind ``exa workbench export-pipeline`` (ADR 0160, Phase 1).

Kept out of ``workbench_cmd.py`` so that module stays a thin Typer surface, exactly as
``pipeline.py`` delegates ``compile``/``explain`` to ``pipeline_ir.py``. Extraction itself lives in
:mod:`examlops.pipeline_dsl.notebook` (pure, CLI-free); this module is only output and exit codes.

Two house rules are load-bearing here and are deliberately not optional:

* **Dropping is reported.** Every untagged code cell is named, and a run that dropped none says so
  rather than staying silent — absence is reported, never inferred.
* **A failed validation is a failed export.** The generated file is written first so the author can
  see and fix it, but the command exits non-zero with the compiler's own message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from examlops.cli import _output
from examlops.pipeline_dsl import IRError, NotLowerableError, lower_training
from examlops.pipeline_dsl.notebook import NotebookExportError, export_notebook


def default_out(notebook: str) -> str:
    """Phase 1 default: ``<notebook stem>_pipeline.py`` beside the caller's working directory."""
    return f"{Path(notebook).stem}_pipeline.py"


def export_pipeline(
    notebook: str, out: str | None, yaml_path: str | None, name: str | None
) -> None:
    """Extract, write, compile and report. Exits 1 on any extraction or validation failure."""
    target = out or default_out(notebook)
    try:
        extraction, doc = export_notebook(notebook, target, name=name)
    except NotebookExportError as exc:
        _output.error(
            f"Export failed: {exc}",
            hint="Tag a cell with one of: param, dataset, train, evaluate, promote, skip-export "
            "— and keep a tagged cell to a single literal assignment.",
        )
    except IRError as exc:
        # The generated file is on disk; the pipeline it describes is not valid.
        _output.error(
            f"Export failed: the generated pipeline does not compile: {exc}",
            hint=f"The generated file was still written to {target} — fix the notebook's tagged "
            "cells (or that file) and try again.",
        )

    lowered_note: list[str] = []
    if yaml_path:
        try:
            lowered = lower_training(doc)
        except NotLowerableError as exc:
            _output.error(f"Not lowerable: {exc}")
        import yaml

        Path(yaml_path).write_text(
            yaml.safe_dump(lowered.model_yaml, sort_keys=False), encoding="utf-8"
        )
        lowered_note = lowered.dropped

    dropped = [d.as_dict() for d in extraction.dropped]
    if _output.json_mode:
        payload: dict[str, Any] = {
            "notebook": notebook,
            "pipeline_file": target,
            "yaml_file": yaml_path,
            "name": doc["name"],
            "content_hash": doc["content_hash"],
            "steps": len(doc["nodes"]),
            "dropped": dropped,
            "warnings": extraction.warnings,
            "not_carried_by_yaml": lowered_note,
            "one_directional": True,
        }
        _output.print_json(payload)
        return

    _output.ok(f"Compiled {doc['name']}: {len(doc['nodes'])} steps, {doc['content_hash']}")
    _output.info(f"Pipeline written to {target}")
    if yaml_path:
        _output.info(f"Registry YAML written to {yaml_path}")
        for item in lowered_note:
            _output.warning(f"not carried by the YAML: {item}")
    for warning in extraction.warnings:
        _output.warning(warning)
    if dropped:
        _output.print_table(
            f"Dropped {len(dropped)} cell(s) (not tagged, not skip-export)",
            ["Cell", "Source"],
            [[d["index"], d["preview"]] for d in dropped],
        )
    else:
        _output.info("No cells dropped: every code cell was tagged or marked skip-export.")
    _output.hint(
        "One-directional: editing this file does not change the notebook — re-run "
        "export-pipeline to regenerate."
    )
