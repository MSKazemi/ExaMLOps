"""D7 — Secrets management & rotation (ADR 0011, spec D7-secrets-management).

One client over three backends, tried in order:

1. **OpenBao/Vault** — when ``EXAMLOPS_VAULT_ADDR`` is set (best-effort HTTP KV).
2. **Local encrypted store** — Fernet-encrypted values in ``platform_db.secrets_store``
   (the fallback; works with no running Vault, spec R1).
3. **Environment variable** — last resort for bootstrap creds.

A missing/denied secret **fails fast** with a clear, non-leaking error (spec R3).
Access, writes, and rotations are audited (spec R4/R6). Secrets are scoped by
``tenant`` (spec R5). A built-in scanner (:func:`scan_text`) backs the CI gate (R10).
"""

from __future__ import annotations

import os
import re
import secrets as _pysecrets
from typing import Any

_TRUTHY = {"1", "true", "yes", "on"}


class SecretNotFound(RuntimeError):
    """Raised when a required secret cannot be resolved (fail-fast, spec R3)."""


class SecretAccessDenied(RuntimeError):
    """Raised when a tenant is not permitted to read a secret path (spec R5)."""


# --- envelope encryption keyring (2.3) ---------------------------------------
# A KEK keyring keyed by ``key_id`` so KEKs can be rotated online: new secrets are wrapped under
# the ACTIVE key (its id stored per-row); old ciphertext still decrypts under its own key until a
# `rewrap` migrates it. ``EXAMLOPS_SECRETS_KEYS`` = comma-separated ``key_id:fernet_key`` pairs;
# ``EXAMLOPS_SECRETS_ACTIVE_KEY`` names the write key. Legacy single-key envs remain as fallbacks so
# existing stores keep decrypting (and `DASHBOARD_SECRET_KEY` is no longer the *primary* KEK).


def _keyring() -> dict[str, str]:
    """Parse the ``key_id -> fernet_key`` map from env (keyring + legacy single-key fallbacks)."""
    keys: dict[str, str] = {}
    raw = os.getenv("EXAMLOPS_SECRETS_KEYS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        kid, key = pair.split(":", 1)
        if kid.strip() and key.strip():
            keys[kid.strip()] = key.strip()
    # Legacy single-key envs get stable synthetic ids so their ciphertext stays decryptable.
    if primary := os.getenv("EXAMLOPS_SECRETS_KEY"):
        keys.setdefault("primary", primary)
    if legacy := os.getenv("DASHBOARD_SECRET_KEY"):
        keys.setdefault("legacy-dashboard", legacy)
    return keys


def _active_key_id() -> str:
    """The key_id used to wrap NEW secrets."""
    keys = _keyring()
    if not keys:
        raise SecretNotFound(
            "no encryption key: set EXAMLOPS_SECRETS_KEYS (key_id:fernet_key,...) + "
            "EXAMLOPS_SECRETS_ACTIVE_KEY, or EXAMLOPS_SECRETS_KEY, to use the local store"
        )
    explicit = os.getenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "").strip()
    if explicit:
        if explicit not in keys:
            raise SecretNotFound(f"active key_id '{explicit}' is not in the keyring")
        return explicit
    if "primary" in keys:
        return "primary"
    # Single-key keyring → that key; otherwise ambiguous.
    if len(keys) == 1:
        return next(iter(keys))
    raise SecretNotFound(
        "multiple keys configured but EXAMLOPS_SECRETS_ACTIVE_KEY is unset — cannot pick a write key"
    )


def _fernet_for(key_id: str):
    from cryptography.fernet import Fernet

    keys = _keyring()
    if key_id not in keys:
        raise SecretNotFound(
            f"no key for key_id '{key_id}' — cannot decrypt (rotate/rewrap needed)"
        )
    k = keys[key_id]
    return Fernet(k.encode() if isinstance(k, str) else k)


def _encrypt(value: str) -> tuple[str, str]:
    """Encrypt ``value`` under the active KEK; returns ``(ciphertext, key_id)``."""
    kid = _active_key_id()
    return _fernet_for(kid).encrypt(value.encode()).decode(), kid


def _decrypt(ciphertext: str, key_id: str | None) -> str:
    """Decrypt using the stored key_id; fall back to trying every keyring key (legacy rows)."""
    from cryptography.fernet import Fernet, MultiFernet

    if key_id:
        try:
            return _fernet_for(key_id).decrypt(ciphertext.encode()).decode()
        except SecretNotFound:
            pass  # key_id not in keyring — try them all below
    keys = _keyring()
    if not keys:
        raise SecretNotFound("no encryption key available to decrypt the secret")
    mf = MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys.values()])
    return mf.decrypt(ciphertext.encode()).decode()


def _audit(action: str, path: str, actor: str | None, extra: dict | None = None) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event("exa-secrets", actor, action, path, extra or {})
    except Exception:
        pass  # audit must never block a secret operation


def _tenant_allowed(path: str, tenant: str) -> bool:
    """Path-prefix tenant scoping (spec R5).

    A ``tenant/...``-prefixed path is readable only by that tenant (or the ``admin``
    tenant). Unprefixed paths are shared. D6 policy can tighten this later.
    """
    if tenant == "admin":
        return True
    parts = path.split("/", 1)
    if len(parts) == 2 and parts[0] in _known_tenant_prefixes():
        return parts[0] == tenant
    return True


def _known_tenant_prefixes() -> set[str]:
    # Any path segment used as a tenant prefix; kept minimal + overridable.
    return {p.strip() for p in os.getenv("EXAMLOPS_SECRET_TENANTS", "").split(",") if p.strip()}


# --- backends ----------------------------------------------------------------


def _vault_get(path: str) -> str | None:
    addr = os.getenv("EXAMLOPS_VAULT_ADDR", "").strip()
    if not addr:
        return None
    try:
        import json
        import urllib.request

        token = os.getenv("EXAMLOPS_VAULT_TOKEN", "")
        url = f"{addr.rstrip('/')}/v1/secret/data/{path}"
        req = urllib.request.Request(url, headers={"X-Vault-Token": token})
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - operator-configured
            data = json.loads(resp.read().decode())
        return data["data"]["data"]["value"]
    except Exception:
        return None  # fall through to local store


# --- public API --------------------------------------------------------------


def get_secret(path: str, *, tenant: str = "default", actor: str | None = None) -> str:
    """Resolve a secret (spec R1-R3, R5-R6). Fails fast if absent."""
    if not _tenant_allowed(path, tenant):
        _audit("secret_denied", path, actor, {"tenant": tenant})
        raise SecretAccessDenied(f"tenant '{tenant}' may not read secret '{path}'")
    _audit("secret_access", path, actor, {"tenant": tenant})

    val = _vault_get(path)
    if val is not None:
        return val

    from examlops.data.secrets import get_secret_record

    rec = get_secret_record(path, tenant)
    if rec is not None:
        return _decrypt(rec["ciphertext"], rec.get("key_id"))

    env_key = path.replace("/", "_").replace("-", "_").upper()
    if env_key in os.environ:
        return os.environ[env_key]

    raise SecretNotFound(f"secret '{path}' not found (tenant '{tenant}')")


def set_secret(path: str, value: str, *, tenant: str = "default", actor: str | None = None) -> int:
    """Store an encrypted secret in the local store (audited). Returns its version."""
    from examlops.data.secrets import put_secret_ciphertext

    ct, key_id = _encrypt(value)
    version = put_secret_ciphertext(path, tenant, ct, updated_by=actor, key_id=key_id)
    _audit("secret_set", path, actor, {"tenant": tenant, "version": version, "key_id": key_id})
    return version


def rotate_secret(
    path: str,
    *,
    tenant: str = "default",
    actor: str | None = None,
    new_value: str | None = None,
) -> int:
    """Rotate a secret to ``new_value`` (or a fresh random token). Audited (spec R4)."""
    value = new_value or _pysecrets.token_urlsafe(32)
    version = set_secret(path, value, tenant=tenant, actor=actor)
    _audit("secret_rotate", path, actor, {"tenant": tenant, "version": version})
    return version


def list_secrets(tenant: str | None = None) -> list[dict]:
    """List secret metadata — never values (spec: no plaintext leak)."""
    from examlops.data.secrets import list_secret_paths

    return list_secret_paths(tenant)


def rewrap_secrets(*, actor: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Re-encrypt every local secret under the ACTIVE KEK (online key rotation, 2.3).

    The rewrap job for envelope encryption: decrypt each stored secret with whatever key it's
    currently wrapped under, re-encrypt under the active key, and persist the new ciphertext +
    key_id — plaintext never leaves this process and versions bump so consumers see a change.
    Idempotent: rows already on the active key are skipped. Returns a summary.
    """
    from examlops.data.secrets import all_secret_records, put_secret_ciphertext

    active = _active_key_id()
    records = all_secret_records()
    rewrapped = skipped = failed = 0
    errors: list[str] = []
    for rec in records:
        if rec.get("key_id") == active:
            skipped += 1
            continue
        try:
            plaintext = _decrypt(rec["ciphertext"], rec.get("key_id"))
            if not dry_run:
                ct, _ = _encrypt(plaintext)
                put_secret_ciphertext(
                    rec["path"], rec["tenant"], ct, updated_by=actor, key_id=active
                )
            rewrapped += 1
        except Exception as exc:  # noqa: BLE001 - report per-secret, don't abort the whole job
            failed += 1
            errors.append(f"{rec['tenant']}/{rec['path']}: {exc}")
    summary = {
        "active_key_id": active,
        "total": len(records),
        "rewrapped": rewrapped,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "dry_run": dry_run,
    }
    if not dry_run:
        _audit(
            "secrets_rewrap",
            "*",
            actor,
            {k: summary[k] for k in ("active_key_id", "rewrapped", "failed")},
        )
    return summary


# --- built-in secret scanner (CI gate, spec R10) -----------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "generic-api-key",
        re.compile(r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*['\"][0-9a-zA-Z\-_]{16,}['\"]"),
    ),
    ("slack-token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("fernet-ish", re.compile(r"gAAAAA[0-9A-Za-z_\-]{20,}")),
]


def scan_text(text: str) -> list[dict]:
    """Return findings (rule, line, match-preview) for likely secrets in ``text``."""
    findings: list[dict] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for rule, pat in _SECRET_PATTERNS:
            m = pat.search(line)
            if m:
                snippet = m.group(0)
                preview = snippet[:6] + "…" if len(snippet) > 6 else snippet
                findings.append({"rule": rule, "line": i, "preview": preview})
    return findings
