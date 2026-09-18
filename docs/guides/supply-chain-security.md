# ML supply-chain security — signing, AI-BOM, verify-before-load

ExaMLOps signs model artifacts, records a **CycloneDX AI-BOM** per version, and
**verifies signature + integrity before a model is served**. This closes the gap
between "a model is in the registry" and "this is the exact, untampered model we
trained and approved."

Design: ADR 0013 · spec `design/vision/specs/D3-supply-chain-security.md`.
Backed by `platform_db.model_signatures` + `platform_db.model_boms`.

## Signing schemes

| Scheme | `algo` | Who holds what | Use |
|---|---|---|---|
| **Ed25519 key pair** | `ed25519-v2` | The signer (training pipeline, release operator) holds the **private** key; serving replicas hold only **public** keys | Default whenever a private key is configured |
| HMAC-SHA256 (legacy) | `hmac-sha256` | Signer **and every verifier** hold the same secret, so any verifier can forge | Only when no Ed25519 key is configured; existing rows keep verifying |

An Ed25519 signature covers a statement that binds three things together: the model name
(case-insensitive), the **version**, and a **manifest digest**. The manifest digest is SHA-256
over a canonical JSON list of every file's relative path, size and SHA-256. This closes three gaps
in the legacy HMAC digest:

- Changing any byte, adding or removing a file, **renaming or moving a file inside the bundle**
  all change the digest. The legacy digest hashed file names and bytes run together, so `a`
  containing `bc` and `ab` containing `c` collided, and directories were ignored.
- A signature cannot be **moved to another version**. Under HMAC, a registry row for version 4
  pointing at version 3's bytes could reuse version 3's signature: a silent rollback.
- A **forged** signature fails, because verifiers cannot sign. A signature made with a key
  outside the trust bundle is refused as `untrusted-key`.

Keyless Sigstore signing (Fulcio certificates, a transparency log) is not implemented. The
`cert` column holds the Ed25519 key id today and is where a certificate would go.

### Keys

```bash
openssl genpkey -algorithm ed25519 -out signing.pem          # private: the signer only
openssl pkey -in signing.pem -pubout -out signing.pub.pem    # public: PEM trust bundle
```

| Variable | Where | Holds |
|---|---|---|
| `EXAMLOPS_SIGNING_PRIVATE_KEY_FILE` (or secret `model-signing/ed25519-private`) | Prefect runner / training workers, release operators | the private key |
| `EXAMLOPS_SIGNING_PUBLIC_KEYS` | Ray Serve, and anyone who verifies | comma-separated base64 raw public keys, one line (`exa models sign` prints the signer's) |
| `EXAMLOPS_SIGNING_PUBLIC_KEYS_FILE` | same | one or more PEM public keys, concatenated |

**Rotation:** generate a new key, add its public key to the trust bundle everywhere, switch the
signer to it, then re-sign the versions still served (`exa models sign <model> <version>`). Remove
the old public key only after that; until then versions it signed keep verifying, because each
signature row records its key id.

## Signing at registration

The training pipeline signs every version it registers, right after registration: a signature
needs a version to name. It signs **the registered version's artifacts as the serving plane will
download them** (`models:/<model>/<version>`), so signer and verifier hash the same tree.
`EXAMLOPS_SIGN_AT_REGISTRATION` sets the policy:

| Value | Behaviour |
|---|---|
| `auto` (default) | Sign when a signing key is configured, and skip quietly when none is |
| `required` | Fail the training run when the version cannot be signed, so an unsigned version never reaches a serving plane that enforces verification |
| `off` | Never sign |

The signature row is copied into the **serving snapshot** (ADR 0127). A replica therefore
verifies against the snapshot's record, including while the datastore is unreachable. The record
is only data: it passes only if it verifies under a key in the replica's trust bundle.

### "Unsigned" has two causes, and they are not the same

*A specific case of [honest degradation](honest-degradation.md) — the platform-wide rule.*

Two other surfaces sign an identity and record the result — the fine-tuning adapter registry
(`exa finetune`) and [reproducibility bundles](reproducibility-bundles.md). Both can end up
recording no signature, for reasons that look identical in the data and are not:

| Cause | What it means | What you see |
|---|---|---|
| **No signing key configured** | a site's choice; unsigned is the documented outcome | nothing — it degrades quietly |
| **Signing failed** | a malformed key, an unreachable secret store, a bug in the signer | a warning naming the subject and the error, saying explicitly that this is *not* the same as having no key |

Until 2026-09-13 both paths caught every exception and returned the same empty signature, so a
broken signer was indistinguishable from policy — permanently, because nothing anywhere said
otherwise. They now share `examlops.supplychain.sign_or_explain()`, which degrades quietly **only**
for a missing key. The command still succeeds either way: a signing fault should not cost someone
their fine-tuning run, but it must not pass unnoticed either. If you need unsigned to be fatal
rather than merely visible, that is what `EXAMLOPS_SIGN_AT_REGISTRATION=required` is for on the
model-signing path.

## CLI

```bash
# Sign / verify the registered version (downloaded exactly as serving downloads it)
exa models sign JPCP 17
exa models verify JPCP 17                  # exit 1 on failure (enforce)
exa models verify JPCP 17 --mode warn      # record only, never blocks

# Or a local artifact directory (relative paths inside it are part of the signature)
exa models sign JPCP 17 --path ./artifacts/jpcp
exa models verify JPCP 17 --path ./artifacts/jpcp

# Emit a CycloneDX AI-BOM (data revision + framework + key deps)
exa models bom JPCP 17 --dataset FData --dataset-revision abc123 --framework sklearn
exa models bom JPCP 17 --output ./boms/jpcp-17.cdx.json
```

Verification reasons: `verified`, `unsigned`, `tampered` (bytes, file set or layout changed),
`bad-signature` (wrong key, or a signature borrowed from another version), `untrusted-key` (signed
by a key not in the trust bundle), `unknown-algorithm`.

## Rolling out enforcement on Ray Serve

`EXAMLOPS_SERVING_VERIFY` defaults to **`warn`**: every load is verified and a failure is audited
(`model_verify_failed`, with its reason), but nothing is refused. To move to `enforce`:

1. Configure the signer key on the training runner (`EXAMLOPS_SIGNING_PRIVATE_KEY_FILE`) and
   put its public key on Ray Serve (`EXAMLOPS_SIGNING_PUBLIC_KEYS`).
2. Sign the versions already served: `exa models sign <model> <version>` for each alias target.
3. Watch for `model_verify_failed` / `model_verify_error` audit events (`exa audit --last 1d`)
   until none arrive.
4. Set `EXAMLOPS_SIGN_AT_REGISTRATION=required` on the runner, then
   `EXAMLOPS_SERVING_VERIFY=enforce` on Ray Serve.

Under `enforce`, a version that fails keeps the replica on its last-known-good version, and the
cached copy that failed is discarded, never kept.

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

# `root` is the bundle's top directory; `record` is an optional signature row (the serving
# snapshot's copy) to check instead of reading `model_signatures`.
if not supplychain.verify_before_load(model, version, paths, mode="enforce", root=bundle_dir):
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
