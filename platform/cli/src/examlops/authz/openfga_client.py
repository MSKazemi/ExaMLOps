"""OpenFGA HTTP client behind the authz seam (ADR 0014 decision 2).

``examlops.authz.check`` answers from the native ``authz_relations`` table. When an OpenFGA server
is *configured* it answers from there instead, so the relationship store can live in a dedicated,
independently operated service (the audit's "lift security stores out of the shared SQLite
monolith"). Nothing changes unless configured:

* ``EXAMLOPS_OPENFGA_URL`` unset               -> native authz, silently (the default).
* URL set but ``EXAMLOPS_OPENFGA_STORE_ID`` not -> native authz, with a loud ERROR log (once per
  process): a half-configured security service must never look like an enabled one.
* fully configured                             -> every check, grant and revoke goes to OpenFGA.

**Fail closed.** A timeout, a connection error, a non-2xx response or a malformed body raises
:class:`OpenFgaError`; :func:`examlops.authz.check` turns that into a *deny* (and an
``authz_error`` audit event). Grants and revokes that OpenFGA refuses are not applied natively.

Object mapping (the model exported by :mod:`examlops.authz.openfga`): ``project:acme`` stays
``project:acme``; a child ``project:acme/model:JPCP`` becomes ``model:acme~JPCP`` and a ``parent``
tuple links it to ``project:acme``, which is what makes project-level grants inherit. Subjects
become ``user:<subject>``. Objects of a type the model does not define (e.g. ``platform:core``)
are passed through and OpenFGA rejects them, which denies: extend the model before relying on
OpenFGA for such objects. No new dependency: ``httpx`` is already required.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_LOGGED_INCOMPLETE = False


class OpenFgaError(RuntimeError):
    """OpenFGA could not answer or refused the request (callers treat this as DENY)."""


@dataclass(frozen=True)
class FgaConfig:
    url: str
    store_id: str
    model_id: str | None
    token: str | None
    timeout_s: float


def config_from_env() -> FgaConfig | None:
    """The configuration, or ``None`` to use the native backend (see the module docstring)."""
    global _LOGGED_INCOMPLETE
    url = os.getenv("EXAMLOPS_OPENFGA_URL", "").strip().rstrip("/")
    if not url:
        return None
    store = os.getenv("EXAMLOPS_OPENFGA_STORE_ID", "").strip()
    if not store:
        if not _LOGGED_INCOMPLETE:
            _LOGGED_INCOMPLETE = True
            logger.error(
                "EXAMLOPS_OPENFGA_URL is set but EXAMLOPS_OPENFGA_STORE_ID is not: OpenFGA is NOT "
                "being used - authorization falls back to the native authz_relations table"
            )
        return None
    try:
        timeout = float(os.getenv("EXAMLOPS_OPENFGA_TIMEOUT", "2.0"))
    except ValueError:
        timeout = 2.0
    return FgaConfig(
        url=url,
        store_id=store,
        model_id=os.getenv("EXAMLOPS_OPENFGA_MODEL_ID", "").strip() or None,
        token=os.getenv("EXAMLOPS_OPENFGA_TOKEN", "").strip() or None,
        timeout_s=max(0.1, timeout),
    )


def reset_config_log() -> None:
    """Test seam: allow the once-per-process incomplete-config error to log again."""
    global _LOGGED_INCOMPLETE
    _LOGGED_INCOMPLETE = False


def fga_user(subject: str) -> str:
    return f"user:{subject}"


def fga_object(native: str) -> tuple[str, str | None]:
    """``(fga_object, fga_parent_or_None)`` for a native hierarchical object string."""
    if "/" not in native:
        return native, None
    parent, last = native.rsplit("/", 1)
    typ, _, ident = last.partition(":")
    root = parent.split("/", 1)[0]  # project:<name>
    project_id = root.partition(":")[2]
    return f"{typ}:{project_id}~{ident}", root


def _headers(cfg: FgaConfig) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if cfg.token:
        h["Authorization"] = f"Bearer {cfg.token}"
    return h


def _post(cfg: FgaConfig, path: str, body: dict[str, Any]) -> dict[str, Any]:
    import httpx

    url = f"{cfg.url}/stores/{cfg.store_id}/{path}"
    try:
        resp = httpx.post(url, json=body, headers=_headers(cfg), timeout=cfg.timeout_s)
    except httpx.HTTPError as exc:  # timeouts, refused connections, TLS errors ...
        raise OpenFgaError(f"OpenFGA unreachable ({type(exc).__name__}: {exc})") from exc
    if resp.status_code >= 400:
        raise OpenFgaError(f"OpenFGA {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise OpenFgaError(f"OpenFGA {path} returned a non-JSON body") from exc
    if not isinstance(doc, dict):
        raise OpenFgaError(f"OpenFGA {path} returned an unexpected body")
    return doc


def _with_model(cfg: FgaConfig, body: dict[str, Any]) -> dict[str, Any]:
    if cfg.model_id:
        body["authorization_model_id"] = cfg.model_id
    return body


def check(cfg: FgaConfig, subject: str, relation: str, obj: str) -> bool:
    """Ask OpenFGA. Raises :class:`OpenFgaError` on any failure; never returns a guess."""
    fobj, _ = fga_object(obj)
    doc = _post(
        cfg,
        "check",
        _with_model(
            cfg,
            {"tuple_key": {"user": fga_user(subject), "relation": relation, "object": fobj}},
        ),
    )
    allowed = doc.get("allowed")
    if not isinstance(allowed, bool):
        raise OpenFgaError("OpenFGA check response has no boolean 'allowed'")
    return allowed


def _tuples(subject: str, relation: str, obj: str) -> list[dict[str, str]]:
    fobj, parent = fga_object(obj)
    keys = [{"user": fga_user(subject), "relation": relation, "object": fobj}]
    if parent:
        # Idempotent link that makes project-level grants reach this child.
        keys.append({"user": parent, "relation": "parent", "object": fobj})
    return keys


def _write(cfg: FgaConfig, kind: str, keys: list[dict[str, str]], tolerate: str) -> None:
    try:
        _post(cfg, "write", _with_model(cfg, {kind: {"tuple_keys": keys}}))
    except OpenFgaError as exc:
        # Re-writing an existing tuple / deleting a missing one is a no-op, not a failure.
        if tolerate in str(exc).lower():
            return
        raise


def write_grant(cfg: FgaConfig, subject: str, relation: str, obj: str) -> None:
    keys = _tuples(subject, relation, obj)
    for key in keys:  # one tuple per call so an already-present parent link cannot mask the grant
        _write(cfg, "writes", [key], "already exist")


def delete_grant(cfg: FgaConfig, subject: str, relation: str, obj: str) -> None:
    # Only the subject's tuple: the parent link is shared by every grant on the child.
    fobj, _ = fga_object(obj)
    _write(
        cfg,
        "deletes",
        [{"user": fga_user(subject), "relation": relation, "object": fobj}],
        "does not exist",
    )


def sync_native_grants(cfg: FgaConfig, *, dry_run: bool = True) -> dict[str, Any]:
    """Backfill OpenFGA from the native ``authz_relations`` table (idempotent)."""
    from examlops.data.governance import list_relations

    rows = list_relations()
    if dry_run:
        return {"dry_run": True, "grants": len(rows)}
    for r in rows:
        write_grant(cfg, r["subject"], r["relation"], r["object"])
    return {"dry_run": False, "grants": len(rows)}
