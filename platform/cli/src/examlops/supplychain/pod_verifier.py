"""The in-pod storage initializer that downloads, then verifies (ADR 0142 d3, spec-usar-1 R-SUB-20).

Run as ``python -m examlops.supplychain.pod_verifier SRC_URI DEST [SRC_URI DEST ...]`` — the same
argv KServe gives its own ``storage-initializer`` — from the image the platform's
``ClusterStorageContainer`` (or an ``LLMInferenceService``'s storage-initializer override) names.
It downloads with KServe's own ``kserve_storage`` (the ``kserve-storage`` distribution, pinned to
the KServe release), then checks the downloaded bytes against the D3 signature record the rendered
object carried in, with :func:`examlops.supplychain.verify_record`.

Inputs (env; the rendered object sets them — from pod annotations through the downward API on an
``InferenceService``, as literal env on an ``LLMInferenceService``):

``EXAMLOPS_VERIFY_MODE``      ``off`` | ``warn`` | ``enforce``; empty ⇒ not a platform servable
``EXAMLOPS_VERIFY_MODEL``     the model name the signature statement binds
``EXAMLOPS_VERIFY_VERSION``   the version it binds
``EXAMLOPS_VERIFY_RECORD``    the public signature record (JSON: algo, digest, signature, cert)
``EXAMLOPS_SIGNING_PUBLIC_KEYS``  the Ed25519 trust bundle (from a ConfigMap)

Exit status is the contract with the kubelet: **non-zero stops the pod** before the model server
starts. ``enforce`` exits 1 on a failed *or unanswerable* verification (no record, no files, no
trust key, a verifier that raised) — a verifier that cannot answer has not answered yes. ``warn``
always exits 0 once the download succeeded. A pod with no mode and no model annotation is not one
the platform rendered (the storage container can be auto-matched by other workloads): it is
downloaded and passed through, and says so.

Nothing here touches ``platform.db`` — a pod has none. The one record of the decision is a single
JSON line on stderr (``event: examlops.verify_before_load``), which Promtail/Loki already collect
from pod logs; the platform-side audit event is the substrate apply that rendered the pod.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from examlops.supplychain import ED25519, verify_record

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

DownloadFn = Callable[[list[str], list[str]], None]


def _kserve_download(src_uris: list[str], dest_paths: list[str]) -> None:
    """KServe's own downloader, exactly as its ``initializer-entrypoint`` calls it (v0.20.0)."""
    from kserve_storage import Storage  # the `kserve-storage` distribution, in the image only

    Storage.download_files(src_uris, dest_paths)


def _log(**fields: Any) -> None:
    print(
        json.dumps({"event": "examlops.verify_before_load", **fields}, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )


def verify_dir(root: Path, *, model: str, version: str, record_json: str) -> tuple[bool, str, str]:
    """``(ok, reason, digest)`` for every file under ``root`` against ``record_json``.

    Every failure to *answer* is a ``False`` with a reason, never an exception.
    """
    if not model or not version:
        return False, "unanswerable: no model/version to bind the signature to", ""
    if not record_json.strip():
        return False, "unsigned: no signature on record", ""
    try:
        record = json.loads(record_json)
    except ValueError as exc:
        return False, f"unanswerable: signature record is not JSON ({exc})", ""
    if not isinstance(record, dict) or not record.get("digest") or not record.get("signature"):
        return False, "unanswerable: signature record lacks digest/signature", ""
    if record.get("algo") != ED25519:
        # An HMAC record verifies only with the shared signing key, which would let the pod forge;
        # looking for it would also send the pod to a secret store and a platform.db it has none
        # of. Only a public-key record can be answered here.
        return (
            False,
            f"unanswerable: {record.get('algo') or 'hmac-sha256'!r} records cannot be verified in "
            "a pod (sign with Ed25519 for Kubernetes serving)",
            "",
        )
    if not root.is_dir():
        return False, f"unanswerable: {root} is not a directory", ""
    paths = [p for p in root.rglob("*") if p.is_file()]
    if not paths:
        return False, f"unanswerable: nothing was downloaded into {root}", ""
    try:
        result = verify_record(record, model, version, paths, root=root)
    except Exception as exc:  # noqa: BLE001 - a verifier that raised has not answered "yes"
        return False, f"unanswerable: verification could not run ({type(exc).__name__}: {exc})", ""
    return result.ok, result.reason, result.digest


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    download: DownloadFn | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if environ is None else environ
    if len(args) < 2 or len(args) % 2:
        print(
            "usage: python -m examlops.supplychain.pod_verifier SRC_URI DEST [SRC_URI DEST ...]",
            file=sys.stderr,
        )
        return EXIT_USAGE
    srcs, dests = args[0::2], args[1::2]
    model = (env.get("EXAMLOPS_VERIFY_MODEL") or "").strip()
    version = (env.get("EXAMLOPS_VERIFY_VERSION") or "").strip()
    raw_mode = (env.get("EXAMLOPS_VERIFY_MODE") or "").strip().lower()
    if not raw_mode:
        # Rendered by the platform ⇒ it always carries a mode. A model without one is a
        # half-rendered object: fail closed. Neither ⇒ some other workload matched the container.
        mode = "enforce" if model else "passthrough"
    elif raw_mode in ("off", "warn", "enforce"):
        mode = raw_mode
    else:
        mode = "enforce"  # a typo must not read as "off"

    try:
        # Destinations exist before any download, as KServe's own entrypoint ensures (HF
        # snapshot_download and the legacy downloader both expect the parent to exist).
        for dest in dests:
            if dest:
                os.makedirs(dest, exist_ok=True)
        (download or _kserve_download)(srcs, dests)
    except Exception as exc:  # noqa: BLE001 - KServe's own entrypoint exits 1 on any failure
        _log(model=model, version=version, mode=mode, ok=False, allowed=False,
             reason=f"download failed: {type(exc).__name__}: {exc}")  # fmt: skip
        return EXIT_REFUSED
    if mode in ("passthrough", "off"):
        _log(model=model, version=version, mode=mode, ok=None, allowed=True,
             reason="not verified (" + ("not a platform servable" if mode == "passthrough"
                                        else "EXAMLOPS_SERVING_VERIFY=off") + ")")  # fmt: skip
        return EXIT_OK
    # A verifier never holds a signing key, and must not go looking for one in a secret store the
    # pod has no business reaching: an empty key file resolves "no local key" immediately.
    os.environ.setdefault("EXAMLOPS_SIGNING_PRIVATE_KEY_FILE", os.devnull)
    if len(dests) != 1:
        # A signature binds one artifact. Verifying only the first of several downloads would let
        # the others reach the model server unchecked, so more than one is unanswerable.
        ok, reason, digest = (
            False,
            f"unanswerable: {len(dests)} artifacts were downloaded; a signature binds exactly one",
            "",
        )
    else:
        ok, reason, digest = verify_dir(
            Path(dests[0]),
            model=model,
            version=version,
            record_json=env.get("EXAMLOPS_VERIFY_RECORD") or "",
        )
    allowed = ok or mode == "warn"
    _log(model=model, version=version, mode=mode, ok=ok, allowed=allowed, reason=reason,
         digest=digest)  # fmt: skip
    return EXIT_OK if allowed else EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - the container entrypoint
    sys.exit(main())
