"""``exa auth`` — sign in with your data center's identity provider (ADR 0120).

ExaMLOps federates with the identity provider and authorization service the hosting center already
runs, instead of keeping its own user store. From a terminal — including a headless HPC login
node — ``exa auth login`` runs the OAuth 2.0 Device Authorization Grant (RFC 8628): it shows a
short code, you approve it in any browser, and every later ``exa`` call to the control plane or
dashboard carries your own identity instead of a shared token. Sites that already run
``oidc-agent`` can hand token minting to it.

The operator-side commands inspect the platform's trust file (``EXAMLOPS_IAM_CONFIG``):
``providers`` lists the trusted centers, ``validate`` checks the file, ``verify`` shows how a token
maps to a principal (role, tenant, the rules that fired), and ``decide`` asks the same question
every enforcement point asks — including the center's own PDP.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path
from typing import Any

import typer

from examlops.cli import _output

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_EX_LOGIN = (
    "Examples:\n\n"
    "  # A center from the platform's trust file\n"
    "  exa auth login --provider jsc\n\n"
    "  # Any OIDC issuer, with the CLI's public client id\n"
    "  exa auth login --issuer https://login.helmholtz.de/oauth2 --client-id exa-cli\n\n"
    "  # Let oidc-agent mint tokens (nothing stored on disk)\n"
    "  exa auth login --oidc-agent helmholtz"
)
_EX_STATUS = "Examples:\n\n  exa auth status\n\n  exa --json auth status"
_EX_WHOAMI = "Examples:\n\n  exa auth whoami"
_EX_TOKEN = (
    "Examples:\n\n"
    '  curl -H "Authorization: Bearer $(exa auth token)" http://localhost:18002/approvals\n\n'
    "  exa auth token --header"
)
_EX_LOGOUT = "Examples:\n\n  exa auth logout"
_EX_PROVIDERS = "Examples:\n\n  exa auth providers\n\n  exa --json auth providers"
_EX_VALIDATE = (
    "Examples:\n\n"
    "  exa auth validate\n\n"
    "  exa auth validate --file identity-providers.yaml --check-discovery"
)
_EX_VERIFY = (
    "Examples:\n\n"
    "  exa auth token | exa auth verify --token-file -\n\n"
    "  exa auth verify --token-file ./token.jwt"
)
_EX_DECIDE = (
    "Examples:\n\n"
    "  exa auth decide model.promote --resource-type model --resource-id JPCP\n\n"
    "  exa auth decide api.write --token-file ./token.jwt --project proj-a"
)


def _now() -> int:
    return int(time.time())


def _trust_config(strict: bool = False):
    from examlops.iam import IamConfigError, load_config

    try:
        return load_config(strict=strict)
    except IamConfigError as exc:
        _output.error(f"trust file is invalid: {exc}", hint="exa auth validate")


def _resolve_login_target(provider: str | None, issuer: str | None, client_id: str | None):
    """(ProviderConfig, ClientConfig) for the login, from flags → CLI config → trust file."""
    from examlops.cli._config import load_config as load_cli_config
    from examlops.iam.config import ClientConfig, ProviderConfig

    cfg = load_cli_config()
    if provider:
        trust = _trust_config()
        p = trust.by_name(provider) if trust else None
        if p is None:
            _output.error(
                f"no provider {provider!r} in the trust file",
                hint="exa auth providers   ·   or pass --issuer URL",
            )
        client = p.client("cli")
        cid = client_id or (client.client_id if client else "")
        if not cid:
            _output.error(
                f"provider {provider!r} has no CLI client configured",
                hint="pass --client-id, or add clients.cli.client_id to the trust file",
            )
        return p, ClientConfig(cid, None, client.scopes if client else ("openid", "profile"))
    issuer = issuer or cfg.auth_issuer
    if not issuer:
        trust = _trust_config()
        able = [p for p in (trust.providers if trust else ()) if p.client("cli")]
        if len(able) == 1:
            p = able[0]
            c = p.client("cli")
            assert c is not None
            return p, ClientConfig(client_id or c.client_id, None, c.scopes)
        _output.error(
            "no identity provider to sign in with",
            hint="exa auth login --issuer <URL>   ·   exa config set auth_issuer <URL>",
        )
    insecure = issuer.startswith(("http://localhost", "http://127.0.0.1"))
    p = ProviderConfig(
        name=provider or "oidc",
        issuer=issuer,
        audiences=("cli",),
        discovery=True,
        allow_insecure_http=insecure,
    )
    return p, ClientConfig(
        client_id or cfg.auth_client_id or "exa-cli", None, ("openid", "profile", "offline_access")
    )


@app.command("login", epilog=_EX_LOGIN)
def login(
    provider: str = typer.Option(
        None, "--provider", "-p", help="A center named in the platform's trust configuration"
    ),
    issuer: str = typer.Option(None, "--issuer", help="OIDC issuer URL (instead of --provider)"),
    client_id: str = typer.Option(None, "--client-id", help="Public OAuth client id of the CLI"),
    oidc_agent: str = typer.Option(
        None, "--oidc-agent", help="Delegate to an oidc-agent account (tokens never stored here)"
    ),
) -> None:
    """Sign in with your organisation (Device Authorization Grant, RFC 8628)."""
    from examlops.iam import flows, session

    if oidc_agent:
        token = session._oidc_agent_token(oidc_agent)
        if not token:
            _output.error(
                f"oidc-agent could not mint a token for account {oidc_agent!r}",
                hint=f"oidc-add {oidc_agent}   ·   is `oidc-token` on PATH?",
            )
        claims = session.unverified_claims(token)
        session.save(
            {
                "provider": provider or oidc_agent,
                "issuer": claims.get("iss", ""),
                "client_id": "",
                "oidc_agent_account": oidc_agent,
                "obtained_at": _now(),
            }
        )
        _output.ok(f"Signed in through oidc-agent account {oidc_agent!r} (no token stored)")
        return

    p, client = _resolve_login_target(provider, issuer, client_id)
    try:
        device = flows.device_authorize(p, client)
    except Exception as exc:  # noqa: BLE001 — discovery/endpoint failure: say which center
        _output.error(f"cannot start sign-in with {p.label}: {exc}")
    uri = device.get("verification_uri_complete") or device.get("verification_uri")
    # RFC 10027: show the user what they are approving and from where, so a phished code stands out.
    _output.info(f"Signing in the exa CLI on host {socket.gethostname()} to {p.label}.")
    _output.info(f"Open {uri}")
    _output.info(f"and confirm the code: {device.get('user_code')}")
    _output.detail("Waiting for approval… (Ctrl+C to cancel)")
    try:
        tokens = flows.poll_device_token(p, client, device)
    except flows.FlowError as exc:
        _output.error(f"sign-in not completed: {exc}")
    except KeyboardInterrupt:
        _output.error("sign-in cancelled", exit_code=130)
    record = session.record_from_tokens(
        tokens, issuer=p.issuer, client_id=client.client_id, provider=p.name
    )
    record["display_name"] = p.label
    session.save(record)
    claims = session.unverified_claims(str(tokens["access_token"]))
    who = claims.get("preferred_username") or claims.get("email") or claims.get("sub", "?")
    _output.ok(f"Signed in to {p.label} as {who}")
    if not tokens.get("refresh_token"):
        _output.warning("no refresh token issued — you will need to log in again when it expires")


@app.command("logout", epilog=_EX_LOGOUT)
def logout() -> None:
    """Forget this context's session (and revoke its refresh token where the IdP supports it)."""
    from examlops.iam import session

    rec = session.load()
    if rec is None:
        if _output.json_mode:
            _output.print_json({"signed_out": False, "reason": "no session"})
            return
        _output.info("No session to sign out of.")
        return
    revoked = False
    if rec.get("refresh_token") and rec.get("issuer"):
        try:
            import httpx

            from examlops.iam import metadata

            ep = metadata.discovery(session._provider_for(rec)).get("revocation_endpoint")
            if ep:  # RFC 7009 token revocation — best effort, the local session goes regardless
                httpx.post(
                    ep,
                    data={
                        "token": rec["refresh_token"],
                        "token_type_hint": "refresh_token",
                        "client_id": rec.get("client_id", ""),
                    },
                    timeout=metadata.http_timeout(),
                )
                revoked = True
        except Exception:  # noqa: BLE001
            revoked = False
    session.delete()
    if _output.json_mode:
        _output.print_json({"signed_out": True, "refresh_token_revoked": revoked})
        return
    _output.ok("Signed out" + (" (refresh token revoked at the IdP)" if revoked else ""))


def _session_view() -> dict[str, Any]:
    from examlops.iam import session

    rec = session.load()
    if rec is None:
        return {"signed_in": False, "credentials_file": str(session.credentials_path())}
    exp = rec.get("expires_at")
    claims = session.unverified_claims(str(rec.get("access_token") or ""))
    return {
        "signed_in": True,
        "provider": rec.get("provider"),
        "display_name": rec.get("display_name") or rec.get("provider"),
        "issuer": rec.get("issuer"),
        "username": claims.get("preferred_username") or claims.get("email") or claims.get("sub"),
        "via": "oidc-agent" if rec.get("oidc_agent_account") else "device-flow",
        "expires_at": exp,
        "expired": isinstance(exp, (int, float)) and exp <= _now(),
        "refreshable": bool(rec.get("refresh_token")) or bool(rec.get("oidc_agent_account")),
        "credentials_file": str(session.credentials_path()),
    }


@app.command("status", epilog=_EX_STATUS)
def status() -> None:
    """Show whether this config context is signed in, to which IdP, and until when."""
    view = _session_view()
    if _output.json_mode:
        _output.print_json(view)
        return
    if not view["signed_in"]:
        _output.info("Not signed in.")
        _output.hint("exa auth login --provider <center>")
        return
    left = (view["expires_at"] - _now()) if view["expires_at"] else None
    _output.print_record(
        {
            "provider": view["display_name"],
            "issuer": view["issuer"],
            "user": view["username"] or "?",
            "via": view["via"],
            "access token": "expired"
            if view["expired"]
            else (f"valid {left // 60} min" if left is not None else "valid"),
            "refresh": "yes" if view["refreshable"] else "no",
        }
    )


@app.command("token", epilog=_EX_TOKEN)
def token(
    header: bool = typer.Option(False, "--header", help="Print as an Authorization header"),
) -> None:
    """Print a current access token (refreshed if needed) for scripts and curl."""
    from examlops.iam import session

    value = session.current_access_token()
    if not value:
        _output.error("not signed in, or the session could not be refreshed", hint="exa auth login")
    sys.stdout.write((f"Authorization: Bearer {value}" if header else value) + "\n")


def _read_token(token_file: str | None) -> str:
    from examlops.iam import session

    if token_file == "-":
        return sys.stdin.read().strip()
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            _output.error(f"cannot read token file: {exc}")
    value = session.current_access_token()
    if not value:
        _output.error("no token: pass --token-file, or sign in first", hint="exa auth login")
    return value


@app.command("whoami", epilog=_EX_WHOAMI)
def whoami() -> None:
    """Who the platform sees: your verified principal (role, tenant, groups) when it can check."""
    from examlops.iam import AuthenticationError, session, verify_access_token

    view = _session_view()
    out: dict[str, Any] = {"signed_in": view["signed_in"]}
    if view["signed_in"]:
        value = session.current_access_token()
        trust = None
        try:
            from examlops.iam import load_config

            trust = load_config()
        except Exception:  # noqa: BLE001 — an unreadable trust file just means "unverified"
            trust = None
        if value and trust is not None and trust.enabled:
            try:
                out.update({"verified": True, **verify_access_token(value, trust).summary()})
            except AuthenticationError as exc:
                out.update({"verified": False, "error": exc.reason})
        else:
            claims = session.unverified_claims(value) if value else {}
            out.update(
                {
                    "verified": False,
                    "note": "no trust file here — claims shown as the token states them",
                    "issuer": claims.get("iss"),
                    "subject": claims.get("sub"),
                    "username": claims.get("preferred_username"),
                    "email": claims.get("email"),
                    "expires_at": claims.get("exp"),
                }
            )
    if _output.json_mode:
        _output.print_json(out)
        return
    if not out["signed_in"]:
        _output.info("Not signed in.")
        _output.hint("exa auth login --provider <center>")
        return
    _output.print_record({k: v for k, v in out.items() if v not in (None, [], {}, "")})


def _provider_rows(cfg) -> list[dict[str, Any]]:
    rows = []
    for p in cfg.providers:
        rows.append(
            {
                "name": p.name,
                "display_name": p.label,
                "issuer": p.issuer,
                "audience": list(p.audiences),
                "tenant": p.tenant or f"claim:{p.tenant_claim}",
                "authorization": p.authorization_mode,
                "pdp": f"{p.pdp.type} {p.pdp.url}" if p.pdp else None,
                "clients": sorted(p.clients),
                "step_up": list(p.step_up.acr_values) if p.step_up.enabled else None,
                "token_types": ["jwt"] + (["opaque"] if p.introspection else []),
            }
        )
    return rows


@app.command("providers", epilog=_EX_PROVIDERS)
def providers() -> None:
    """List the identity providers (data centers) this platform trusts."""
    from examlops.iam import IamConfigError, load_config

    try:
        cfg = load_config()
    except IamConfigError as exc:
        if _output.json_mode:
            _output.print_json({"providers": [], "error": str(exc)})
            return
        _output.error(f"trust file is invalid: {exc}", hint="exa auth validate")
    rows = _provider_rows(cfg)
    if _output.json_mode:
        _output.print_json({"source": cfg.source, "providers": rows})
        return
    if not rows:
        _output.info("Identity federation is off — no trust file (EXAMLOPS_IAM_CONFIG).")
        _output.hint("see docs/guides/identity-federation.md")
        return
    _output.print_table(
        f"Trusted identity providers ({cfg.source})",
        ["Name", "Center", "Issuer", "Tenant", "Authorization", "Clients"],
        [
            [
                r["name"],
                r["display_name"],
                r["issuer"],
                r["tenant"],
                r["authorization"],
                ",".join(r["clients"]),
            ]
            for r in rows
        ],
    )


@app.command("validate", epilog=_EX_VALIDATE)
def validate(
    file: str = typer.Option(
        None, "--file", "-f", help="Trust file path (default: $EXAMLOPS_IAM_CONFIG)"
    ),
    check_discovery: bool = typer.Option(
        False, "--check-discovery", help="Also fetch each issuer's discovery document"
    ),
) -> None:
    """Validate a trust file; exit 1 on any error (a CI gate for identity config)."""
    from examlops.iam import metadata
    from examlops.iam.config import config_path, load_file

    path = Path(file).expanduser() if file else config_path()
    if path is None:
        _output.error("no trust file", hint="pass --file or set EXAMLOPS_IAM_CONFIG")
    cfg, errors = load_file(path)
    checks: list[dict[str, Any]] = []
    if check_discovery and not errors:
        for p in cfg.providers:
            try:
                doc = metadata.discovery(p)
                checks.append(
                    {"provider": p.name, "discovery": "ok", "jwks_uri": doc.get("jwks_uri")}
                )
            except metadata.MetadataError as exc:
                errors.append(f"providers[{p.name}]: discovery failed: {exc}")
                checks.append({"provider": p.name, "discovery": "fail"})
    result = {
        "file": str(path),
        "valid": not errors,
        "providers": [p.name for p in cfg.providers],
        "errors": errors,
        "discovery": checks,
    }
    if _output.json_mode:
        _output.print_json(result)
        if errors:
            raise typer.Exit(1)
        return
    if errors:
        for e in errors:
            _output.warning(e)
        _output.error(f"{path}: {len(errors)} error(s)")
    names = ", ".join(p.name for p in cfg.providers)
    _output.ok(f"{path}: {len(cfg.providers)} provider(s) valid ({names})")


@app.command("verify", epilog=_EX_VERIFY)
def verify(
    token_file: str = typer.Option(
        None, "--token-file", help="File holding the token ('-' = stdin; default: your session)"
    ),
    provider: str = typer.Option(None, "--provider", help="Provider for an opaque token"),
) -> None:
    """Verify a token against the trust file and show the principal it maps to."""
    from examlops.iam import AuthenticationError, verify_access_token

    value = _read_token(token_file)
    try:
        principal = verify_access_token(value, provider_hint=provider)
    except AuthenticationError as exc:
        if _output.json_mode:
            _output.print_json({"valid": False, "error": exc.reason})
            raise typer.Exit(1) from exc
        _output.error(f"token rejected: {exc.reason}")
    summary = {"valid": True, **principal.summary()}
    if _output.json_mode:
        _output.print_json(summary)
        return
    _output.print_record(
        {
            "principal": principal.id,
            "actor": principal.actor,
            "tenant": principal.tenant,
            "role": principal.role or "none — authenticated but not authorized",
            "projects": principal.projects or None,
            "acr": principal.acr,
            "expires": principal.expires_at,
        }
    )
    for why in principal.matched:
        _output.detail(f"rule: {why}")


@app.command("decide", epilog=_EX_DECIDE)
def decide(
    action: str = typer.Argument(
        ..., help="Action, e.g. model.promote, api.write, retrain.trigger"
    ),
    resource_type: str = typer.Option("platform", "--resource-type", help="Resource type"),
    resource_id: str = typer.Option(None, "--resource-id", help="Resource id"),
    tenant: str = typer.Option(None, "--tenant", help="Resource tenant (default: yours)"),
    project: str = typer.Option(None, "--project", help="Resource project"),
    token_file: str = typer.Option(
        None, "--token-file", help="File holding the token ('-' = stdin; default: your session)"
    ),
) -> None:
    """Ask the platform's authorizer — tenant, local policy, the center's PDP — about an action."""
    from examlops.iam import AuthenticationError, authorize, verify_access_token

    value = _read_token(token_file)
    try:
        principal = verify_access_token(value)
    except AuthenticationError as exc:
        _output.error(f"token rejected: {exc.reason}")
    resource: dict[str, Any] = {"type": resource_type}
    if resource_id:
        resource["id"] = resource_id
    if tenant:
        resource["tenant"] = tenant
    if project:
        resource["project"] = project
    d = authorize(principal, action, resource, audit=False)
    out = {"principal": principal.id, "action": action, "resource": resource, **d.as_dict()}
    if _output.json_mode:
        _output.print_json(out)
    elif d.allowed:
        _output.ok(f"ALLOW {action} for {principal.actor} — {d.reason} [{d.layer}]")
    else:
        _output.warning(f"DENY {action} for {principal.actor} — {d.reason} [{d.layer}]")
    if not d.allowed:
        raise typer.Exit(1)


__all__ = ["app"]
