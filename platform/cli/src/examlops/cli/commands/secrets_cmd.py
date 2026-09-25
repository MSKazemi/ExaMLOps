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
    """Store a secret in the write backend (local store by default; audited).

    EXAMLOPS_SECRETS_WRITE_BACKEND=vault|sops writes to OpenBao/Vault or the SOPS file instead;
    an unreachable manager fails the command rather than writing somewhere else.
    """
    from examlops.secrets import (
        SecretAccessDenied,
        SecretBackendError,
        SecretNotFound,
        set_secret,
        write_backend,
    )

    try:
        version = set_secret(path, value, tenant=tenant, actor=_actor())
    except SecretNotFound as exc:
        _output.error(
            str(exc),
            hint="Generate a key: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'",
        )
        return
    except (SecretBackendError, SecretAccessDenied) as exc:
        _output.error(str(exc))
        return
    _output.ok(f"Stored {path} (tenant {tenant}, {write_backend()}, v{version})")


@app.command("get", epilog=_EX_GET)
def get_cmd(
    path: str = typer.Argument(..., help="Secret path"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    reveal: bool = typer.Option(False, "--reveal", help="Print the plaintext value (dangerous)"),
) -> None:
    """Resolve a secret. Redacts by default; --reveal prints plaintext.

    Also reports the backend that served it (vault/local/env), and warns when a configured
    vault was unreachable — that fallback changes which store the value came from.
    """
    from examlops.secrets import SecretAccessDenied, SecretNotFound, resolve_secret

    try:
        res = resolve_secret(path, tenant=tenant, actor=_actor())
    except (SecretNotFound, SecretAccessDenied) as exc:
        _output.error(str(exc))  # raises typer.Exit(1)
        return
    value, backend, vault_error = res["value"], res["backend"], res.get("vault_error")
    shown = value if reveal else ("•" * 8 + f" ({len(value)} chars)")
    if _output.json_mode:
        _output.print_json(
            {
                "path": path,
                "tenant": tenant,
                "value": value if reveal else None,
                "backend": backend,
                "vault_error": vault_error,
            }
        )
        return
    if vault_error:
        _output.warning(
            f"vault unreachable ({vault_error}) - this value came from the {backend} store, "
            "which may hold something different. Set EXAMLOPS_VAULT_STRICT=1 to fail instead."
        )
    _output.print_record({"path": path, "tenant": tenant, "value": shown, "backend": backend})


@app.command("rotate", epilog=_EX_ROTATE)
def rotate_cmd(
    path: str = typer.Argument(..., help="Secret path"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Rotate a secret to a fresh random value (audited, spec R4)."""
    from examlops.cli._policy_gate import enforce_and_confirm
    from examlops.secrets import (
        SecretAccessDenied,
        SecretBackendError,
        SecretNotFound,
        rotate_secret,
        write_backend,
    )

    if not enforce_and_confirm(
        "secret_rotate",
        {"target": path, "path": path, "tenant": tenant, "actor": _actor()},
        what=f"rotation of secret {path}",
        prompt=f"Rotate secret '{path}'?",
    ):
        return
    try:
        version = rotate_secret(path, tenant=tenant, actor=_actor())
    except (SecretNotFound, SecretBackendError, SecretAccessDenied) as exc:
        _output.error(str(exc))
        return
    _output.ok(f"Rotated {path} in {write_backend()} → v{version} (value re-generated, audited)")


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
        # The exit code, not only the payload. Rotation exists so the previous KEK can be
        # decommissioned; a script that reads the exit code to gate that next step was told a
        # rotation with failures had succeeded, and retiring the old key then makes every secret
        # still wrapped under it permanently unreadable.
        raise typer.Exit(1 if summary["failed"] else 0)
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


@app.command(
    "backends",
    epilog="Examples:\n\n  exa secrets backends\n\n  exa secrets backends --json",
)
def backends_cmd() -> None:
    """Show every secrets backend (vault · sops · local · env): configured, reachable, writes.

    Reports the resolution order, which backend `set`/`rotate` write to, OpenBao/Vault health
    (sealed / initialised, via the unauthenticated health endpoint), the sops binary and file,
    and the local keyring's key ids. Never prints a secret or a key.
    """
    from examlops.secrets import backends_status

    status = backends_status()
    if _output.json_mode:
        _output.print_json(status)
        return
    vault, sops, local = status["vault"], status["sops"], status["local"]

    def _vault_state() -> str:
        if not vault["configured"]:
            return "not configured"
        if not vault["reachable"]:
            return f"UNREACHABLE ({vault['error']})"
        if not vault["initialized"]:
            return "reachable, NOT initialised"
        return "reachable, SEALED" if vault["sealed"] else "reachable, unsealed"

    def _sops_state() -> str:
        if not sops["configured"]:
            return "not configured"
        if sops["error"]:
            return f"ERROR ({sops['error']})"
        return f"{sops['file']} ({sops['version'] or 'sops'})"

    _output.print_table(
        "Secrets backends (resolution order)",
        ["Backend", "State"],
        [
            ["vault", _vault_state()],
            ["sops", _sops_state()],
            [
                "local",
                local["error"]
                or f"keys {', '.join(local['keys'])} (active {local['active_key_id']})",
            ],
            ["env", "last resort"],
        ],
    )
    if status["write_backend_error"]:
        _output.warning(status["write_backend_error"])
    _output.info(
        f"Writes go to: {status['write_backend']}"
        + (" · strict (no fallback on a manager outage)" if status["strict"] else "")
    )


@app.command(
    "refs",
    epilog=("Examples:\n\n  exa secrets refs\n\n  exa secrets refs --env-file .env --strict"),
)
def refs_cmd(
    env_file: str | None = typer.Option(
        None, "--env-file", help="Check a dotenv file instead of this process's environment"
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit 1 on any plaintext credential or unresolvable reference"
    ),
) -> None:
    """Audit an environment for startup injection (ADR 0011 clause 2): references vs plaintext.

    Lists every credential-carrying variable as `reference` (secret://path — resolved through the
    secrets client), `file` (secret+file:///run/secrets/x), `bootstrap` (the store's own key or
    token) or `plaintext` (a credential in clear — what injection replaces), and whether each
    reference resolves. Values are never printed; each resolution is an audited access.
    `--strict` makes it a deploy gate.
    """
    from examlops.secrets.inject import check_env, parse_env_file

    if env_file:
        path = Path(env_file)
        if not path.is_file():
            _output.error(f"env file not found: {env_file}")
            return
        environ = parse_env_file(path.read_text(encoding="utf-8", errors="replace"))
    else:
        environ = dict(os.environ)
    rows = check_env(environ, actor=_actor())
    bad = [r for r in rows if r["kind"] == "plaintext" or r["resolves"] is False]
    if _output.json_mode:
        _output.print_json(rows)
    elif not rows:
        _output.info("No credential-carrying variables found.")
    else:
        _output.print_table(
            "Credential variables (values never shown)",
            ["Variable", "Kind", "Target", "Resolves"],
            [
                [
                    r["name"],
                    r["kind"],
                    r["target"] or "",
                    ""
                    if r["resolves"] is None
                    else (f"yes ({r.get('backend')})" if r["resolves"] else f"NO - {r['error']}"),
                ]
                for r in rows
            ],
        )
    if strict and bad:
        _output.error(
            f"{len(bad)} variable(s) hold a plaintext credential or an unresolvable reference.",
            hint="Store the value (exa secrets set <path> …) and set VAR=secret://<path>.",
            exit_code=1,
        )


# ── dynamic, short-lived credentials (ADR 0011 clause 3) ──────────────────────────────────────

lease_app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Dynamic short-lived credentials from an OpenBao/Vault secrets engine (leases).",
)
app.add_typer(lease_app, name="lease")


@lease_app.command(
    "issue",
    epilog="Examples:\n\n  exa secrets lease issue database/creds/readonly\n\n"
    "  exa secrets lease issue database/creds/readonly --json",
)
def lease_issue_cmd(
    engine_path: str = typer.Argument(..., help="Dynamic engine path, e.g. database/creds/<role>"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
    reveal: bool = typer.Option(False, "--reveal", help="Print the credential fields (dangerous)"),
) -> None:
    """Mint a short-lived credential; prints the lease (id, TTL) and, with --reveal, the fields.

    Needs EXAMLOPS_VAULT_ADDR and a mounted dynamic engine. A path with no lease (a static KV
    secret) is refused rather than presented as short-lived. Audited without the credential.
    """
    from examlops.secrets import SecretAccessDenied
    from examlops.secrets.leases import LeaseError, issue

    try:
        lease = issue(engine_path, actor=_actor(), tenant=tenant)
    except (LeaseError, SecretAccessDenied) as exc:
        _output.error(str(exc))
        return
    if _output.json_mode:
        _output.print_json({**lease, "data": lease["data"] if reveal else None})
        return
    fields = lease["data"] if reveal else {k: "•" * 8 for k in lease["data"]}
    _output.print_record(
        {
            "lease_id": lease["lease_id"],
            "ttl_seconds": str(lease["lease_duration"]),
            "renewable": str(lease["renewable"]),
            **{f"data.{k}": str(v) for k, v in fields.items()},
        }
    )


@lease_app.command(
    "renew",
    epilog="Examples:\n\n  exa secrets lease renew database/creds/readonly/abc123 --increment 3600",
)
def lease_renew_cmd(
    lease_id: str = typer.Argument(..., help="Lease id returned by `lease issue`"),
    increment: int | None = typer.Option(
        None, "--increment", help="Requested extension in seconds (the engine caps it)"
    ),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Extend a lease (bounded by the engine's max TTL); audited."""
    from examlops.secrets import SecretAccessDenied
    from examlops.secrets.leases import LeaseError, renew

    try:
        out = renew(lease_id, increment=increment, actor=_actor(), tenant=tenant)
    except (LeaseError, SecretAccessDenied) as exc:
        _output.error(str(exc))
        return
    if _output.json_mode:
        _output.print_json(out)
        return
    _output.ok(f"Renewed {out['lease_id']} → {out['lease_duration']}s")


@lease_app.command(
    "revoke",
    epilog="Examples:\n\n  exa secrets lease revoke database/creds/readonly/abc123",
)
def lease_revoke_cmd(
    lease_id: str = typer.Argument(..., help="Lease id to revoke now"),
    tenant: str = typer.Option("default", "--tenant", help="Tenant scope"),
) -> None:
    """Revoke a lease now — the credential stops working at the manager; audited."""
    from examlops.cli._policy_gate import enforce_and_confirm
    from examlops.secrets import SecretAccessDenied
    from examlops.secrets.leases import LeaseError, revoke

    if not enforce_and_confirm(
        "secret_lease_revoke",
        {"target": lease_id, "lease_id": lease_id, "actor": _actor()},
        what=f"revocation of lease {lease_id}",
        prompt=f"Revoke lease '{lease_id}'?",
    ):
        return
    try:
        revoke(lease_id, actor=_actor(), tenant=tenant)
    except (LeaseError, SecretAccessDenied) as exc:
        _output.error(str(exc))
        return
    if _output.json_mode:
        _output.print_json({"lease_id": lease_id, "revoked": True})
        return
    _output.ok(f"Revoked {lease_id}")
