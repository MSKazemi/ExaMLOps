"""Startup secret injection — ADR 0011 clause 2.

A service's environment names **where** a credential lives instead of holding it:

``CONTROL_PLANE_TOKEN=secret://control-plane/token``
    resolved through the secrets client (vault → sops → local store → env), audited as a
    ``secret_access`` by ``service:<name>``. ``secret://acme/api-key?tenant=acme`` reads as a
    tenant.
``DASHBOARD_JWT_SECRET=secret+file:///run/secrets/dashboard_jwt``
    read from a mounted file — the Docker/Kubernetes secrets and Vault-Agent-template convention
    — so the value arrives through a tmpfs mount, never through compose or ``docker inspect``.

:func:`inject_env` runs once at process start, before the service reads its configuration, and
replaces each reference with its value in ``os.environ``; every existing ``os.getenv`` in the
service then sees the credential without change. It is **idempotent** (a resolved value is no
longer a reference) and **fails closed**: an unresolvable reference aborts start-up with
:class:`SecretInjectionError`, naming the variable and the reason but never a value. With
``EXAMLOPS_SECRETS_INJECT_STRICT=0`` the service starts anyway, and the unresolved variable is
*removed* — never left holding the literal ``secret://…`` string, which a token check would
otherwise accept as the credential.

``EXAMLOPS_SECRETS_INJECT=0`` switches the mechanism off: references are then *removed* unresolved
(for the same reason as above) and a warning is logged. It is on by default and a no-op when the
environment holds no reference.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

log = logging.getLogger("examlops.secrets.inject")

REF_PREFIX = "secret://"
FILE_PREFIX = "secret+file://"
MAX_REFS = 256
MAX_FILE_BYTES = 64 * 1024
_FALSY = {"0", "false", "no", "off"}

# Variable names that look like they carry a credential. Used by :func:`classify_env` to flag
# plaintext credentials — the thing clause 2 says must not sit in compose or the environment.
_SECRETISH = re.compile(r"(TOKEN|PASSWORD|PASSWD|SECRET|API_?KEY|PRIVATE_KEY|CREDENTIALS)", re.I)
_NOT_A_VALUE = re.compile(
    r"_(FILE|PATH|DIR|URL|ADDR|ID|IDS|CLAIM|HEADER|MODE|TTL|BACKEND|NAME|TENANTS|MOUNT|"
    r"NAMESPACE|TIMEOUT|STRICT|INJECT|ENABLED|KEY_ID|ALGORITHM|ISSUER|AUDIENCE|REF)$",
    re.I,
)
# "Secret zero": the credentials the secrets client itself needs to reach a store. They cannot
# be references to that store, so they are reported as bootstrap, not as a plaintext leak.
BOOTSTRAP = frozenset(
    {
        "EXAMLOPS_VAULT_TOKEN",
        "EXAMLOPS_SECRETS_KEY",
        "EXAMLOPS_SECRETS_KEYS",
        "DASHBOARD_SECRET_KEY",
        "SOPS_AGE_KEY",
    }
)


class SecretInjectionError(RuntimeError):
    """One or more ``secret://`` references could not be resolved at start-up."""


@dataclass(frozen=True)
class Injected:
    name: str
    kind: str  # "manager" | "file"
    target: str  # the secret path or file path — never the value
    backend: str  # vault | sops | local | env | file


@dataclass
class InjectionReport:
    service: str
    injected: list[Injected] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (variable, reason)
    removed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "injected": [i.__dict__ for i in self.injected],
            "errors": [{"name": n, "error": e} for n, e in self.errors],
            "removed": list(self.removed),
        }


def is_reference(value: str | None) -> bool:
    return bool(value) and (str(value).startswith(REF_PREFIX) or str(value).startswith(FILE_PREFIX))


def parse_ref(value: str) -> tuple[str, str]:
    """``secret://a/b?tenant=t`` → ``("a/b", "t")`` (tenant defaults to ``default``)."""
    if not value.startswith(REF_PREFIX):
        raise ValueError("not a secret:// reference")
    parts = urlsplit(value)
    path = unquote((parts.netloc + parts.path).strip("/"))
    if not path or any(seg in {"", ".", ".."} for seg in path.split("/")):
        raise ValueError("secret:// reference names no valid path")
    tenant = (parse_qs(parts.query).get("tenant") or ["default"])[0].strip() or "default"
    return path, tenant


def parse_file_ref(value: str) -> Path:
    """``secret+file:///run/secrets/x`` → ``Path('/run/secrets/x')`` (absolute paths only)."""
    if not value.startswith(FILE_PREFIX):
        raise ValueError("not a secret+file:// reference")
    raw = unquote(value[len(FILE_PREFIX) :])
    if not raw.startswith("/"):
        raise ValueError("secret+file:// needs an absolute path (secret+file:///run/secrets/x)")
    return Path(raw)


def read_secret_file(path: Path) -> str:
    """Read a mounted secret file: regular file, ≤ :data:`MAX_FILE_BYTES`, one trailing EOL cut."""
    if not path.is_file():
        raise FileNotFoundError(f"secret file {path} does not exist or is not a regular file")
    # Read at most one byte past the limit rather than trusting a stat taken before the read.
    with path.open("rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"secret file {path} exceeds the limit of {MAX_FILE_BYTES} bytes")
    text = data.decode("utf-8")
    if text.endswith("\r\n"):
        return text[:-2]
    return text[:-1] if text.endswith("\n") else text


def find_refs(environ: Mapping[str, str]) -> dict[str, str]:
    """Every environment variable whose value is a secret reference."""
    return {k: v for k, v in environ.items() if is_reference(v)}


def _enabled() -> bool:
    return os.getenv("EXAMLOPS_SECRETS_INJECT", "1").strip().lower() not in _FALSY


def _strict() -> bool:
    return os.getenv("EXAMLOPS_SECRETS_INJECT_STRICT", "1").strip().lower() not in _FALSY


def _resolve_one(name: str, value: str, actor: str) -> tuple[str, Injected]:
    if value.startswith(FILE_PREFIX):
        fpath = parse_file_ref(value)
        return read_secret_file(fpath), Injected(name, "file", str(fpath), "file")
    from examlops.secrets import resolve_secret

    path, tenant = parse_ref(value)
    res = resolve_secret(path, tenant=tenant, actor=actor)
    return res["value"], Injected(name, "manager", path, str(res["backend"]))


def inject_env(
    service: str,
    environ: MutableMapping[str, str] | None = None,
    *,
    strict: bool | None = None,
) -> InjectionReport:
    """Resolve every secret reference in ``environ`` (default ``os.environ``) in place.

    Returns what was injected (names, targets and backends — never values). Raises
    :class:`SecretInjectionError` when strict (the default) and any reference fails.
    """
    env: MutableMapping[str, str] = os.environ if environ is None else environ
    report = InjectionReport(service=service)
    refs = find_refs(env)
    if not refs:
        return report
    if not _enabled():
        # Off means "do not resolve", never "use the reference as the credential": a literal
        # `secret://control-plane/token` sits in compose for anyone to read, and a token check
        # that compared against it would accept that public string. Drop them instead.
        for name in sorted(refs):
            env.pop(name, None)
            report.removed.append(name)
        log.warning(
            "EXAMLOPS_SECRETS_INJECT is off - %d secret reference(s) removed unresolved: %s",
            len(refs),
            ", ".join(report.removed),
        )
        return report
    strict = _strict() if strict is None else strict
    if len(refs) > MAX_REFS:
        raise SecretInjectionError(
            f"{service}: {len(refs)} secret references exceeds the limit of {MAX_REFS}"
        )
    actor = f"service:{service}"
    for name in sorted(refs):
        try:
            value, done = _resolve_one(name, refs[name], actor)
        except Exception as exc:  # noqa: BLE001 - collected, reported by name, never by value
            report.errors.append((name, f"{type(exc).__name__}: {exc}"[:300]))
            continue
        env[name] = value
        report.injected.append(done)
    if report.errors and not strict:
        for name, _ in report.errors:
            env.pop(name, None)  # never leave the literal reference to be used as a credential
            report.removed.append(name)
    _audit(report)
    backends = sorted({i.backend for i in report.injected})
    log.info(
        "secret injection for %s: %d injected (%s), %d failed",
        service,
        len(report.injected),
        ",".join(backends) or "-",
        len(report.errors),
    )
    if report.errors and strict:
        detail = "; ".join(f"{n}: {e}" for n, e in report.errors)
        raise SecretInjectionError(
            f"{service}: {len(report.errors)} secret reference(s) could not be resolved - "
            f"refusing to start ({detail})"
        )
    return report


def _audit(report: InjectionReport) -> None:
    from examlops.data.audit import audit_best_effort

    audit_best_effort(
        "secrets-inject",
        f"service:{report.service}",
        "secrets_injected" if not report.errors else "secrets_inject_failed",
        report.service,
        {
            "injected": [[i.name, i.backend] for i in report.injected],
            "failed": [n for n, _ in report.errors],
            "removed": report.removed,
        },
    )


def classify_env(environ: Mapping[str, str]) -> list[dict[str, Any]]:
    """Classify credential-carrying variables: ``reference`` · ``file`` · ``bootstrap`` · ``plaintext``.

    Values are never returned. A ``plaintext`` row is a credential sitting in the environment in
    clear — what clause 2 says a deploy must not do.
    """
    rows: list[dict[str, Any]] = []
    for name in sorted(environ):
        value = environ[name]
        if value.startswith(REF_PREFIX):
            rows.append({"name": name, "kind": "reference", "target": value.split("?", 1)[0]})
        elif value.startswith(FILE_PREFIX):
            rows.append({"name": name, "kind": "file", "target": value[len(FILE_PREFIX) :]})
        elif name in BOOTSTRAP and value:
            rows.append({"name": name, "kind": "bootstrap", "target": None})
        elif value and _SECRETISH.search(name) and not _NOT_A_VALUE.search(name):
            rows.append({"name": name, "kind": "plaintext", "target": None})
    return rows


def parse_env_file(text: str) -> dict[str, str]:
    """Minimal dotenv reader (``KEY=VALUE``, ``export`` prefix, quotes, ``#`` comments)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def check_env(environ: Mapping[str, str], *, actor: str) -> list[dict[str, Any]]:
    """:func:`classify_env` plus whether each reference resolves (value discarded, access audited)."""
    rows = classify_env(environ)
    for row in rows:
        row["resolves"] = None
        row["error"] = None
        if row["kind"] not in {"reference", "file"}:
            continue
        try:
            _, done = _resolve_one(row["name"], environ[row["name"]], actor)
            row["resolves"] = True
            row["backend"] = done.backend
        except Exception as exc:  # noqa: BLE001 - reported per row
            row["resolves"] = False
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return rows
