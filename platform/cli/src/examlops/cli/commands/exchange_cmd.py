"""``exa exchange`` — signed cross-institution packages (Phase 5 item 5.6)."""

from __future__ import annotations

import typer

from examlops.cli import _output

app = typer.Typer(no_args_is_help=True, help="NovaFabric Exchange — signed shareable packages")


@app.command("pack")
def pack(
    kind: str = typer.Argument(..., help="model | pipeline | provider | policy"),
    name: str = typer.Argument(..., help="Package name"),
    files: list[str] = typer.Option(..., "--file", "-f", help="File to include (repeatable)"),
    out: str = typer.Option(..., "--out", "-o", help="Output .novapack path"),
    version: str = typer.Option("1", "--version", help="Package version"),
) -> None:
    """Build a signed .novapack (fails closed without EXAMLOPS_SIGNING_KEY)."""
    from examlops.exchange import ExchangeError
    from examlops.exchange import pack as _pack

    try:
        manifest = _pack(kind, name, list(files), out, version=version)
    except ExchangeError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    _output.ok(
        f"Packed {kind} '{name}' v{version} → {out} "
        f"({len(manifest['files'])} file(s), digest {manifest['digest'][:12]}…, signed)."
    )


@app.command("verify")
def verify(pack_path: str = typer.Argument(..., help="Path to a .novapack")) -> None:
    """Verify a package's signature + file integrity. Exit 1 if untrusted/tampered."""
    from examlops.exchange import verify as _verify

    result = _verify(pack_path)
    if _output.json_mode:
        _output.print_json({"ok": result.ok, "reason": result.reason, "manifest": result.manifest})
    elif result.ok:
        _output.ok(f"Package verified: {pack_path}")
    else:
        _output.error(f"Package FAILED verification: {result.reason}")
    if not result.ok:
        raise typer.Exit(1)


@app.command("inspect")
def inspect(pack_path: str = typer.Argument(..., help="Path to a .novapack")) -> None:
    """Show a package's manifest without importing it."""
    from examlops.exchange import inspect as _inspect

    _output.print_json(_inspect(pack_path))


@app.command("import")
def import_(
    pack_path: str = typer.Argument(..., help="Path to a .novapack"),
    dest: str = typer.Option(..., "--dest", "-d", help="Directory to extract into"),
) -> None:
    """Verify-before-import: verify signature + integrity, then extract. Refuses unverified."""
    from examlops.exchange import ExchangeError, import_pack

    try:
        manifest = import_pack(pack_path, dest)
    except ExchangeError as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    _output.ok(f"Imported {manifest['kind']} '{manifest['name']}' → {dest} (verified).")
