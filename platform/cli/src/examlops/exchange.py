"""NovaFabric Exchange — signed, verifiable cross-institution packages (Phase 5 item 5.6).

A portable ``.novapack`` bundle for sharing models / pipelines / providers / policy-packs between
institutions, built on the D3 supply-chain signing already in the platform. Every package carries a
manifest with a per-file SHA-256 set + a signature over the bundle digest, and **verify-before-import**
refuses to unpack anything whose signature or integrity fails — so a shared artifact can't be tampered
with in transit or swapped for a malicious one. This is the packaging + trust core; the discovery/
marketplace surface (MCP/A2A listing, Projects scoping) layers on top.

Container is a stdlib zip (portable, no deps). Signing reuses ``supplychain._hmac_sign`` (the D7 key),
so it fails closed when no signing key is configured — an unsigned package is never silently trusted.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MANIFEST = "novapack.manifest.json"
_VALID_KINDS = ("model", "pipeline", "provider", "policy")


class ExchangeError(RuntimeError):
    """Raised when a package is malformed, unsigned, or fails verification (fail-closed)."""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    reason: str
    manifest: dict[str, Any] | None = None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _bundle_digest(files: dict[str, str]) -> str:
    """Deterministic digest over the sorted ``{arcname: sha256}`` map."""
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def pack(
    kind: str,
    name: str,
    files: list[str | Path],
    out_path: str,
    *,
    version: str = "1",
    metadata: dict[str, Any] | None = None,
    created_at: str = "",
) -> dict[str, Any]:
    """Build a signed ``.novapack`` from ``files``. Returns the manifest. Fails closed if unsigned."""
    if kind not in _VALID_KINDS:
        raise ExchangeError(f"kind must be one of {_VALID_KINDS}, got {kind!r}")
    from examlops.supplychain import SigningKeyMissing, _hmac_sign

    paths = [Path(f) for f in files]
    for p in paths:
        if not p.is_file():
            raise ExchangeError(f"not a file: {p}")
    file_hashes = {p.name: _sha256_file(p) for p in paths}
    digest = _bundle_digest(file_hashes)
    try:
        signature = _hmac_sign(digest)
    except SigningKeyMissing as exc:
        raise ExchangeError(
            f"cannot sign package: {exc}. Set EXAMLOPS_SIGNING_KEY — unsigned packages are refused."
        ) from exc

    manifest = {
        "format": "novapack-v1",
        "kind": kind,
        "name": name,
        "version": version,
        "created_at": created_at,
        "files": file_hashes,
        "digest": digest,
        "signature": signature,
        "algo": "hmac-sha256",
        "metadata": metadata or {},
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_MANIFEST, json.dumps(manifest, indent=2))
        for p in paths:
            zf.write(p, arcname=p.name)
    return manifest


def inspect(pack_path: str) -> dict[str, Any]:
    """Read a package's manifest without extracting/verifying its files."""
    with zipfile.ZipFile(pack_path) as zf:
        if _MANIFEST not in zf.namelist():
            raise ExchangeError("not a novapack: missing manifest")
        return json.loads(zf.read(_MANIFEST))


def verify(pack_path: str) -> VerifyResult:
    """Verify a package: signature over the bundle digest + every file's hash. Never raises."""
    try:
        with zipfile.ZipFile(pack_path) as zf:
            names = zf.namelist()
            if _MANIFEST not in names:
                return VerifyResult(False, "missing manifest")
            manifest = json.loads(zf.read(_MANIFEST))
            # Recompute each file's hash from the archived bytes.
            actual: dict[str, str] = {}
            for arc, expected in manifest.get("files", {}).items():
                if arc not in names:
                    return VerifyResult(False, f"file missing from package: {arc}", manifest)
                actual[arc] = hashlib.sha256(zf.read(arc)).hexdigest()
                if actual[arc] != expected:
                    return VerifyResult(False, f"tampered file: {arc}", manifest)
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
        return VerifyResult(False, f"corrupt package: {exc}")

    if _bundle_digest(actual) != manifest.get("digest"):
        return VerifyResult(False, "digest mismatch", manifest)

    from examlops.supplychain import SigningKeyMissing, _hmac_sign

    try:
        import hmac as _hmac

        if not _hmac.compare_digest(_hmac_sign(manifest["digest"]), manifest.get("signature", "")):
            return VerifyResult(False, "bad signature — untrusted or wrong key", manifest)
    except SigningKeyMissing:
        return VerifyResult(False, "no signing key to verify the package signature", manifest)
    return VerifyResult(True, "verified", manifest)


def import_pack(pack_path: str, dest: str, *, require_signature: bool = True) -> dict[str, Any]:
    """Verify-before-import: verify, then extract the files to ``dest``. Raises on failure.

    ``require_signature=False`` (NOT recommended) extracts after integrity-only checks, for the
    air-gapped/no-key case — but logs no trust. Returns the manifest.
    """
    result = verify(pack_path)
    if not result.ok:
        if require_signature or "signing key" not in result.reason:
            raise ExchangeError(f"refusing to import an unverified package: {result.reason}")
    Path(dest).mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pack_path) as zf:
        for arc in zf.namelist():
            if arc != _MANIFEST:
                zf.extract(arc, dest)
    return result.manifest or inspect(pack_path)
