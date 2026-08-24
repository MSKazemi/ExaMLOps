# ML supply-chain security — signing, AI-BOM, verify-before-load

ExaMLOps signs model artifacts, records a **CycloneDX AI-BOM** per version, and
**verifies signature + integrity before a model is served**. This closes the gap
between "a model is in the registry" and "this is the exact, untampered model we
trained and approved."

Design: ADR 0013 · spec `design/vision/specs/D3-ml-supply-chain-security.md`.
Backed by `platform_db.model_signatures` + `platform_db.model_boms`.

## Signing backends (graceful degrade)

- **Production**: Sigstore / OpenSSF model-signing — keyless signatures via an OIDC
  identity (Fulcio short-lived cert), verifiable against a transparency log.
- **Fallback** (no OIDC identity, default in dev/CI): an **HMAC-SHA256** signature
  keyed by a D7-managed signing key. Set `EXAMLOPS_SIGNING_KEY`, or store a secret at
  `model-signing/key` (`exa secrets set model-signing/key`). No external service needed.

The bundle digest is a SHA-256 over the sorted artifact file set, so any change to any
artifact byte (or the file set) changes the digest and invalidates the signature.

## CLI

```bash
# Sign a model's artifact bundle (a local file or directory)
exa models sign JPCP 17 --path ./artifacts/jpcp

# Verify current artifact bytes against the recorded signature
exa models verify JPCP 17 --path ./artifacts/jpcp            # exit 1 on failure (enforce)
exa models verify JPCP 17 --path ./artifacts/jpcp --mode warn  # record only, never blocks

# Emit a CycloneDX AI-BOM (data revision + framework + key deps)
exa models bom JPCP 17 --dataset FData --dataset-revision abc123 --framework sklearn
exa models bom JPCP 17 --output ./boms/jpcp-17.cdx.json
```

## Verify-before-load gate

`verify_before_load(model, version, artifact_paths, mode=...)` is the enforcement point
for serving and CI:

- **`enforce`** — a signature/integrity failure returns `False`: the loader must refuse.
  Wire this into `exa pipeline promote` / serving startup as a hard gate.
- **`warn`** — records a `model_verify_failed` audit event and returns `True`: the model
  may still load, but the failure is visible for triage.

A verification that cannot **run** counts as a failure, not as a pass. Deciding the answer needs
the recorded signature (a datastore read) and a digest of the artifact files (a disk read), and
either can fail on its own account — an unreachable signature store, an unreadable path. When that
happens the question has no answer, so `enforce` refuses and `warn` still loads, exactly as for a
signature mismatch, and a `model_verify_error` audit event records why. Alert on that event: it
means artifacts are being served without their signatures having been checked.

```python
from examlops import supplychain

if not supplychain.verify_before_load(model, version, paths, mode="enforce"):
    raise RuntimeError(f"refusing to load unverified {model}@{version}")
```

Verification is **never cached**. Both the artifact hash and the signature comparison run on every
call, so a `verified` answer is always about the bytes on disk right now, and swapping an artifact
between two loads is caught by the second one. The cost is one SHA-256 pass over the artifact
bundle per load — roughly 2 ms for a 1 MB model, 77 ms for 50 MB. Budget for it on the load path
rather than trying to avoid it: a cached verdict about a model's bytes is stale the moment those
bytes, its signature record or the signing key change. (Spec D3 R8 originally asked for a
per-digest cache; it is withdrawn — see the 2026-08-24 amendment on ADR 0013.)

Every sign and every verify-failure is written to `audit_events` (source
`exa-supplychain`), so tamper attempts and unsigned-load attempts are auditable (D4).

## AI-BOM contents

The BOM is CycloneDX 1.6 and links the model to its **provenance**: the training
dataset + pinned revision (A1), the ML framework, key library versions, and an eval
summary. Store it alongside the model card (D-cards) for a complete provenance record.
