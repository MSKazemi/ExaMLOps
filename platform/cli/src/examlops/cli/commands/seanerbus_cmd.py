from __future__ import annotations

import re
import uuid
from pathlib import Path

import typer
import yaml

from examlops.cli import _client, _output
from examlops.cli._config import load_config

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

MODELS_DIR = Path("pipelines/models")

_EXAMPLES_LIST = "Examples:\n\n  exa seanerbus list\n\n  exa --json seanerbus list"
_EXAMPLES_INIT = "Examples:\n\n  exa seanerbus init-uuids"
_EXAMPLES_REGEN = "Examples:\n\n  exa seanerbus regen-uuid JPCP"
_EXAMPLES_STATUS = "Examples:\n\n  exa seanerbus status"


def _iter_yamls() -> list[tuple[str, Path, str]]:
    """Return list of (model_name, path, raw_text) for every non-private model YAML."""
    if not MODELS_DIR.is_dir():
        _output.error(f"{MODELS_DIR} not found — run from the repo root")
        raise typer.Exit(1)
    results = []
    for p in sorted(MODELS_DIR.glob("*.yaml")):
        if p.stem.startswith("_"):
            continue
        text = p.read_text()
        raw = yaml.safe_load(text) or {}
        results.append((raw.get("name", p.stem), p, text))
    return results


def _insert_uuid(text: str, uid: str) -> str:
    """Insert seanerbus_uuid after the 'enabled:' line (preserves all formatting)."""
    m = re.search(r"^enabled:.*$", text, re.MULTILINE)
    if m:
        pos = m.end()
        return text[:pos] + f"\nseanerbus_uuid: {uid}" + text[pos:]
    return text.rstrip() + f"\nseanerbus_uuid: {uid}\n"


def _replace_uuid(text: str, uid: str) -> str:
    """Replace existing seanerbus_uuid value in YAML text."""
    return re.sub(
        r"^seanerbus_uuid:.*$",
        f"seanerbus_uuid: {uid}",
        text,
        count=1,
        flags=re.MULTILINE,
    )


@app.command(name="list", epilog=_EXAMPLES_LIST)
def list_uuids():
    """Show all models and their SeanerBUS UUIDs."""
    rows = []
    for name, path, text in _iter_yamls():
        raw = yaml.safe_load(text) or {}
        rows.append(
            [
                name,
                raw.get("seanerbus_uuid") or "(not assigned)",
            ]
        )
    _output.print_table("SeanerBUS UUIDs", ["Model", "UUID"], rows)


@app.command(name="init-uuids", epilog=_EXAMPLES_INIT)
def init_uuids():
    """Assign a SeanerBUS UUID to every model that doesn't have one. Idempotent."""
    assigned: list[list[str]] = []
    for name, path, text in _iter_yamls():
        raw = yaml.safe_load(text) or {}
        if raw.get("seanerbus_uuid") is not None:
            continue
        new_uid = str(uuid.uuid4())
        path.write_text(_insert_uuid(text, new_uid))
        assigned.append([name, new_uid])
    if assigned:
        if _output.json_mode:
            import json as _json

            typer.echo(_json.dumps({"assigned": len(assigned), "models": [r[0] for r in assigned]}))
        else:
            _output.ok(f"Assigned {len(assigned)} new UUID(s) — commit the YAML changes to git:")
            _output.print_table("Assigned UUIDs", ["Model", "UUID"], assigned)
    else:
        typer.echo("All models already have UUIDs — nothing to do.")


@app.command(name="regen-uuid", epilog=_EXAMPLES_REGEN)
def regen_uuid(
    model: str = typer.Argument(..., help="Model name (e.g. JPCP)"),
):
    """Regenerate the SeanerBUS UUID for one model. Notify HPC teams of the change."""
    for name, path, text in _iter_yamls():
        if name.upper() == model.upper():
            new_uid = str(uuid.uuid4())
            raw = yaml.safe_load(text) or {}
            if raw.get("seanerbus_uuid") is not None:
                path.write_text(_replace_uuid(text, new_uid))
            else:
                path.write_text(_insert_uuid(text, new_uid))
            typer.echo(f"UUID regenerated for {name}: {new_uid}")
            typer.echo(
                "WARNING: HPC teams must update their configuration"
                " — the old UUID will no longer work."
            )
            return
    _output.error(f"Model {model!r} not found in {MODELS_DIR}")
    raise typer.Exit(1)


@app.command(epilog=_EXAMPLES_STATUS)
def status():
    """Probe the SeanerBUS bridge health and runtime stats endpoints."""
    cfg = load_config()
    base_url = getattr(cfg, "seanerbus_bridge_url", "http://localhost:18003")
    try:
        health = _client.get(f"{base_url}/health")
        stats = _client.get(f"{base_url}/stats")
    except _client.ClientError as e:
        _output.error(str(e))
        return
    _output.print_record({"health": health, "stats": stats})
