"""Transparency-log anchoring for audit checkpoints (ADR 0028 decisions 2 and 3).

The WORM anchor (:mod:`examlops.audit_worm`) proves the platform's own copy of the checkpoints was
not rewritten — as long as whoever controls that bucket is not the attacker. A **public
transparency log** closes the last gap: once a checkpoint statement is in Rekor, anyone holding the
log's public key can prove the chain head existed at the log's integration time, and nobody —
the platform operator included — can remove or back-date it.

Two backends, selected with ``EXAMLOPS_AUDIT_TRANSPARENCY`` (``off`` | ``rekor`` | ``sigstore``):

``rekor``
    A ``hashedrekord`` entry is uploaded to ``EXAMLOPS_AUDIT_REKOR_URL`` (``/api/v1/log/entries``).
    The checkpoint statement is signed with a dedicated **ECDSA P-256** key
    (``EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE`` or the D7 secret ``audit/transparency-ecdsa-private``)
    — asymmetric on purpose: the log, and any third party, can check the signature without being
    able to make one. A ``409`` (the entry is already in the log) follows the ``Location`` header,
    so a retried upload is idempotent. Verification binds every receipt to the audit chain as it
    is *now* (the logged head must still be the event at that id), re-fetches every recorded
    entry, checks the digest, the signature against the **platform's** public key
    (``EXAMLOPS_AUDIT_TRANSPARENCY_PUBLIC_KEY_FILE`` or the signing key's public half — never the
    key inside the entry), the log index, and — when ``EXAMLOPS_AUDIT_REKOR_PUBLIC_KEY_FILE`` is
    set — the log's Signed Entry Timestamp.
``sigstore``
    Keyless signing through `sigstore-python <https://pypi.org/project/sigstore/>`_ (the optional
    ``examlops[audit-sigstore]`` extra, imported lazily): a Fulcio certificate bound to the ambient
    OIDC identity signs the statement and the bundle carries the Rekor inclusion. Without the
    library, or without an identity, the anchor is **refused and reported**, never faked.

Security defaults: a non-HTTPS Rekor URL is refused unless it points at the loopback interface;
responses are capped at 1 MiB; every request has a timeout. A failure is counted
(:func:`transparency_failures`) and reported to the caller; nothing is written for a failed upload,
so a later run retries it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

STATEMENT_PREFIX = "examlops-audit-checkpoint/v1"
BACKENDS = ("off", "rekor", "sigstore")
_MAX_RESPONSE = 1 << 20  # 1 MiB: a Rekor entry is a few KiB; anything larger is not one
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

#: Upload/verification failures this process saw, by backend (process-local, like the WORM count).
_FAILURES: dict[str, int] = {}


class TransparencyError(RuntimeError):
    """The transparency anchor could not be made or read; the message says why."""


def transparency_failures() -> dict[str, int]:
    return dict(_FAILURES)


def reset_transparency_failures() -> None:
    _FAILURES.clear()


def _count_failure(backend: str) -> None:
    _FAILURES[backend] = _FAILURES.get(backend, 0) + 1


# ── configuration ────────────────────────────────────────────────────────────────────────────


def backend() -> str:
    """The configured backend. ``rekor`` is implied by ``EXAMLOPS_AUDIT_REKOR_URL`` alone."""
    raw = os.getenv("EXAMLOPS_AUDIT_TRANSPARENCY", "").strip().lower()
    if not raw:
        return "rekor" if os.getenv("EXAMLOPS_AUDIT_REKOR_URL", "").strip() else "off"
    if raw not in BACKENDS:
        raise TransparencyError(
            f"EXAMLOPS_AUDIT_TRANSPARENCY must be one of {', '.join(BACKENDS)}, got {raw!r}"
        )
    return raw


def enabled() -> bool:
    try:
        return backend() != "off"
    except TransparencyError:
        return True  # misconfigured is not "off": the anchor attempt reports the error


def rekor_url() -> str:
    url = os.getenv("EXAMLOPS_AUDIT_REKOR_URL", "").strip().rstrip("/")
    if not url:
        raise TransparencyError("EXAMLOPS_AUDIT_REKOR_URL is not set")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK:
        return url
    raise TransparencyError(
        f"refusing Rekor URL {url!r}: only https (or http on the loopback interface) is allowed"
    )


def _timeout() -> float:
    raw = os.getenv("EXAMLOPS_AUDIT_REKOR_TIMEOUT", "10").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise TransparencyError(
            f"EXAMLOPS_AUDIT_REKOR_TIMEOUT must be a number, got {raw!r}"
        ) from exc
    return min(60.0, max(1.0, value))


# ── the signed statement ─────────────────────────────────────────────────────────────────────


def checkpoint_statement(head_id: int, head_hash: str) -> bytes:
    """What is logged: the chain head, in a fixed, versioned, unambiguous encoding."""
    return f"{STATEMENT_PREFIX}\n{int(head_id)}\n{head_hash}\n".encode()


def _load_signing_key() -> Any:
    """The ECDSA P-256 private key that signs statements for Rekor. Fails closed."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    pem: bytes | None = None
    path = os.getenv("EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE", "").strip()
    if path:
        with open(path, "rb") as fh:
            pem = fh.read()
    else:
        try:
            from examlops.secrets import get_secret

            value = get_secret("audit/transparency-ecdsa-private", actor="audit-transparency")
            pem = value.encode() if isinstance(value, str) else value
        except Exception:  # noqa: BLE001 - absent secret store == no key, reported below
            pem = None
    if not pem:
        raise TransparencyError(
            "no transparency signing key: set EXAMLOPS_AUDIT_TRANSPARENCY_KEY_FILE (ECDSA P-256 "
            "PEM) or store the secret 'audit/transparency-ecdsa-private'"
        )
    key = load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise TransparencyError("the transparency signing key must be an ECDSA P-256 key")
    return key


def _public_pem(public_key: Any) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return bytes(public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))


def _key_id(public_key: Any) -> str:
    return hashlib.sha256(_public_pem(public_key)).hexdigest()[:16]


def _verify_ecdsa(pem: bytes, signature: bytes, payload: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    try:
        key = load_pem_public_key(pem)
        if isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
        elif isinstance(key, ed25519.Ed25519PublicKey):
            key.verify(signature, payload)
        else:
            return False
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


# ── HTTP transport (the test seam) ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float
    ) -> HttpResponse: ...


def _urllib_transport(
    method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float
) -> HttpResponse:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked
            data = resp.read(_MAX_RESPONSE + 1)
            return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, data)
    except urllib.error.HTTPError as exc:
        data = exc.read(_MAX_RESPONSE + 1) if exc.fp else b""
        hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return HttpResponse(exc.code, hdrs, data)


_transport: Transport = _urllib_transport


def set_transport(transport: Transport | None) -> None:
    """Replace the HTTP transport (tests); ``None`` restores urllib."""
    global _transport
    _transport = transport or _urllib_transport


def _json_response(resp: HttpResponse, what: str) -> dict[str, Any]:
    if len(resp.body) > _MAX_RESPONSE:
        raise TransparencyError(f"{what}: response larger than {_MAX_RESPONSE} bytes - refused")
    try:
        doc = json.loads(resp.body or b"{}")
    except json.JSONDecodeError as exc:
        raise TransparencyError(f"{what}: response is not JSON") from exc
    if not isinstance(doc, dict):
        raise TransparencyError(f"{what}: response is not a JSON object")
    return doc


# ── Rekor client ─────────────────────────────────────────────────────────────────────────────


class RekorClient:
    """The two Rekor v1 calls the anchor needs: upload an entry, read one back."""

    def __init__(self, url: str, *, timeout: float = 10.0, transport: Transport | None = None):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._transport = transport

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> HttpResponse:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        transport = self._transport or _transport
        try:
            return transport(method, f"{self.url}{path}", data, headers, self.timeout)
        except TransparencyError:
            raise
        except Exception as exc:  # noqa: BLE001 - network errors become one typed error
            raise TransparencyError(
                f"Rekor {method} {path} failed: {type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _single_entry(doc: dict[str, Any], what: str) -> tuple[str, dict[str, Any]]:
        if len(doc) != 1:
            raise TransparencyError(f"{what}: expected exactly one log entry, got {len(doc)}")
        uuid, entry = next(iter(doc.items()))
        if not isinstance(entry, dict) or "body" not in entry or "logIndex" not in entry:
            raise TransparencyError(f"{what}: malformed log entry")
        return str(uuid), entry

    def upload_hashedrekord(
        self, digest_hex: str, signature_b64: str, public_pem: bytes
    ) -> tuple[str, dict[str, Any]]:
        proposed = {
            "apiVersion": "0.0.1",
            "kind": "hashedrekord",
            "spec": {
                "data": {"hash": {"algorithm": "sha256", "value": digest_hex}},
                "signature": {
                    "content": signature_b64,
                    "publicKey": {"content": base64.b64encode(public_pem).decode()},
                },
            },
        }
        resp = self._call("POST", "/api/v1/log/entries", proposed)
        if resp.status == 201:
            return self._single_entry(_json_response(resp, "Rekor upload"), "Rekor upload")
        if resp.status == 409:
            # Already logged (a retry after a lost response): follow Location - idempotent.
            location = resp.headers.get("location", "")
            uuid = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
            if not uuid:
                raise TransparencyError("Rekor reported a duplicate entry without a Location")
            return self.get_entry(uuid)
        raise TransparencyError(
            f"Rekor upload refused: HTTP {resp.status} {resp.body[:200].decode(errors='replace')}"
        )

    def get_entry(self, uuid: str) -> tuple[str, dict[str, Any]]:
        if not uuid or not all(c in "0123456789abcdefABCDEF" for c in uuid):
            raise TransparencyError(f"not a Rekor entry uuid: {uuid!r}")
        resp = self._call("GET", f"/api/v1/log/entries/{uuid}")
        if resp.status != 200:
            raise TransparencyError(f"Rekor entry {uuid[:16]}...: HTTP {resp.status}")
        return self._single_entry(_json_response(resp, "Rekor read"), "Rekor read")


def _decode_body(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        body = json.loads(base64.b64decode(entry["body"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise TransparencyError("log entry body is not base64 JSON") from exc
    if not isinstance(body, dict):
        raise TransparencyError("log entry body is not a JSON object")
    return body


def _check_set(entry: dict[str, Any]) -> str:
    """Verify the Signed Entry Timestamp: ``"ok"``, ``"bad"`` or ``"unchecked"`` (no log key)."""
    path = os.getenv("EXAMLOPS_AUDIT_REKOR_PUBLIC_KEY_FILE", "").strip()
    if not path:
        return "unchecked"
    with open(path, "rb") as fh:
        pem = fh.read()
    set_b64 = (entry.get("verification") or {}).get("signedEntryTimestamp")
    if not set_b64:
        return "bad"
    payload = json.dumps(
        {
            "body": entry.get("body"),
            "integratedTime": entry.get("integratedTime"),
            "logID": entry.get("logID"),
            "logIndex": entry.get("logIndex"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    try:
        sig = base64.b64decode(set_b64)
    except ValueError:
        return "bad"
    return "ok" if _verify_ecdsa(pem, sig, payload) else "bad"


# ── Sigstore (optional, lazily imported) ─────────────────────────────────────────────────────


def _sigstore_sign(payload: bytes) -> dict[str, Any]:
    """Keyless-sign ``payload`` with sigstore-python; returns the bundle as a JSON object.

    The seam tests replace. Supports sigstore 3.x (``SigningContext.production()``) and 4.x
    (``SigningContext.from_trust_config(ClientTrustConfig.production())``).
    """
    try:
        from sigstore.oidc import IdentityToken, detect_credential  # type: ignore[import-not-found]
        from sigstore.sign import SigningContext  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TransparencyError(
            "sigstore is not installed - pip install 'examlops[audit-sigstore]'"
        ) from exc
    token = os.getenv("EXAMLOPS_AUDIT_SIGSTORE_TOKEN", "").strip() or detect_credential()
    if not token:
        raise TransparencyError(
            "no OIDC identity for keyless signing (set EXAMLOPS_AUDIT_SIGSTORE_TOKEN or run where "
            "an ambient credential is available)"
        )
    if hasattr(SigningContext, "from_trust_config"):
        from sigstore.models import ClientTrustConfig  # type: ignore[import-not-found]

        ctx = SigningContext.from_trust_config(ClientTrustConfig.production())
    else:
        ctx = SigningContext.production()
    with ctx.signer(IdentityToken(token)) as signer:
        bundle = signer.sign_artifact(payload)
    doc = json.loads(bundle.to_json())
    if not isinstance(doc, dict):
        raise TransparencyError("sigstore returned a bundle that is not a JSON object")
    return doc


_sigstore_signer: Callable[[bytes], dict[str, Any]] = _sigstore_sign


def set_sigstore_signer(fn: Callable[[bytes], dict[str, Any]] | None) -> None:
    """Replace the keyless signer (tests); ``None`` restores sigstore-python."""
    global _sigstore_signer
    _sigstore_signer = fn or _sigstore_sign


def _bundle_tlog(bundle: dict[str, Any]) -> dict[str, Any]:
    entries = (bundle.get("verificationMaterial") or {}).get("tlogEntries") or []
    if not entries or not isinstance(entries[0], dict):
        raise TransparencyError("the sigstore bundle carries no transparency-log entry")
    return entries[0]


def _bundle_digest(bundle: dict[str, Any]) -> str:
    digest = ((bundle.get("messageSignature") or {}).get("messageDigest") or {}).get("digest")
    if not digest:
        raise TransparencyError("the sigstore bundle carries no message digest")
    return base64.b64decode(digest).hex()


# ── storage ──────────────────────────────────────────────────────────────────────────────────


def get_receipt(head_hash: str, backend_name: str) -> dict[str, Any] | None:
    from examlops.data.audit_anchors import get_transparency_receipt

    return get_transparency_receipt(head_hash, backend_name)


def list_receipts(limit: int = 100) -> list[dict[str, Any]]:
    from examlops.data.audit_anchors import list_transparency_receipts

    return list_transparency_receipts(limit)


def _store(row: dict[str, Any]) -> None:
    from examlops.data.audit_anchors import store_transparency_receipt

    store_transparency_receipt(row)


# ── anchoring ────────────────────────────────────────────────────────────────────────────────


def anchor_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any] | None:
    """Log ``checkpoint`` (``head_id``/``head_hash``) to the configured transparency log.

    Returns ``None`` when the backend is ``off``; ``{"status": "already-logged"|"logged", ...}``
    otherwise. Raises :class:`TransparencyError` (after counting it) when the anchor could not be
    made - nothing is recorded then, so the next run retries.
    """
    name = backend()
    if name == "off":
        return None
    head_id, head_hash = int(checkpoint["head_id"]), str(checkpoint["head_hash"])
    existing = get_receipt(head_hash, name)
    if existing is not None:
        return {
            "status": "already-logged",
            "backend": name,
            "entry_uuid": existing.get("entry_uuid"),
            "log_index": existing.get("log_index"),
        }
    statement = checkpoint_statement(head_id, head_hash)
    digest = hashlib.sha256(statement).hexdigest()
    try:
        if name == "rekor":
            row = _anchor_rekor(head_id, head_hash, statement, digest)
        else:
            row = _anchor_sigstore(head_id, head_hash, statement, digest)
    except TransparencyError:
        _count_failure(name)
        raise
    except Exception as exc:  # noqa: BLE001 - one typed error for the caller, counted
        _count_failure(name)
        raise TransparencyError(f"{name} anchor failed: {type(exc).__name__}: {exc}") from exc
    _store(row)
    logger.info(
        "audit checkpoint head %s logged to %s (index %s)", head_id, name, row.get("log_index")
    )
    return {
        "status": "logged",
        "backend": name,
        "entry_uuid": row.get("entry_uuid"),
        "log_index": row.get("log_index"),
        "integrated_time": row.get("integrated_time"),
    }


def _anchor_rekor(head_id: int, head_hash: str, statement: bytes, digest: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    url = rekor_url()
    key = _load_signing_key()
    # Deterministic ECDSA (RFC 6979): the same statement always yields the same signature, so the
    # same entry - a retry after a lost response hits Rekor's 409 instead of logging twice.
    signature = key.sign(statement, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
    pub = _public_pem(key.public_key())
    uuid, entry = RekorClient(url, timeout=_timeout()).upload_hashedrekord(
        digest, base64.b64encode(signature).decode(), pub
    )
    _check_body(_decode_body(entry), digest, pub)  # the log stored what we sent
    return {
        "head_id": head_id,
        "head_hash": head_hash,
        "backend": "rekor",
        "log_url": url,
        "entry_uuid": uuid,
        "log_index": int(entry["logIndex"]),
        "integrated_time": int(entry.get("integratedTime") or 0),
        "statement_sha256": digest,
        "key_id": _key_id(key.public_key()),
        "receipt": {"uuid": uuid, **entry},
    }


def _anchor_sigstore(head_id: int, head_hash: str, statement: bytes, digest: str) -> dict[str, Any]:
    bundle = _sigstore_signer(statement)
    if _bundle_digest(bundle) != digest:
        raise TransparencyError("the sigstore bundle signs a different digest than the statement")
    tlog = _bundle_tlog(bundle)
    return {
        "head_id": head_id,
        "head_hash": head_hash,
        "backend": "sigstore",
        "log_url": "sigstore",
        "entry_uuid": None,
        "log_index": int(tlog.get("logIndex", 0)),
        "integrated_time": int(tlog.get("integratedTime", 0)),
        "statement_sha256": digest,
        "key_id": None,
        "receipt": bundle,
    }


def _check_body(body: dict[str, Any], digest: str, public_pem: bytes | None) -> dict[str, Any]:
    """The log's copy of the entry must name our digest (and, when given, our key)."""
    spec = body.get("spec") or {}
    got = ((spec.get("data") or {}).get("hash") or {}).get("value")
    if body.get("kind") != "hashedrekord" or got != digest:
        raise TransparencyError("the log entry does not record this checkpoint's digest")
    sig = spec.get("signature") or {}
    try:
        logged_pub = base64.b64decode((sig.get("publicKey") or {}).get("content", ""))
    except ValueError as exc:
        raise TransparencyError("the log entry's public key is not base64") from exc
    if public_pem is not None and logged_pub.strip() != public_pem.strip():
        raise TransparencyError("the log entry was signed by a different key")
    return {"public_pem": logged_pub, "signature_b64": sig.get("content", "")}


# ── verification ─────────────────────────────────────────────────────────────────────────────


def _trusted_public_pem() -> bytes | None:
    """The public key a Rekor receipt must be signed with, or ``None`` when none is known here.

    ``EXAMLOPS_AUDIT_TRANSPARENCY_PUBLIC_KEY_FILE`` (a verifier that holds only the public half),
    else the public half of the configured signing key. The key *inside* a log entry is never the
    trust anchor: anyone can upload an entry, so checking an entry's signature with the entry's own
    key would accept a receipt swapped for a self-signed entry over a forged head.
    """
    path = os.getenv("EXAMLOPS_AUDIT_TRANSPARENCY_PUBLIC_KEY_FILE", "").strip()
    if path:
        with open(path, "rb") as fh:
            return fh.read()
    try:
        return _public_pem(_load_signing_key().public_key())
    except (TransparencyError, OSError, ValueError):
        return None


def _chain_context(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """The audit chain's hash today at every logged head id, plus the audited retention cut."""
    from examlops.data import get_db, init_db
    from examlops.data.audit_retention import prune_anchor

    ids = sorted({int(r["head_id"]) for r in receipts})
    hashes: dict[int, str] = {}
    init_db()
    with get_db() as conn:
        for i in range(0, len(ids), 500):  # bounded IN lists
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT id, hash FROM audit_events WHERE id IN ({marks})",  # noqa: S608
                chunk,
            ).fetchall()
            hashes.update({int(r["id"]): r["hash"] for r in rows})
    return {"hashes": hashes, "cut": prune_anchor()}


def _chain_error(rec: dict[str, Any], chain: dict[str, Any]) -> str | None:
    """The logged head must still be the chain's event at that id - else the chain was rewritten.

    This is what the log is *for*: a receipt that only re-proves itself says nothing about the
    audit trail a reader sees today.
    """
    head_id, head_hash = int(rec["head_id"]), str(rec["head_hash"])
    if head_id in chain["hashes"]:
        if chain["hashes"][head_id] != head_hash:
            return (
                f"the audit chain no longer holds the logged head: event {head_id} was rewritten "
                "after it was logged"
            )
        return None
    cut = chain.get("cut")
    if cut is not None and head_id <= int(cut["cut_id"]):
        if head_id == int(cut["cut_id"]) and str(cut["cut_hash"]) != head_hash:
            return f"the retention cut at event {head_id} does not match the logged head"
        return None  # pruned under an audited cut (its signature is `exa audit verify`'s job)
    return f"the logged head (event {head_id}) is missing from the audit chain"


def _verify_one(
    rec: dict[str, Any],
    client: RekorClient | None,
    chain: dict[str, Any],
    trusted_pem: bytes | None,
) -> str | None:
    """Error text for one receipt, or ``None`` when it verifies."""
    statement = checkpoint_statement(rec["head_id"], rec["head_hash"])
    digest = hashlib.sha256(statement).hexdigest()
    if rec.get("statement_sha256") != digest:
        return "the stored statement digest does not match the checkpoint"
    chain_err = _chain_error(rec, chain)
    if chain_err:
        return chain_err
    if rec["backend"] == "sigstore":
        try:
            bundle = json.loads(rec["receipt"])
            if _bundle_digest(bundle) != digest:
                return "the sigstore bundle signs a different digest"
            _bundle_tlog(bundle)
        except (TransparencyError, ValueError) as exc:
            return str(exc)
        return None
    if client is None:
        return "no Rekor client"
    if trusted_pem is None:
        return (
            "no trusted transparency public key to check the logged signature against (set "
            "EXAMLOPS_AUDIT_TRANSPARENCY_PUBLIC_KEY_FILE or the signing key)"
        )
    try:
        _uuid, entry = client.get_entry(str(rec.get("entry_uuid") or ""))
        recorded = rec.get("log_index")
        if recorded is None or int(entry["logIndex"]) != int(recorded):
            return f"the log reports index {entry['logIndex']}, the receipt {rec.get('log_index')}"
        body = _check_body(_decode_body(entry), digest, trusted_pem)
        if not _verify_ecdsa(trusted_pem, base64.b64decode(body["signature_b64"] or ""), statement):
            return "the logged signature does not verify over the checkpoint statement"
        if _check_set(entry) == "bad":
            return "the log's Signed Entry Timestamp does not verify"
    except TransparencyError as exc:
        return str(exc)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return f"malformed log entry: {exc}"
    return None


def verify_transparency(*, limit: int = 100) -> dict[str, Any]:
    """Re-check the newest ``limit`` receipts against the log. Never raises for a bad receipt.

    Returns ``{"ok", "backend", "checked", "failures": [...], "unlogged": n}``; ``unlogged`` counts
    signed checkpoints newer than the newest logged head (they have no receipt yet).
    """
    try:
        name = backend()
    except TransparencyError as exc:
        return {"ok": False, "backend": "invalid", "checked": 0, "reason": str(exc), "failures": []}
    receipts = list_receipts(limit)
    if name == "off" and not receipts:
        return {
            "ok": True,
            "backend": "off",
            "checked": 0,
            "reason": "no transparency log configured",
            "failures": [],
        }
    client: RekorClient | None = None
    if any(r["backend"] == "rekor" for r in receipts):
        try:
            client = RekorClient(rekor_url(), timeout=_timeout())
        except TransparencyError as exc:
            return {"ok": False, "backend": name, "checked": 0, "reason": str(exc), "failures": []}
    try:
        trusted_pem = _trusted_public_pem() if client is not None else None
    except OSError as exc:
        return {"ok": False, "backend": name, "checked": 0, "reason": str(exc), "failures": []}
    chain = _chain_context(receipts) if receipts else {"hashes": {}, "cut": None}
    failures = []
    for rec in receipts:
        err = _verify_one(rec, client, chain, trusted_pem)
        if err:
            failures.append({"head_id": rec["head_id"], "backend": rec["backend"], "reason": err})
    if failures:
        _count_failure("verify")
    unlogged = _count_unlogged(receipts)
    out: dict[str, Any] = {
        "ok": not failures,
        "backend": name,
        "checked": len(receipts),
        "failures": failures,
        "unlogged": unlogged,
        "reason": "verified" if not failures else f"{len(failures)} receipt(s) failed",
        "set_checked": bool(os.getenv("EXAMLOPS_AUDIT_REKOR_PUBLIC_KEY_FILE", "").strip()),
    }
    warnings: list[str] = []
    if unlogged:
        warnings.append(
            f"{unlogged} signed checkpoint(s) newer than the last logged head are not in the "
            "transparency log - the next `exa audit maintain` run logs the newest"
        )
    sigstore_n = sum(1 for r in receipts if r["backend"] == "sigstore")
    if sigstore_n:
        # Say what was not checked: the Fulcio certificate chain and signer identity need the
        # sigstore library and an identity policy (ADR 0028, deferred).
        out["sigstore_structural_only"] = sigstore_n
        warnings.append(
            f"{sigstore_n} sigstore bundle(s) checked for digest and tlog entry only - the "
            "certificate chain and signer identity were not verified"
        )
    if warnings:
        out["warning"] = "; ".join(warnings)
    return out


def _count_unlogged(receipts: list[dict[str, Any]]) -> int:
    """Signed checkpoints newer than the newest logged head: the state the log cannot vouch for."""
    if not receipts:
        return 0
    from examlops.data.audit_anchors import count_checkpoints_after

    return count_checkpoints_after(max(int(r["head_id"]) for r in receipts))


__all__ = [
    "BACKENDS",
    "HttpResponse",
    "RekorClient",
    "TransparencyError",
    "anchor_checkpoint",
    "backend",
    "checkpoint_statement",
    "enabled",
    "get_receipt",
    "list_receipts",
    "reset_transparency_failures",
    "set_sigstore_signer",
    "set_transport",
    "transparency_failures",
    "verify_transparency",
]
