# ML supply-chain security — signing, AI-BOM, verify-before-load

ExaMLOps signs model artifacts, records a **CycloneDX AI-BOM** per version, and
**verifies signature + integrity before a model is served**. This closes the gap
between "a model is in the registry" and "this is the exact, untampered model we
trained and approved."

Design: ADR 0013 · spec `design/vision/specs/D3-supply-chain-security.md`.
Backed by `platform_db.model_signatures`, `platform_db.model_boms` and
`platform_db.model_provenance`.

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

The `cert` column holds the Ed25519 key id for `ed25519-v2` rows and the signer identity for
`sigstore-v1` rows (next section).

### Keyless signing with Sigstore

`EXAMLOPS_SIGNING_SCHEME=sigstore` switches the signer to **Sigstore keyless** signing
(`sigstore-v1`). The signer holds no long-lived key: it presents an OIDC identity token to
**Fulcio**, receives a short-lived certificate for an ephemeral key, and the signature is logged
in the **Rekor** transparency log. The Sigstore bundle (certificate, signature, inclusion proof)
is stored as the signature. It covers the same statement an Ed25519 signature covers (model,
version, manifest digest), so a bundle cannot be moved to another version either.

Install the optional extra first: `pip install 'examlops[supplychain]'` (the `sigstore` library,
imported only on this path).

| Variable | Where | Meaning |
|---|---|---|
| `EXAMLOPS_SIGNING_SCHEME` | signer | `auto` (default: Ed25519 when a private key is configured, else legacy HMAC) or `sigstore`. Any other value refuses to sign rather than silently picking a scheme |
| `EXAMLOPS_SIGSTORE_IDENTITY_TOKEN` / `EXAMLOPS_SIGSTORE_IDENTITY_TOKEN_FILE` | signer | The OIDC token. Unset → the ambient CI credential (GitHub Actions, GitLab, Buildkite, GCP). There is no interactive browser flow |
| `EXAMLOPS_SIGSTORE_IDENTITIES` | every verifier | Comma-separated identity–issuer pairs (identity, a pipe character, issuer) allowed to sign models — see the example below |
| `EXAMLOPS_SIGSTORE_INSTANCE` | both | `production` (default) or `staging` |
| `EXAMLOPS_SIGSTORE_OFFLINE` | verifiers | Truthy → verify with the trusted root already in the local TUF cache (air-gapped HPC) |

For example:

```bash
export EXAMLOPS_SIGSTORE_IDENTITIES='https://github.com/MSKazemi/ExaMLOps/.github/workflows/train.yml@refs/heads/main|https://token.actions.githubusercontent.com'
```

Verification trusts **identities, not keys**, and fails closed: an empty
`EXAMLOPS_SIGSTORE_IDENTITIES` answers `untrusted-identity`; an entry without an issuer is
dropped rather than widened to "any issuer"; a missing `sigstore` library answers `unavailable`,
which `enforce` refuses like any other failure to verify. A verifier that only ever sees
Ed25519/HMAC rows does not need the extra.

**Offline fallback.** When keyless signing cannot run — no OIDC token, Fulcio or Rekor
unreachable — and an Ed25519 private key is configured, the signer falls back to it (the
"D7-managed key" the ADR names for offline HPC). The fallback is audited as
`model_sign_keyless_fallback` with the reason. With no key to fall back to, signing fails.
Provenance signing falls back the same way and is audited as `model_provenance_keyless_fallback`
(with no key it is recorded unsigned, which never verifies).

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

## SLSA build provenance

Every version the pipeline registers can carry **SLSA v1 provenance**: an in-toto Statement v1
whose subject is `models:/<model>/<version>` with the manifest digest the signature covers, and
whose predicate records the build — model, dataset and pinned dataset revision, backend,
framework, parameters (bounded to 64 × 1 KiB), the source commit (`EXAMLOPS_SOURCE_COMMIT`,
`GITHUB_SHA` or `CI_COMMIT_SHA`) and repository (`EXAMLOPS_SOURCE_REPOSITORY`), the builder id (`EXAMLOPS_BUILDER_ID`), the MLflow run id and
the HPC job id and scheduler.

The statement is signed in a **DSSE** envelope with the Ed25519 signing key (`ed25519-dsse`,
PAE-encoded per DSSE v1), or — under `EXAMLOPS_SIGNING_SCHEME=sigstore` — keyless into a Sigstore
bundle with a Rekor entry (`sigstore-v1`). With no key at all it is recorded **unsigned**
(`none`): it documents the build, but it is not evidence and never verifies.

Each record is also anchored in the hash-chained audit trail: the `model_provenance_recorded`
event carries the SHA-256 of the exact envelope stored, so rewriting the `model_provenance` row
later is detectable from the chain (and from the WORM anchor, when `EXAMLOPS_AUDIT_WORM_PATH` is
set). Re-recording the same build is a no-op; a *different* statement for a version that already
has one is refused unless `--replace` is given, and the replacement is audited too. The write
itself is insert-if-absent, so two recorders racing on one version cannot silently overwrite each
other. A build first recorded **unsigned** (no key at the time) is signed by re-recording the same
build once a key exists — that adds the missing signature, so it needs no `--replace`.

### SLSA build type: training v1

`buildDefinition.buildType` is
`https://github.com/MSKazemi/ExaMLOps/blob/main/docs/guides/supply-chain-security.md#slsa-build-type-training-v1`:

| Field | Content |
|---|---|
| `externalParameters` | `model`, `version`, `dataset`, `datasetRevision`, `backend`, `framework`, `parameters` |
| `internalParameters` | `examlops` — the platform version that ran the build |
| `resolvedDependencies` | `dataset:<name>` (annotation `revision`) and the source repository (digest `gitCommit`) |
| `runDetails.metadata.invocationId` | the MLflow run id |
| `runDetails.byproducts` | `hpc-job` with `jobId` and `scheduler` |

### At registration

`EXAMLOPS_PROVENANCE_AT_REGISTRATION` sets what the training pipeline records after signing:

| Value | Behaviour |
|---|---|
| `auto` (default) | Record the AI-BOM and provenance whenever signing produced a manifest digest (Ed25519 or Sigstore) — no second artifact download; skip otherwise |
| `required` | Always record them, downloading the registered artifacts when signing did not; fail the training run when it cannot |
| `off` | Never |

The AI-BOM recorded here carries an `examlops:artifact_digest` property, which binds it to the
signed bytes.

## Release gate

`exa models release-check <model> <version>` exits 1 unless the version carries the required
evidence (default: all three):

| Requirement | Passes when |
|---|---|
| `signature` | a signature row exists **and** is a valid Ed25519 / Sigstore signature by a trusted key or identity over its recorded digest (`bad-signature` / `untrusted-key` otherwise). A legacy HMAC row is refused (`legacy-hmac`): it is a shared-secret MAC over a different digest, so nothing else can be bound to it. Whether the artifact bytes still match is verify-before-load's job |
| `bom` | an AI-BOM is recorded, names an artifact digest (`unbound` otherwise — re-run `exa models attest`), and that digest equals the verified signature's digest, or with no verified signature the verified provenance subject (`bom-mismatch` otherwise, also when two recorded BOMs for the version bind different digests) |
| `provenance` | provenance is recorded, its signature verifies under the trust bundle / trusted identities, and its subject digest equals the signed digest |

The same check governs **promotion** when the policy engine's `supply_chain` gate is armed with
a `require` list:

```yaml
# policy.yaml
gates:
  supply_chain:
    mode: enforce          # or monitor: audit a would-deny, never block
    require: [signature, bom, provenance]
```

`exa pipeline promote` then refuses a version that lacks any of them (not overridable by
`--force`), and so does the **autopilot**, which promotes on its own road: an armed
`supply_chain` gate is consulted for the Staging candidate there too (blocked cycles are audited
as `autopilot_promote_blocked`), and a candidate version that cannot be resolved is refused. Without `require`, the gate keeps its original meaning (signature only). A misspelt
requirement denies: a typo must not drop a check.

## CLI

```bash
# Sign / verify the registered version (downloaded exactly as serving downloads it)
exa models sign JPCP 17
exa models verify JPCP 17                  # exit 1 on failure (enforce)
exa models verify JPCP 17 --mode warn      # record only, never blocks

# Or a local artifact directory (relative paths inside it are part of the signature)
exa models sign JPCP 17 --path ./artifacts/jpcp
exa models verify JPCP 17 --path ./artifacts/jpcp

# Record signed SLSA provenance + a digest-bound AI-BOM (versions registered before this existed)
exa models attest JPCP 17 --dataset FData --dataset-revision abc123
exa models attest JPCP 17 --path ./artifacts/jpcp --sign

# Show and verify provenance (exit 1 when it does not verify); export the signed envelope
exa models provenance JPCP 17
exa models provenance JPCP 17 --output ./evidence/jpcp-17.intoto.json

# CI gate: signed + bound AI-BOM + verified provenance, or exit 1
exa models release-check JPCP 17
exa models release-check JPCP 17 --require signature,provenance

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
