"""D7 — Secrets management & rotation (ADR 0011, spec D7-secrets-management).

One client over four backends, tried in order:

1. **OpenBao/Vault** — when ``EXAMLOPS_VAULT_ADDR`` is set (HTTP KV v2).
2. **SOPS + age** — when ``EXAMLOPS_SOPS_FILE`` is set: an encrypted, committable document
   decrypted per key by ``sops`` (the ADR's dev/CI fallback tier, :mod:`examlops.secrets.sops`).
3. **Local encrypted store** — Fernet-encrypted values in ``platform_db.secrets_store``
   (works with no running Vault and no sops binary, spec R1).
4. **Environment variable** — last resort for bootstrap creds.

Writes (``set``/``rotate``) go to ONE store, chosen by ``EXAMLOPS_SECRETS_WRITE_BACKEND``
(``local`` default · ``vault`` · ``sops``) — so a rotation lands in the manager of record rather
than in a local copy the manager still overrides. A write to a manager that cannot be reached
fails; it never silently lands somewhere else.

Services resolve ``secret://<path>`` references in their environment at startup through
:mod:`examlops.secrets.inject` (ADR 0011 clause 2).

A missing/denied secret **fails fast** with a clear, non-leaking error (spec R3).
Access, writes, and rotations are audited (spec R4/R6). Secrets are scoped by
``tenant`` (spec R5). A built-in scanner (:func:`scan_text`) backs the CI gate (R10).
"""

from __future__ import annotations

import logging
import os
import re
import secrets as _pysecrets
from typing import Any

_TRUTHY = {"1", "true", "yes", "on"}

log = logging.getLogger("examlops.secrets")


class SecretNotFound(RuntimeError):
    """Raised when a required secret cannot be resolved (fail-fast, spec R3)."""


class SecretAccessDenied(RuntimeError):
    """Raised when a tenant is not permitted to read a secret path (spec R5)."""


class SecretBackendError(RuntimeError):
    """Raised when the configured write backend cannot store a secret (fail closed)."""


class InvalidSecretPath(SecretNotFound):
    """A secret path with an empty, ``.`` or ``..`` segment, or a control character.

    The tenant check reads the path's first segment, but OpenBao/Vault (a Go server) *cleans* a
    request path — ``acme/../globex/key`` and ``/globex/key`` are redirected to ``globex/key`` — so
    a path that passes the prefix check as tenant ``acme`` (or as an unprefixed, shared path) could
    otherwise be served another tenant's secret. Such a path is refused before any backend is asked.
    """


def _check_path(path: str) -> None:
    segments = path.split("/")
    if (
        not path
        or any(seg in {"", ".", ".."} for seg in segments)
        or any(ord(ch) < 0x20 or ch in "\\\x7f" for ch in path)
    ):
        raise InvalidSecretPath(f"invalid secret path {path!r}")


WRITE_BACKENDS = ("local", "vault", "sops")


def write_backend() -> str:
    """The store ``set``/``rotate`` write to (``EXAMLOPS_SECRETS_WRITE_BACKEND``)."""
    chosen = os.getenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "local").strip().lower() or "local"
    if chosen not in WRITE_BACKENDS:
        raise SecretBackendError(
            f"EXAMLOPS_SECRETS_WRITE_BACKEND={chosen!r} is not one of {', '.join(WRITE_BACKENDS)}"
        )
    return chosen


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


def _audit(
    action: str,
    path: str,
    actor: str | None,
    extra: dict | None = None,
    *,
    source: str = "exa-secrets",
) -> None:
    # `audit_best_effort` keeps the half this comment is about — a secret operation is never
    # blocked by the audit log — and drops the half that hid the loss. A lost record of a secret
    # read is exactly what an auditor asks for, and the hash chain cannot show a row that never
    # arrived, so the loss is logged and counted instead of passed over.
    from examlops.data.audit import audit_best_effort

    # The tenant goes on the row, not only in the details, so a per-tenant reading of the trail
    # (ADR 0027's rotation evidence, `exa audit --tenant`) sees it.
    tenant = str((extra or {}).get("tenant") or "default")
    audit_best_effort(source, actor, action, path, extra or {}, tenant=tenant)


def _tenant_allowed(path: str, tenant: str) -> bool:
    """Path-prefix tenant scoping (spec R5).

    A ``tenant/...``-prefixed path is readable only by that tenant (or the ``admin``
    tenant). Unprefixed paths (no ``/``) are shared. D6 policy can tighten this later.

    Under multitenancy (``EXAMLOPS_MULTITENANCY`` truthy) this **fails closed** (C9): *any*
    ``<seg>/...`` prefix is treated as tenant-scoped, whether or not ``seg`` is registered in
    ``EXAMLOPS_SECRET_TENANTS`` — otherwise a site that enabled multitenancy but never set
    that variable had no isolation at all. Single-tenant mode keeps the legacy behaviour
    (only registered prefixes are scoped) so existing shared ``a/b``-style paths still work.
    """
    if tenant == "admin":
        return True
    parts = path.split("/", 1)
    if len(parts) != 2 or not parts[0]:
        return True  # unprefixed → shared
    if parts[0] in _known_tenant_prefixes():
        return parts[0] == tenant
    from examlops.authz import multitenancy_enabled

    if multitenancy_enabled():
        return parts[0] == tenant  # fail closed on any prefix mismatch under multitenancy
    return True


def _tenant_may_write_shared(path: str, tenant: str) -> bool:
    """May ``tenant`` write ``path`` in a store every tenant shares (vault / sops)?

    A read of an unprefixed path is allowed to everyone — it is a shared secret. A *write* of one
    in the vault or SOPS document replaces that shared secret for every tenant, so under
    multitenancy only ``admin`` may do it; any other tenant writes under its own prefix. (The
    local store is keyed by tenant, so a write there cannot reach another tenant's value.)
    """
    if not _tenant_allowed(path, tenant):
        return False
    if tenant == "admin" or "/" in path:
        return True
    from examlops.authz import multitenancy_enabled

    return not multitenancy_enabled()


def _known_tenant_prefixes() -> set[str]:
    # Any path segment used as a tenant prefix; kept minimal + overridable.
    return {p.strip() for p in os.getenv("EXAMLOPS_SECRET_TENANTS", "").split(",") if p.strip()}


# --- backends ----------------------------------------------------------------


def _vault_get(path: str) -> tuple[str | None, str | None]:
    """``(value, error)`` from the vault — ``error`` is set only when it *failed to answer*.

    Two very different outcomes used to collapse into a bare ``None``: "the vault answered,
    this secret is not in it" (a 404 — falling through to the local store is exactly right)
    and "the vault is down, or refused my token" (an outage — falling through serves a value
    from a *different trust domain*, possibly a stale one). Only the second is a degradation,
    so only the second returns an ``error`` for the caller to report.
    """
    addr = os.getenv("EXAMLOPS_VAULT_ADDR", "").strip()
    if not addr:
        return None, None
    try:
        import json
        import urllib.request

        req = urllib.request.Request(_vault_data_url(addr, path), headers=_vault_headers())
        with urllib.request.urlopen(req, timeout=_vault_timeout()) as resp:  # noqa: S310 - operator-configured
            data = json.loads(resp.read().decode())
        return data["data"]["data"]["value"], None
    except Exception as exc:
        import urllib.error

        if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
            return None, None  # the vault answered: not here. Not a degradation.
        return None, f"{type(exc).__name__}: {exc}"


def _vault_data_url(addr: str, path: str) -> str:
    """KV v2 data URL for ``path`` — percent-encoded, so ``?``/``#``/``%`` stay inside the path."""
    from urllib.parse import quote

    return f"{addr.rstrip('/')}/v1/{_vault_mount()}/data/{quote(path, safe='/')}"


def _vault_mount() -> str:
    """KV v2 mount (``EXAMLOPS_VAULT_MOUNT``, default ``secret``)."""
    return os.getenv("EXAMLOPS_VAULT_MOUNT", "secret").strip().strip("/") or "secret"


def _vault_headers() -> dict[str, str]:
    headers = {"X-Vault-Token": os.getenv("EXAMLOPS_VAULT_TOKEN", "")}
    namespace = os.getenv("EXAMLOPS_VAULT_NAMESPACE", "").strip()
    if namespace:
        headers["X-Vault-Namespace"] = namespace
    return headers


def _vault_timeout() -> float:
    try:
        value = float(os.getenv("EXAMLOPS_VAULT_TIMEOUT", "5"))
    except ValueError:
        return 5.0
    return value if value > 0 else 5.0


def _vault_put(path: str, value: str) -> int:
    """Write ``value`` as a new KV v2 version of ``path``; return that version.

    Fails closed with :class:`SecretBackendError` — a rotation that could not reach the manager
    of record must be seen to fail, not land in a local copy the vault then overrides on read.
    """
    import json
    import urllib.error
    import urllib.request

    addr = os.getenv("EXAMLOPS_VAULT_ADDR", "").strip()
    if not addr:
        raise SecretBackendError(
            "EXAMLOPS_SECRETS_WRITE_BACKEND=vault but EXAMLOPS_VAULT_ADDR is not set"
        )
    url = _vault_data_url(addr, path)
    body = json.dumps({"data": {"value": value}}).encode()
    headers = {**_vault_headers(), "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_vault_timeout()) as resp:  # noqa: S310
            data = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise SecretBackendError(f"vault refused the write of '{path}': HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - any transport failure is a failed write
        raise SecretBackendError(
            f"vault unreachable for the write of '{path}': {type(exc).__name__}"
        ) from exc
    try:
        return int((data.get("data") or {}).get("version") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _vault_status() -> dict[str, Any]:
    """Vault reachability via ``/v1/sys/health`` (unauthenticated; no token sent)."""
    import json
    import urllib.request

    addr = os.getenv("EXAMLOPS_VAULT_ADDR", "").strip()
    out: dict[str, Any] = {
        "configured": bool(addr),
        "addr": addr or None,
        "mount": _vault_mount(),
        "token": bool(os.getenv("EXAMLOPS_VAULT_TOKEN")),
        "reachable": None,
        "initialized": None,
        "sealed": None,
        "error": None,
    }
    if not addr:
        return out
    # 200 active · 429 standby · 472/473 perf standby · 501 uninitialised · 503 sealed — every one
    # of them is an answer, so ask for 200 across the board and read the body.
    url = (
        f"{addr.rstrip('/')}/v1/sys/health?standbyok=true&perfstandbyok=true"
        "&sealedcode=200&uninitcode=200"
    )
    try:
        with urllib.request.urlopen(url, timeout=_vault_timeout()) as resp:  # noqa: S310
            data = json.loads(resp.read().decode() or "{}")
        out.update(
            reachable=True,
            initialized=bool(data.get("initialized")),
            sealed=bool(data.get("sealed")),
        )
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        out.update(reachable=False, error=f"{type(exc).__name__}: {exc}"[:300])
    return out


def backends_status() -> dict[str, Any]:
    """Every backend's configuration + health, and the write backend — never a secret value."""
    from examlops.secrets import sops as _sops

    local: dict[str, Any] = {"keys": sorted(_keyring()), "active_key_id": None, "error": None}
    try:
        local["active_key_id"] = _active_key_id()
    except SecretNotFound as exc:
        local["error"] = str(exc)
    writes: str | None
    write_error: str | None = None
    try:
        writes = write_backend()
    except SecretBackendError as exc:
        writes, write_error = None, str(exc)
    return {
        "order": ["vault", "sops", "local", "env"],
        "write_backend": writes,
        "write_backend_error": write_error,
        "strict": os.getenv("EXAMLOPS_VAULT_STRICT", "").strip().lower() in _TRUTHY,
        "vault": _vault_status(),
        "sops": _sops.status(),
        "local": local,
    }


# --- public API --------------------------------------------------------------


def get_secret(path: str, *, tenant: str = "default", actor: str | None = None) -> str:
    """Resolve a secret (spec R1-R3, R5-R6). Fails fast if absent."""
    return resolve_secret(path, tenant=tenant, actor=actor)["value"]


def resolve_secret(
    path: str, *, tenant: str = "default", actor: str | None = None
) -> dict[str, Any]:
    """Resolve a secret and say **where it came from**: ``{value, backend, vault_error}``.

    :func:`get_secret` is the thin wrapper that returns only the value. Operator-facing
    surfaces (``exa secrets get``) use this one so they can show the backend and warn when
    a configured vault was skipped.

    The audit event records **which backend served the value** (``vault``/``local``/``env``),
    and ``vault_error`` when a configured vault could not be reached. Without that, a clean
    vault read and a silent downgrade to an environment variable left identical audit rows,
    which defeats the point of auditing a secrets subsystem. Set ``EXAMLOPS_VAULT_STRICT=1``
    to refuse the downgrade outright and fail the read instead.
    """
    try:
        _check_path(path)
    except InvalidSecretPath:
        _audit("secret_denied", path, actor, {"tenant": tenant, "reason": "invalid_path"})
        raise
    if not _tenant_allowed(path, tenant):
        _audit("secret_denied", path, actor, {"tenant": tenant})
        raise SecretAccessDenied(f"tenant '{tenant}' may not read secret '{path}'")

    backend = "none"
    extra: dict[str, Any] = {"tenant": tenant}
    try:
        val, vault_error = _vault_get(path)
        if vault_error:
            extra["vault_error"] = vault_error
            if os.getenv("EXAMLOPS_VAULT_STRICT", "").strip().lower() in _TRUTHY:
                raise SecretNotFound(
                    f"vault unreachable for secret '{path}' ({vault_error}) and "
                    "EXAMLOPS_VAULT_STRICT is set — refusing to fall back to another store"
                )
            # Keep serving — a vault blip must not take the platform down — but say so. The
            # value below comes from a different store than the operator configured.
            log.warning(
                "vault unreachable for secret %r (%s) - falling back to the local store/env; "
                "the value served may differ from the one held in the vault",
                path,
                vault_error,
            )
        if val is not None:
            backend = "vault"
            return {"value": val, "backend": backend, "vault_error": vault_error}

        from examlops.secrets import sops as _sops

        sval, sops_error = _sops.get(path)
        if sops_error:
            extra["sops_error"] = sops_error
            if os.getenv("EXAMLOPS_VAULT_STRICT", "").strip().lower() in _TRUTHY:
                raise SecretNotFound(
                    f"SOPS backend failed for secret '{path}' ({sops_error}) and "
                    "EXAMLOPS_VAULT_STRICT is set — refusing to fall back to another store"
                )
            log.warning(
                "SOPS backend failed for secret %r (%s) - falling back to the local store/env",
                path,
                sops_error,
            )
        if sval is not None:
            backend = "sops"
            return {
                "value": sval,
                "backend": backend,
                "vault_error": vault_error,
                "sops_error": sops_error,
            }

        from examlops.data.secrets import get_secret_record

        rec = get_secret_record(path, tenant)
        if rec is not None:
            backend = "local"
            value = _decrypt(rec["ciphertext"], rec.get("key_id"))
            return {"value": value, "backend": backend, "vault_error": vault_error}

        env_key = path.replace("/", "_").replace("-", "_").upper()
        if env_key in os.environ:
            backend = "env"
            return {"value": os.environ[env_key], "backend": backend, "vault_error": vault_error}

        raise SecretNotFound(f"secret '{path}' not found (tenant '{tenant}')")
    finally:
        # Audited on every outcome, including the failures - an access that found nothing (or
        # was refused by strict mode) is exactly the event an operator needs to see.
        extra["backend"] = backend
        _audit("secret_access", path, actor, extra)


def set_secret(
    path: str,
    value: str,
    *,
    tenant: str = "default",
    actor: str | None = None,
    source: str = "exa-secrets",
) -> int:
    """Store a secret in the write backend (audited). Returns its version.

    The store is :func:`write_backend` — ``local`` (Fernet, default), ``vault`` (a new KV v2
    version) or ``sops`` (re-encrypted in place; unversioned, so ``0`` is returned). A manager
    that cannot be reached raises :class:`SecretBackendError`; nothing is written elsewhere.
    Vault and SOPS hold ONE namespace shared by every tenant, so a write there must pass the
    same path-prefix tenant check a read does — otherwise tenant ``acme`` could overwrite
    ``globex/…``. (The local store is keyed by tenant, so a write there cannot collide.)

    ``source`` attributes the audit event to the calling surface (``"exa-secrets"`` by default; the
    dashboard passes ``"dashboard"``) so this shared write serves every face of the platform.
    """
    backend = write_backend()
    extra: dict[str, Any] = {"tenant": tenant, "backend": backend}
    try:
        _check_path(path)
    except InvalidSecretPath:
        _audit(
            "secret_denied",
            path,
            actor,
            {**extra, "op": "write", "reason": "invalid_path"},
            source=source,
        )
        raise
    if backend != "local" and not _tenant_may_write_shared(path, tenant):
        _audit("secret_denied", path, actor, {**extra, "op": "write"}, source=source)
        raise SecretAccessDenied(f"tenant '{tenant}' may not write secret '{path}'")
    version: int
    if backend == "vault":
        try:
            version = _vault_put(path, value)
        except SecretBackendError as exc:
            _audit("secret_set_failed", path, actor, {**extra, "error": str(exc)}, source=source)
            raise
    elif backend == "sops":
        from examlops.secrets import sops as _sops

        try:
            _sops.put(path, value)
        except _sops.SopsError as exc:
            _audit("secret_set_failed", path, actor, {**extra, "error": str(exc)}, source=source)
            raise SecretBackendError(str(exc)) from exc
        version = 0
    else:
        from examlops.data.secrets import put_secret_ciphertext

        ct, key_id = _encrypt(value)
        version = put_secret_ciphertext(path, tenant, ct, updated_by=actor, key_id=key_id)
        extra["key_id"] = key_id
    extra["version"] = version
    _audit("secret_set", path, actor, extra, source=source)
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
    _audit(
        "secret_rotate",
        path,
        actor,
        {"tenant": tenant, "version": version, "backend": write_backend()},
    )
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
                # rewrap=True: a KEK change is not a credential rotation, so the value's age
                # (updated_at) must survive it (ADR 0027 secrets_rotation evidence).
                put_secret_ciphertext(
                    rec["path"], rec["tenant"], ct, updated_by=actor, key_id=active, rewrap=True
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
    # The platform's **own** credential. `exa gateway key create` mints `exa-` +
    # `secrets.token_urlsafe(24)`, and until this rule existed the scanner recognised Slack's
    # tokens and AWS's but not the ones this platform hands out — so a virtual key pasted into a
    # prompt passed the guardrail untouched and reached the model provider, the cache and the logs.
    #
    # The lookaheads are what keep it off the documentation: `docs/assets/explore/data/` is full of
    # slugs like `exa-status-platform-snapshot-at-a-glance`, and a plain `exa-[\w-]{24,}` matched
    # **462** of them across the tracked tree — a rule that fires on the docs is a rule someone
    # turns off. A real key is exactly 32 characters and, with probability 1 − 3e−8, contains both
    # an upper-case letter and a digit; a slug is lower-case words.
    (
        "examlops-virtual-key",
        re.compile(
            r"\bexa-(?=[A-Za-z0-9_\-]{32}(?![A-Za-z0-9_\-]))"
            r"(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{32}"
        ),
    ),
    # Upstream model-provider keys. This platform is an LLM gateway: its users hold these, and a
    # prompt is exactly where one gets pasted by accident.
    ("provider-api-key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}")),
    (
        "github-token",
        re.compile(
            r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}\b"
        ),
    ),
]


def redact_secrets(text: str, placeholder: str = "[redacted-secret]") -> tuple[str, list[str]]:
    """Replace every likely secret in ``text`` with ``placeholder``; return (text, rules hit).

    The same patterns as :func:`scan_text`, which only reports. A guardrail that finds a secret
    in a request has to be able to remove it, not just count it.
    """
    hits: list[str] = []
    for rule, pat in _SECRET_PATTERNS:
        if pat.search(text):
            hits.append(rule)
            text = pat.sub(placeholder, text)
    return text, hits


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
