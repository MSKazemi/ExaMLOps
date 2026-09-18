"""exa dataplane — pull remote data into versioned snapshots (ADR 0130)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, NoReturn

import typer

from examlops.cli import _output

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Dataplane — pull SQL, object storage, Zenodo, REST and Kafka data into versioned snapshots.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
sources_app = typer.Typer(
    no_args_is_help=True,
    help="Register, inspect and remove dataplane sources.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(sources_app, name="sources")

_EX_CREATE = (
    "Examples:\n\n"
    "  # A Postgres table, credentials from a Named Connection\n"
    '  exa connection create lab-pg --kind sql --config \'{"url": "postgresql+psycopg://reader@db.lab/jobs"}\'\n'
    '  exa dataplane sources create pm100 --connector sql --connection lab-pg --spec-json \'{"table": "jobs"}\'\n\n'
    "  # A public Zenodo record, refreshed daily\n"
    "  exa dataplane sources create pm100-zenodo --connector zenodo --spec-json '{\"record\": 10127767}' --schedule 1d"
)
_EX_PRUNE = (
    "Prune never runs beside a pull of the same source (it takes the source's pull lock), and it "
    "refuses to prune blind: if the store has snapshots but the catalog has no revision rows for "
    "the source (a lost or restored platform.db), run `exa dataplane catalog-rebuild` first, or "
    "pass --force.\n\n"
    "Examples:\n\n  exa dataplane prune pm100 --keep 5 --dry-run\n\n"
    "  exa dataplane prune pm100 --keep 5"
)
_EX_REBUILD = (
    "Walks every source prefix and committed manifest in the store; idempotent. Source "
    "definitions are not in the store — sources that have snapshots but no definition are listed "
    "to re-register.\n\n"
    "Examples:\n\n  exa dataplane catalog-rebuild --dry-run\n\n  exa dataplane catalog-rebuild"
)
_EX_PULL = (
    "Examples:\n\n  exa dataplane pull pm100\n\n  # Ignore the watermark and re-read everything\n"
    "  exa dataplane pull pm100 --full"
)


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"


def _fail(exc: Exception) -> NoReturn:
    _output.error(str(exc))


def _source_row(s: Any) -> dict[str, Any]:
    return {
        "project": s.project,
        "name": s.name,
        "connector": s.connector,
        "connection": s.connection,
        "schedule": s.schedule,
        "enabled": s.enabled,
        "spec": s.spec,
        "limits": s.limits.to_dict(),
        "contract": s.contract,
    }


@app.command("connectors")
def connectors() -> None:
    """List connector kinds, whether their dependencies are installed, and plugin load errors."""
    from examlops.dataplane.connectors import registry

    rows = []
    for c in registry.all_connectors():
        ok, why = c.available()
        rows.append(
            {
                "kind": c.kind,
                "available": ok,
                "detail": why,
                "connection_kinds": list(c.connection_kinds),
                "incremental": c.supports_incremental,
                "extra": c.extra,
            }
        )
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        "Dataplane connectors",
        ["kind", "available", "incremental", "extra", "detail"],
        [
            [
                r["kind"],
                "yes" if r["available"] else "no",
                "yes" if r["incremental"] else "-",
                r["extra"] or "-",
                r["detail"] or "-",
            ]
            for r in rows
        ],
    )
    for name, err in registry.plugin_errors().items():
        _output.warning(f"plugin {name} failed to load: {err}")


@sources_app.command("list")
def sources_list(
    project: str | None = typer.Option(None, "--project", "-p", help="Only this project"),
) -> None:
    """List registered sources."""
    from examlops import dataplane

    rows = [_source_row(s) for s in dataplane.list_source_defs(project)]
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        "Dataplane sources",
        ["project", "name", "connector", "connection", "schedule"],
        [
            [
                r["project"] or "-",
                r["name"],
                r["connector"],
                r["connection"] or "-",
                r["schedule"] or "-",
            ]
            for r in rows
        ],
    )


@sources_app.command("show")
def sources_show(name: str, project: str = typer.Option("", "--project", "-p")) -> None:
    """Show one source definition (never any credential)."""
    from examlops import dataplane

    try:
        row = _source_row(dataplane.get_source_def(name, project))
    except Exception as exc:
        _fail(exc)
    _output.print_json(row) if _output.json_mode else _output.print_record(row)


@sources_app.command("create", epilog=_EX_CREATE)
def sources_create(
    name: str,
    connector: str = typer.Option(
        ..., "--connector", "-k", help="Connector kind (see: exa dataplane connectors)"
    ),
    connection: str | None = typer.Option(
        None, "--connection", help="Named Connection holding the credentials"
    ),
    spec_json: str = typer.Option(
        "{}", "--spec-json", help="Inline JSON source spec (what to read)"
    ),
    schedule: str | None = typer.Option(
        None, "--schedule", help="Refresh interval: 15m, 6h, 1d, @daily"
    ),
    max_rows: int | None = typer.Option(None, "--max-rows", help="Refuse pulls larger than this"),
    max_bytes: int | None = typer.Option(
        None, "--max-bytes", help="Refuse pulls larger than this many bytes"
    ),
    contract: str | None = typer.Option(
        None, "--contract", help="Data contract checked before commit"
    ),
    project: str = typer.Option("", "--project", "-p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and show; register nothing"),
) -> None:
    """Register or update a source."""
    from examlops import dataplane
    from examlops.dataplane.connectors import registry
    from examlops.dataplane.types import Limits

    try:
        spec = json.loads(spec_json)
        c = registry.get(connector)
        errors = c.validate_spec(spec)
    except Exception as exc:
        _fail(exc)
    if dry_run:
        out = {
            "dry_run": True,
            "name": name,
            "connector": connector,
            "spec": spec,
            "errors": errors,
        }
        _output.print_json(out) if _output.json_mode else _output.print_record(out)
        return
    try:
        s = dataplane.define_source(
            name,
            connector,
            project=project,
            connection=connection,
            spec=spec,
            schedule=schedule,
            limits=Limits(max_rows=max_rows, max_bytes=max_bytes),
            contract=contract,
            actor=_actor(),
        )
    except Exception as exc:
        _fail(exc)
    _output.print_json(_source_row(s)) if _output.json_mode else _output.ok(
        f"source {s.key} registered"
    )


@sources_app.command("apply")
def sources_apply(
    file: Path = typer.Option(
        ..., "--file", "-f", exists=True, dir_okay=False, help="YAML file with a sources: list"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate every entry; register nothing"),
) -> None:
    """Register every source in a YAML file (GitOps). Credentials are refused."""
    import yaml

    from examlops import dataplane
    from examlops.dataplane.types import Limits

    entries = (yaml.safe_load(file.read_text()) or {}).get("sources") or []
    done = []
    try:
        for e in entries:
            if dry_run:
                from examlops.dataplane.pull import _reject_secret_keys

                _reject_secret_keys(e.get("spec") or {})
                done.append({"name": e["name"], "dry_run": True})
                continue
            s = dataplane.define_source(
                e["name"],
                e["connector"],
                project=e.get("project", ""),
                connection=e.get("connection"),
                spec=e.get("spec") or {},
                schedule=e.get("schedule"),
                limits=Limits.from_dict(e.get("limits")),
                contract=e.get("contract"),
                actor=_actor(),
            )
            done.append({"name": s.name, "key": s.key})
    except Exception as exc:
        _fail(exc)
    _output.print_json(done) if _output.json_mode else _output.ok(f"{len(done)} source(s) applied")


@sources_app.command("delete")
def sources_delete(
    name: str,
    project: str = typer.Option("", "--project", "-p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be removed"),
) -> None:
    """Remove a source definition. Snapshots stay in the store until pruned."""
    from examlops import dataplane

    if dry_run:
        if _output.json_mode:
            _output.print_json({"dry_run": True, "would_delete": name})
        else:
            _output.info(f"would delete {name}")
        return
    if not _output.confirm(f"Delete dataplane source {name}?", default=False):
        raise typer.Exit(0)
    removed = dataplane.remove_source(name, project, actor=_actor())
    if not removed:
        _output.error(f"source {name!r} not found")
    _output.print_json({"deleted": name}) if _output.json_mode else _output.ok(f"deleted {name}")


@app.command("test")
def test_cmd(name: str, project: str = typer.Option("", "--project", "-p")) -> None:
    """Check that a source's system is reachable and the credentials work (reads no data)."""
    from examlops import dataplane

    try:
        probe = dataplane.probe_source(name, project=project)
    except Exception as exc:
        _fail(exc)
    out = {"name": name, "ok": probe.ok, "detail": probe.detail}
    if _output.json_mode:
        _output.print_json(out)
        return
    (_output.ok if probe.ok else _output.warning)(f"{name}: {probe.detail}")


@app.command("preview")
def preview_cmd(
    name: str,
    project: str = typer.Option("", "--project", "-p"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=1000),
) -> None:
    """Show the first rows a pull would read; nothing is stored."""
    from examlops import dataplane

    try:
        rows = dataplane.preview(name, project=project, limit=limit)
    except Exception as exc:
        _fail(exc)
    if _output.json_mode:
        _output.print_json(rows)
        return
    cols = sorted({k for r in rows for k in r})
    _output.print_table(
        f"Preview of {name}", cols, [[str(r.get(c, "")) for c in cols] for r in rows]
    )


@app.command("pull", epilog=_EX_PULL)
def pull_cmd(
    name: str,
    project: str = typer.Option("", "--project", "-p"),
    full: bool = typer.Option(False, "--full", help="Ignore the watermark; re-read everything"),
    remote: bool = typer.Option(False, "--remote", help="Ask the dataplane service to run it"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be pulled"),
) -> None:
    """Pull a source now and commit a snapshot (or report it unchanged)."""
    from examlops import dataplane

    if dry_run:
        try:
            src = dataplane.get_source_def(name, project)
        except Exception as exc:
            _fail(exc)
        out = {"dry_run": True, "source": src.key, "connector": src.connector, "full": full}
        _output.print_json(out) if _output.json_mode else _output.print_record(out)
        return
    if remote:
        from examlops.cli import _client
        from examlops.cli._config import load_config

        cfg = load_config()
        try:
            resp = _client.post(
                f"{cfg.dataplane_url}/sources/{name}/pull",
                {"project": project, "full": full},
                token=cfg.dataplane_token or "",
            )
        except Exception as exc:
            _fail(exc)
        _output.print_json(resp) if _output.json_mode else _output.ok(
            f"queued pull {resp.get('pull_id')}"
        )
        return
    try:
        r = dataplane.run_pull(name, project=project, full=full, actor=_actor())
    except Exception as exc:
        _fail(exc)
    out = vars(r)
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(
        f"{name}: {r.status} → revision {r.revision[:12] if r.revision else '-'} ({r.row_count} rows)"
    )


@app.command("pulls")
def pulls_cmd(
    source: str | None = typer.Option(None, "--source", "-s"),
    project: str | None = typer.Option(None, "--project", "-p"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=500),
) -> None:
    """Recent pulls, newest first."""
    from examlops.data import dataplane as catalog

    rows = catalog.list_pulls(project=project, source=source, limit=limit)
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        "Dataplane pulls",
        ["id", "source", "status", "revision", "rows", "error"],
        [
            [
                r["id"],
                r["source"],
                r["status"],
                (r.get("revision") or "-")[:12],
                str(r.get("row_count") or "-"),
                (r.get("error") or "")[:60],
            ]
            for r in rows
        ],
    )


@app.command("snapshots")
def snapshots_cmd(name: str, project: str = typer.Option("", "--project", "-p")) -> None:
    """Committed snapshots of a source, newest first."""
    from examlops.data import dataplane as catalog

    rows = [
        {
            "pull_id": r["id"],
            "revision": r["revision"],
            "status": r["status"],
            "rows": r.get("row_count"),
            "finished_at": r.get("finished_at"),
        }
        # Selected as snapshots in SQL — a page of pulls narrowed afterwards would answer about
        # the pulls, and hide every revision behind a run of recent failures.
        for r in catalog.list_snapshots(project=project, source=name, limit=200)
    ]
    if _output.json_mode:
        _output.print_json(rows)
        return
    _output.print_table(
        f"Snapshots of {name}",
        ["revision", "rows", "finished_at"],
        [[r["revision"][:12], str(r["rows"]), str(r["finished_at"])] for r in rows],
    )


@app.command("manifest")
def manifest_cmd(
    name: str,
    revision: str = typer.Argument("latest"),
    project: str = typer.Option("", "--project", "-p"),
) -> None:
    """Show one snapshot's manifest (tables, files, schema, watermark)."""
    from dataclasses import asdict

    from examlops import dataplane

    try:
        store = dataplane.store_from_env()
        m = dataplane.read_manifest(
            store, dataplane.resolve(store, dataplane.source_key(project, name), revision)
        )
    except Exception as exc:
        _fail(exc)
    out = asdict(m)
    _output.print_json(out) if _output.json_mode else _output.print_record(
        {
            k: out[k]
            for k in ("source", "revision", "tables", "row_count", "byte_count", "created_at")
        }
    )


@app.command("prune", epilog=_EX_PRUNE)
def prune_cmd(
    name: str,
    keep: int = typer.Option(5, "--keep", min=1),
    project: str = typer.Option("", "--project", "-p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be removed"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Prune even though the catalog has no revision rows for the source (a lost or "
        "restored platform.db) — revisions training runs used are then NOT protected",
    ),
) -> None:
    """Delete old snapshots; the newest N, the latest and any revision an MLflow run used are kept."""
    from examlops.dataplane.pull import prune_source

    key_hint = f"{project or '_global'}/{name}"
    if not dry_run and not _output.confirm(
        f"Prune snapshots of {name}, keeping {keep}?", default=False
    ):
        raise typer.Exit(0)
    try:
        removed = prune_source(
            project, name, keep=keep, dry_run=dry_run, force=force, actor=_actor()
        )
    except Exception as exc:
        _fail(exc)
    out = {"source": key_hint, "removed": removed, "dry_run": dry_run}
    _output.print_json(out) if _output.json_mode else _output.ok(
        f"{len(removed)} pull(s) {'would be ' if dry_run else ''}removed"
    )


@app.command("catalog-rebuild", epilog=_EX_REBUILD)
def catalog_rebuild(dry_run: bool = typer.Option(False, "--dry-run", help="Count only")) -> None:
    """Rebuild the pull history and revision index from the snapshot store (e.g. a lost platform.db)."""
    from examlops.dataplane.pull import rebuild_catalog

    try:
        report = rebuild_catalog(dry_run=dry_run, actor=_actor())
    except Exception as exc:
        _fail(exc)
    if _output.json_mode:
        _output.print_json(report)
        return
    verb = "would restore" if dry_run else "restored"
    _output.ok(
        f"{report['snapshots']} committed snapshot(s) of {report['sources']} source(s) found; "
        f"{verb} {report['pulls_restored']} pull row(s) and indexed {report['revisions']} "
        "revision(s)"
    )
    for src in report["sources_to_register"]:
        _output.warning(
            f"source {src['project'] or '_global'}/{src['name']} ({src['connector']}, connection "
            f"{src['connection'] or '-'}) has snapshots but no definition — re-register it with "
            "`exa dataplane sources create` (the store keeps only its spec hash)"
        )
