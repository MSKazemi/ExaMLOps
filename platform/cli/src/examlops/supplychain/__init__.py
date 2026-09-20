"""D3 — ML supply-chain security: signing, AI-BOM, verify-before-load (ADR 0013).

Sign a model artifact bundle, emit a CycloneDX AI-BOM, and verify signature +
integrity before serving. The production signer is Sigstore/OpenSSF model-signing
(keyless Fulcio/OIDC); the **fallback** — used with no OIDC identity — is an
HMAC-SHA256 signature keyed by a D7-managed signing key, so the feature works with
no external service.

Verify-before-load (spec R6/R7): ``enforce`` mode refuses to load a version whose
signature/integrity check fails; ``warn`` mode records + alerts but may load.

Two signature schemes (plan P4.10):

``ed25519-v2`` (used whenever a private key is configured)
    An asymmetric signature. The signer — the training pipeline at registration, or an operator
    running ``exa models sign`` — holds the private key (``EXAMLOPS_SIGNING_PRIVATE_KEY_FILE``,
    or the D7 secret ``model-signing/ed25519-private``). A serving replica holds only the public
    trust bundle (``EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE``), so it can check a signature and cannot
    make one. The signed statement binds the model name and version to a canonical manifest
    digest (relative path, size and SHA-256 of every file), so a signature cannot be moved to
    another version, and renaming or moving a file inside the bundle breaks it. The row's ``cert``
    column holds the key id, which is how several trusted keys are told apart during a rotation.
``hmac-sha256`` (legacy fallback)
    A shared-secret MAC over a name-and-bytes digest. Every verifier holds the signing key, so
    every verifier could also forge. Kept so existing rows still verify, and used to sign only
    when no Ed25519 key is configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20

# There is deliberately no verification cache here. Spec R8 asked for one "per artifact digest",
# and that key answers a different question than the gate asks: "have these bytes ever verified,
# for anything?" rather than "do these bytes match *this* version's recorded signature?". The two
# diverge the moment two records share a digest, and the divergence is a false accept — a version
# whose artifacts had been swapped for another model's signed bytes was reported `verified` and
# loaded in `enforce` mode. A key that could be correct would have to include the recorded
# signature and the signing key, and resolving the signing key is most of the remaining work.
#
# What the cache could ever have saved is 4 us of HMAC over a 64-character digest. It could never
# save the part that costs anything — re-hashing the artifacts — because that hash IS the tamper
# check, so a cache hit would be the check answering itself. Measured: 3.79 us against a 2.00 ms
# digest for a 1 MB artifact (0.19%), 4.26 us against 77 ms for 50 MB (0.0055%).


class SigningKeyMissing(RuntimeError):
    """No signing key available (an Ed25519 private key, the D7 secret, or EXAMLOPS_SIGNING_KEY)."""


ED25519 = "ed25519-v2"
HMAC = "hmac-sha256"
_STATEMENT = "examlops-model-signature/v2"


@dataclass(frozen=True)
class Signature:
    model: str
    version: str
    digest: str
    algo: str
    signature: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str
    digest: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _signing_key() -> bytes:
    """Resolve the HMAC signing key from D7 secrets or the env fallback."""
    key = os.getenv("EXAMLOPS_SIGNING_KEY")
    if not key:
        try:
            from examlops.secrets import get_secret

            key = get_secret("model-signing/key", actor="supplychain")
        except Exception:
            key = None
    if not key:
        raise SigningKeyMissing(
            "no signing key: set EXAMLOPS_SIGNING_KEY or store secret 'model-signing/key'"
        )
    return key.encode() if isinstance(key, str) else key


def artifact_digest(paths: Iterable[Path]) -> str:
    """SHA-256 over the sorted artifact file set (bundle digest)."""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        if not p.is_file():
            continue
        h.update(p.name.encode())
        with p.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
    return h.hexdigest()


def _hmac_sign(digest: str) -> str:
    return hmac.new(_signing_key(), digest.encode(), hashlib.sha256).hexdigest()


def sign_or_explain(payload: str, *, subject: str) -> tuple[str | None, str | None]:
    """``(signature, algorithm)`` for ``payload``, or ``(None, None)`` with the reason made visible.

    Two callers sign an identity and record the result — the fine-tuning adapter registry and the
    reproducibility bundle — and both used to wrap the signer in `except Exception`. That made a
    **failure** to sign indistinguishable from **no key configured**: same `(None, None)`, same
    column, nothing anywhere to tell them apart, so a broken signer looked like a deliberate policy
    forever.

    Running without a signing key is a site's choice, so that degrades quietly. Anything else — a
    malformed key, an unreachable secret store, a bug in the signer — is logged with the cause and
    the subject, and still degrades: a signing fault should not cost someone their run, but it must
    not pass unnoticed either.
    """
    try:
        return _hmac_sign(payload), "hmac-sha256"
    except SigningKeyMissing:
        return None, None  # no key configured — the documented, quiet degradation
    except Exception as exc:  # noqa: BLE001 - a failure to sign must not fail the caller's work
        logger.warning(
            "%s is unsigned because signing FAILED (%s: %s) — this is not the same as having no "
            "signing key configured; check EXAMLOPS_SIGNING_KEY or the 'model-signing/key' secret",
            subject,
            type(exc).__name__,
            exc,
        )
        return None, None


# ── Ed25519 (plan P4.10) ─────────────────────────────────────────────────────


def _root_of(paths: list[Path], root: Path | None) -> Path:
    if root is not None:
        return Path(root)
    if not paths:
        return Path(".")
    if len(paths) == 1:
        return paths[0].parent
    return Path(os.path.commonpath([str(p) for p in paths]))


def manifest(artifact_paths: Iterable[Path], *, root: Path | None = None) -> list[list]:
    """``[relative path, size, sha256]`` for every file, sorted by path."""
    paths = [Path(p) for p in artifact_paths if Path(p).is_file()]
    base = _root_of(paths, root)
    rows = []
    for p in paths:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
        rows.append([p.relative_to(base).as_posix(), p.stat().st_size, h.hexdigest()])
    return sorted(rows)


def manifest_digest(artifact_paths: Iterable[Path], *, root: Path | None = None) -> str:
    """The digest an Ed25519 signature covers: SHA-256 of the canonical JSON manifest.

    JSON escaping makes the encoding unambiguous whatever a file is called, unlike joining names
    and bytes; the relative path is included, so moving a file inside the bundle changes it.
    """
    encoded = json.dumps(manifest(artifact_paths, root=root), separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def statement(model: str, version: str, digest: str) -> bytes:
    """What is signed: the model (case-insensitive, as it is looked up), the version and digest."""
    return f"{_STATEMENT}\n{model.strip().lower()}\n{version}\n{digest}".encode()


def _load_private_key():  # -> Ed25519PrivateKey | None
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    pem: bytes | None = None
    path = os.getenv("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", "").strip()
    if path:
        pem = Path(path).read_bytes()
    else:
        try:
            from examlops.secrets import get_secret

            value = get_secret("model-signing/ed25519-private", actor="supplychain")
            pem = value.encode() if isinstance(value, str) else value
        except Exception:
            pem = None
    if not pem:
        return None
    key = load_pem_private_key(pem, password=None)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not isinstance(key, Ed25519PrivateKey):
        raise SigningKeyMissing("the configured signing private key is not an Ed25519 key")
    return key


def _raw_public(public_key) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def public_key_b64(public_key) -> str:
    """A public key as ``EXAMLOPS_SIGNING_PUBLIC_KEYS`` takes it: base64 of the raw 32 bytes."""
    return base64.b64encode(_raw_public(public_key)).decode()


def signer_public_key() -> str | None:
    """The configured signer's public key (base64 raw), to hand to the serving plane."""
    private = _load_private_key()
    return public_key_b64(private.public_key()) if private is not None else None


def key_id(public_key) -> str:
    """A short, stable name for a public key: the first 16 hex of SHA-256 over its raw bytes."""
    return hashlib.sha256(_raw_public(public_key)).hexdigest()[:16]


def trusted_public_keys() -> dict[str, object]:
    """Key id → Ed25519 public key, from the trust bundle and the local private key if any.

    Two sources, both optional: ``EXAMLOPS_SIGNING_PUBLIC_KEYS`` — comma-separated base64 raw
    32-byte keys, one line, so it fits a ``.env`` file with no mount — and
    ``EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE``, one or more PEM public keys concatenated. Keep a retiring
    key in the bundle until every version it signed has been re-signed or retired.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    keys: dict[str, object] = {}
    for item in os.getenv("EXAMLOPS_SIGNING_PUBLIC_KEYS", "").split(","):
        if item.strip():
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(item.strip()))
            keys[key_id(key)] = key
    path = os.getenv("EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE", "").strip()
    if path:
        text = Path(path).read_text(encoding="utf-8")
        end = "-----END PUBLIC KEY-----"
        for block in text.split(end):
            if "-----BEGIN PUBLIC KEY-----" not in block:
                continue
            pem = (block[block.index("-----BEGIN PUBLIC KEY-----") :] + end + "\n").encode()
            loaded = load_pem_public_key(pem)
            if isinstance(loaded, Ed25519PublicKey):
                keys[key_id(loaded)] = loaded
    private = _load_private_key()
    if private is not None:
        keys[key_id(private.public_key())] = private.public_key()
    return keys


def sign_model(
    model: str,
    version: str,
    artifact_paths: Iterable[Path],
    *,
    actor: str | None = None,
    root: Path | None = None,
) -> Signature:
    """Sign a model artifact bundle and store the signature (spec R1/R2).

    Ed25519 when a private key is configured, otherwise the legacy HMAC. ``root`` is the bundle's
    top directory; by default the common parent of ``artifact_paths``.
    """
    from examlops.data.registry import store_model_signature

    paths = [Path(p) for p in artifact_paths]
    private = _load_private_key()
    if private is not None:
        digest = manifest_digest(paths, root=root)
        sig = base64.b64encode(private.sign(statement(model, version, digest))).decode()
        kid = key_id(private.public_key())
        store_model_signature(model, version, digest, sig, algo=ED25519, cert=kid, signed_by=actor)
        _audit("model_signed", model, version, actor, {"digest": digest[:23], "key_id": kid})
        return Signature(model, version, digest, ED25519, sig)
    digest = artifact_digest(paths)
    sig = _hmac_sign(digest)
    store_model_signature(model, version, digest, sig, algo=HMAC, signed_by=actor)
    _audit("model_signed", model, version, actor, {"digest": digest[:16]})
    return Signature(model, version, digest, HMAC, sig)


def signing_configured() -> str | None:
    """The scheme ``sign_model`` would use now (``ed25519-v2`` / ``hmac-sha256``), or None."""
    try:
        if _load_private_key() is not None:
            return ED25519
    except Exception:  # noqa: BLE001 - a broken key is "not configured" here; sign_model says why
        return None
    try:
        _signing_key()
        return HMAC
    except SigningKeyMissing:
        return None


def registered_artifacts(model: str, version: str, dst: Path) -> Path:
    """Download ``models:/<model>/<version>`` into ``dst``: the tree serving verifies and loads."""
    import mlflow  # noqa: PLC0415

    return Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"models:/{model}/{version}", dst_path=str(dst)
        )
    )


def sign_registered_version(model: str, version: str, *, actor: str | None = None) -> Signature:
    """Sign a registered version's artifacts exactly as the serving plane will download them."""
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="examlops-sign-") as tmp:
        local = registered_artifacts(model, version, Path(tmp))
        paths = [p for p in local.rglob("*") if p.is_file()]
        if not paths:
            raise RuntimeError(f"models:/{model}/{version} has no artifact files to sign")
        return sign_model(model, version, paths, actor=actor, root=local)


def verify_record(
    record: dict, model: str, version: str, artifact_paths: Iterable[Path], *, root=None
) -> VerifyResult:
    """Check ``artifact_paths`` against one signature ``record`` (a ``model_signatures`` row, or
    the copy a serving snapshot carries). Reasons: ``verified``, ``tampered``, ``bad-signature``,
    ``untrusted-key`` and ``unknown-algorithm``."""
    paths = [Path(p) for p in artifact_paths]
    algo = record.get("algo") or HMAC
    if algo == ED25519:
        current = manifest_digest(paths, root=root)
        if current != record["digest"]:
            return VerifyResult(False, "tampered: artifact digest changed", current)
        public = trusted_public_keys().get(record.get("cert") or "")
        if public is None:
            return VerifyResult(False, f"untrusted-key: {record.get('cert')!r}", current)
        from cryptography.exceptions import InvalidSignature

        try:
            public.verify(  # type: ignore[attr-defined]
                base64.b64decode(record["signature"]), statement(model, version, current)
            )
        except (InvalidSignature, ValueError):
            return VerifyResult(False, "bad-signature", current)
        return VerifyResult(True, "verified", current)
    if algo != HMAC:
        return VerifyResult(False, f"unknown-algorithm: {algo!r}")
    current = artifact_digest(paths)
    if current != record["digest"]:
        return VerifyResult(False, "tampered: artifact digest changed", current)
    expected = _hmac_sign(current)
    ok = hmac.compare_digest(expected, record["signature"])
    return VerifyResult(ok, "verified" if ok else "bad-signature", current)


def verify_model(
    model: str, version: str, artifact_paths: Iterable[Path], *, root: Path | None = None
) -> VerifyResult:
    """Verify a model's signature against the current artifact bytes (spec R6).

    Every check runs on every call — see the note at the top of this module for why the R8 cache
    was removed rather than re-keyed.
    """
    from examlops.data.registry import get_model_signature

    row = get_model_signature(model, version)
    if row is None:
        return VerifyResult(False, "unsigned: no signature on record")
    return verify_record(row, model, version, artifact_paths, root=root)


def verify_before_load(
    model: str,
    version: str,
    artifact_paths: Iterable[Path],
    *,
    mode: str = "enforce",
    root: Path | None = None,
    record: dict | None = None,
) -> bool:
    """Gate model loading on verification (spec R7), then on the policy engine's supply-chain gate.

    ``enforce`` → return False (refuse) on failure; ``warn`` → return True but record. When the
    ``supply_chain`` policy gate is armed (``examlops.policy_engine.gates``; off by default, so this
    is unchanged unless a site opts in) an unverified artifact is also denied by the engine —
    which can only tighten: ``warn`` mode cannot loosen an armed *enforce* gate.
    """
    allowed, verified = _verify_before_load(
        model, version, artifact_paths, mode=mode, root=root, record=record
    )
    try:
        from examlops.policy_engine import supply_chain_gate
        from examlops.policy_engine.gates import consult
    except ImportError:  # a slim serving image without the policy engine keeps the base behaviour
        return allowed
    gate = consult("supply_chain", lambda _opts: supply_chain_gate(model, version, signed=verified))
    if gate is not None and not gate.allow:
        return False
    return allowed


def _verify_before_load(
    model: str,
    version: str,
    artifact_paths: Iterable[Path],
    *,
    mode: str = "enforce",
    root: Path | None = None,
    record: dict | None = None,
) -> tuple[bool, bool]:
    """``(allowed, verified)`` — gate model loading on verification (spec R7).

    ``enforce`` → return False (refuse) on failure; ``warn`` → return True but record.

    A verification that cannot *run* counts as a failure, not as a pass. Deciding this needs two
    reads that can fail on their own account — the recorded signature comes from the datastore and
    the digest comes from the artifact files on disk — and when either raises, the question "do
    these bytes match their signature?" has no answer. Letting the exception escape pushed that
    decision onto whichever caller was least equipped to make it: the KServe loader hook answered
    it with ``return True``.
    """
    try:
        if record is not None:  # the signature a serving snapshot carries (no datastore read)
            result = verify_record(record, model, version, artifact_paths, root=root)
        else:
            result = verify_model(model, version, artifact_paths, root=root)
    except Exception as exc:  # noqa: BLE001 - any failure to verify is a failure to verify
        _audit(
            "model_verify_error",
            model,
            version,
            "serving",
            {"reason": f"verification could not run: {exc}", "mode": mode},
        )
        return mode != "enforce", False
    if result.ok:
        return True, True
    _audit(
        "model_verify_failed", model, version, "serving", {"reason": result.reason, "mode": mode}
    )
    return mode != "enforce", False  # warn may still load; enforce refuses


def generate_ai_bom(
    model: str,
    version: str,
    *,
    dataset: str | None = None,
    dataset_revision: str | None = None,
    framework: str | None = None,
    eval_summary: dict | None = None,
    dependencies: list[str] | None = None,
) -> dict:
    """Emit a CycloneDX-style AI-BOM for a model version (spec R3/R4)."""
    from examlops.data.registry import store_model_bom

    deps = dependencies or _key_dependency_versions()
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {
            "timestamp": _now(),
            "component": {"type": "machine-learning-model", "name": model, "version": str(version)},
        },
        "components": [
            {
                "type": "data",
                "name": dataset or "unknown",
                "version": dataset_revision or "unknown",
            },
            *[
                {"type": "library", "name": d.split("==")[0], "version": d.split("==")[-1]}
                for d in deps
            ],
        ],
        "properties": [
            {"name": "examlops:framework", "value": framework or "unknown"},
            {"name": "examlops:dataset_revision", "value": dataset_revision or "unknown"},
            {"name": "examlops:eval", "value": str(eval_summary or {})},
        ],
    }
    store_model_bom(model, version, bom)
    return bom


def _key_dependency_versions() -> list[str]:
    out = []
    for pkg in ("mlflow", "scikit-learn", "torch", "numpy", "pandas"):
        try:
            import importlib.metadata as md

            out.append(f"{pkg}=={md.version(pkg)}")
        except Exception:
            continue
    return out


def _audit(action: str, model: str, version: str, actor: str | None, extra: dict) -> None:
    # Still fails open — a signature must not be refused because the audit store blinked — but
    # the loss is counted rather than hidden.
    from examlops.data.audit import audit_best_effort

    audit_best_effort("exa-supplychain", actor, action, f"{model}@{version}", extra)
