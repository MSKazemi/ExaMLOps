"""``exa secrets`` — secrets management, rotation, and scanning (Next-Gen 40 · D7, ADR 0011).

Encrypted local store + OpenBao/env fallback; rotation and access are audited (D4);
``exa secrets scan`` is a built-in leak gate for CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from examlops.cli import _output

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EX_SET = "Examples:\n\n  exa secrets set control-plane/token s3cr3t\n\n  exa secrets set acme/api-key k --tenant acme"
_EX_GET = "Examples:\n\n  exa secrets get control-plane/token"
_EX_ROTATE = "Examples:\n\n  exa secrets rotate control-plane/token"
_EX_SCAN = "Examples:\n\n  exa secrets scan .env\n\n  exa secrets scan platform/"


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "cli"


@app.command("set", epilog=_EX_SET)
def set_cmd(
    path: str = typer.Argument(..., help="Secret path (e.g. control-plane/token)"),
    value: str = typer.Argument(..., help="Secret value (stored encrypted)"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Store an encrypted secret in the local store (audited)."""
    from examlops.secrets import SecretNotFound, set_secret

    try:
        version = set_secret(path, value, tenant=tenant, actor=_actor())
    except SecretNotFound as exc:
        _output.error(
            str(exc),
            hint="Generate a key: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'",
        )
        return
    _output.ok(f"Stored [bold]{path}[/bold] (tenant {tenant}, v{version})")


@app.command("get", epilog=_EX_GET)
def get_cmd(
    path: str = typer.Argument(..., help="Secret path"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    reveal: bool = typer.Option(False, "--reveal", help="Print the plaintext value (dangerous)"),
) -> None:
    """Resolve a secret. Redacts by default; --reveal prints plaintext."""
    from examlops.secrets import SecretAccessDenied, SecretNotFound, get_secret

    try:
        value = get_secret(path, tenant=tenant, actor=_actor())
    except (SecretNotFound, SecretAccessDenied) as exc:
        _output.error(str(exc))
        return
    shown = value if reveal else ("•" * 8 + f" ({len(value)} chars)")
    if _output.json_mode:
        _output.print_json({"path": path, "tenant": tenant, "value": value if reveal else None})
        return
    _output.print_record({"path": path, "tenant": tenant, "value": shown})


@app.command("rotate", epilog=_EX_ROTATE)
def rotate_cmd(
    path: str = typer.Argument(..., help="Secret path"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Rotate a secret to a fresh random value (audited, spec R4)."""
    from examlops.secrets import SecretNotFound, rotate_secret

    try:
        version = rotate_secret(path, tenant=tenant, actor=_actor())
    except SecretNotFound as exc:
        _output.error(str(exc))
        return
    _output.ok(f"Rotated [bold]{path}[/bold] → v{version} (value re-generated, audited)")


@app.command("rewrap")
def rewrap_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would rewrap; change nothing"
    ),
) -> None:
    """Re-encrypt every local secret under the ACTIVE KEK (online key rotation, item 2.3).

    Run after adding a new key to EXAMLOPS_SECRETS_KEYS and pointing EXAMLOPS_SECRETS_ACTIVE_KEY at
    it: secrets migrate to the new key so the old one can be decommissioned. Plaintext never leaves
    the process; the operation is audited.
    """
    from examlops.secrets import SecretNotFound, rewrap_secrets

    if not dry_run:
        from examlops.backup import auto_backup_before

        auto_backup_before("secrets-rewrap")  # rollback point; best-effort, never blocks
    try:
        summary = rewrap_secrets(actor=_actor(), dry_run=dry_run)
    except SecretNotFound as exc:
        _output.error(str(exc))
        raise typer.Exit(1) from exc
    if _output.json_mode:
        _output.print_json(summary)
        return
    verb = "Would rewrap" if dry_run else "Rewrapped"
    _output.ok(
        f"{verb} {summary['rewrapped']}/{summary['total']} secret(s) under key "
        f"'{summary['active_key_id']}' ({summary['skipped']} already current, "
        f"{summary['failed']} failed)."
    )
    if summary["failed"]:
        for e in summary["errors"]:
            _output.error(e)
        raise typer.Exit(1)


@app.command("list", epilog="Examples:\n\n  exa secrets list\n\n  exa secrets list --tenant acme")
def list_cmd(
    tenant: str | None = typer.Option(None, "--tenant", help="Filter by tenant"),
) -> None:
    """List secret metadata (paths/versions) — never values."""
    from examlops.secrets import list_secrets

    rows = list_secrets(tenant)
    if _output.json_mode:
        _output.print_json(rows)
        return
    if not rows:
        _output.info("No secrets stored.")
        return
    _output.print_table(
        "Secrets (metadata only)",
        ["Path", "Tenant", "Version", "Updated"],
        [[r["path"], r["tenant"], str(r["version"]), (r["updated_at"] or "")[:19]] for r in rows],
    )


# Vendored/generated directories that hold third-party fixtures — never the
# project's own secrets, and full of false positives. Skipped by the scanner.
_SCAN_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "coverage",
    "htmlcov",
    "site-packages",
    "egg-info",
}
_SCAN_SKIP_SUFFIXES = {".lock", ".min.js", ".map", ".png", ".jpg", ".svg", ".woff", ".woff2"}


def _scan_candidates(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    out: list[Path] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if any(part in _SCAN_SKIP_DIRS or part.endswith(".egg-info") for part in f.parts):
            continue
        if f.suffix in _SCAN_SKIP_SUFFIXES or f.name in {"package-lock.json", "uv.lock"}:
            continue
        out.append(f)
    return sorted(out)


@app.command("scan", epilog=_EX_SCAN)
def scan_cmd(
    target: str = typer.Argument(..., help="File or directory to scan for secrets"),
) -> None:
    """Scan a file/dir for likely secrets; exit non-zero on any finding (CI gate, R10)."""
    from examlops.secrets import scan_text

    files = _scan_candidates(Path(target))
    findings: list[dict] = []
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        for hit in scan_text(text):
            findings.append({"file": str(f), **hit})
    if _output.json_mode:
        _output.print_json(findings)
    elif not findings:
        _output.ok(f"No secrets detected in {target}.")
    else:
        _output.print_table(
            "Potential secrets",
            ["File", "Rule", "Line", "Preview"],
            [[x["file"], x["rule"], str(x["line"]), x["preview"]] for x in findings],
        )
    if findings:
        _output.error(f"{len(findings)} potential secret(s) found — commit blocked.", exit_code=1)
