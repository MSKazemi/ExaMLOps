"""The ``exa`` CLI's federated session: token cache, refresh, and ``oidc-agent`` delegation.

``exa auth login`` runs the Device Authorization Grant (RFC 8628) against the center's IdP and
stores the result here; every later ``exa`` call that talks to the control plane or dashboard
picks the access token up automatically (see ``cli/_config.load_config``), refreshing it when it
has expired. An explicitly configured static token always wins.

Storage: ``credentials.json`` next to the CLI config (``EXAMLOPS_CONFIG``'s directory, default
``~/.config/examlops/``), mode ``0600`` in a ``0700`` directory, one record per config context, so
``exa config use lxp`` and ``exa config use jsc`` hold separate logins.

HPC sites that already run **oidc-agent** (common on Helmholtz/EGI infrastructure) need no token
on disk at all: ``exa auth login --oidc-agent <account>`` records only the account name, and each
access token is minted on demand by ``oidc-token <account>``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

_SKEW = 30  # refresh this many seconds before expiry


def credentials_path() -> Path:
    from examlops.cli._config import config_path

    return config_path().parent / "credentials.json"


def _context_key() -> str:
    try:
        from examlops.cli._config import active_context

        return active_context() or "default"
    except Exception:  # noqa: BLE001 — an unreadable config falls back to the default slot
        return "default"


def _read_all() -> dict[str, Any]:
    path = credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(data: dict[str, Any]) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load(context: str | None = None) -> dict[str, Any] | None:
    rec = _read_all().get(context or _context_key())
    return rec if isinstance(rec, dict) else None


def save(record: dict[str, Any], context: str | None = None) -> None:
    data = _read_all()
    data[context or _context_key()] = record
    _write_all(data)


def delete(context: str | None = None) -> bool:
    data = _read_all()
    key = context or _context_key()
    if key not in data:
        return False
    del data[key]
    _write_all(data)
    return True


def record_from_tokens(
    tokens: dict[str, Any], *, issuer: str, client_id: str, provider: str
) -> dict[str, Any]:
    now = int(time.time())
    expires_in = tokens.get("expires_in")
    return {
        "provider": provider,
        "issuer": issuer,
        "client_id": client_id,
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "token_type": tokens.get("token_type", "Bearer"),
        "scope": tokens.get("scope", ""),
        "obtained_at": now,
        "expires_at": now + int(expires_in) if isinstance(expires_in, (int, float)) else None,
    }


def unverified_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload **without verification** — for display only, never for decisions."""
    try:
        import jwt

        return jwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001 — opaque tokens have no readable claims
        return {}


def _oidc_agent_token(account: str) -> str:
    exe = shutil.which("oidc-token")
    if not exe:
        return ""
    try:
        out = subprocess.run(  # noqa: S603 — fixed executable, account passed as one argv item
            [exe, account], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _provider_for(record: dict[str, Any]):
    from examlops.iam.config import ProviderConfig

    issuer = str(record["issuer"])
    return ProviderConfig(
        name=str(record.get("provider") or "cli"),
        issuer=issuer,
        audiences=("cli",),
        discovery=True,
        allow_insecure_http=issuer.startswith(("http://localhost", "http://127.0.0.1")),
    )


def current_access_token(context: str | None = None, *, refresh: bool = True) -> str:
    """A usable access token for the active context, or ``""``. Never raises."""
    rec = load(context)
    if not rec:
        return ""
    if rec.get("oidc_agent_account"):
        return _oidc_agent_token(str(rec["oidc_agent_account"]))
    token = str(rec.get("access_token") or "")
    exp = rec.get("expires_at")
    if token and (exp is None or float(exp) - _SKEW > time.time()):
        return token
    if not refresh or not rec.get("refresh_token"):
        return ""
    try:
        from examlops.iam.config import ClientConfig
        from examlops.iam.flows import refresh as do_refresh

        tokens = do_refresh(
            _provider_for(rec), ClientConfig(str(rec["client_id"])), str(rec["refresh_token"])
        )
    except Exception:  # noqa: BLE001 — a failed refresh means "log in again", not a crash
        return ""
    new = record_from_tokens(
        tokens, issuer=rec["issuer"], client_id=rec["client_id"], provider=rec.get("provider", "")
    )
    # RFC 9700 §4.14: a rotated refresh token replaces the old one; keep the old if none returned.
    new["refresh_token"] = tokens.get("refresh_token") or rec.get("refresh_token")
    save(new, context)
    return str(new["access_token"])
