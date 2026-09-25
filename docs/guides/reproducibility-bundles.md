# Reproducibility Bundles (A8)

> Next-Gen 40 · feature **A8** · ADR 0038 · spec `design/vision/specs/A8-reproducibility-bundles.md`

A reproducibility **bundle** is a *signed manifest* that captures **every input** to a
model version, so that months later you can answer two questions with evidence:

1. **Can I rebuild this?** — `exa reproduce run` documents the exact rebuild plan and, given
   re-observed metrics, checks they match the recorded ones within a documented tolerance.
2. **Is this still reproducible?** — `exa reproduce verify` checks that every referenced
   input still exists and its hash still matches, flagging a bundle that has *rotted*.

## What the manifest captures (R1)

| Input | Captured as | Filled by |
|---|---|---|
| Code | `git rev-parse HEAD` commit SHA | collected |
| All code repos | `code_commits` — the platform checkout **and** the upstream model library (`EXAMLOPS_MODELZOO_DIR`, default `./modelzoo`), each with commit + dirty flag; a library that is not a git checkout is recorded by its distribution version with `commit: null` | collected |
| Dataset | A1 dataset revision id (`<name>@<rev>`) | pipeline / `--revision` |
| Features | A3 feature views, pinned by the SHA-256 of their definition (the store keeps no version counter) | `--feature-view` / `EXAMLOPS_FEATURE_VIEWS` |
| Environment | `uv.lock` SHA-256, the installed package set (`name==version`), Python version, container image digest | collected; digest from `EXAMLOPS_IMAGE_DIGEST` or `--image-digest` |
| Code state | `code_dirty` — whether tracked files differed from the commit (`null` = unknown) | collected |
| Dataset source | for a dataplane snapshot, the source key (`dataset_source`) | pipeline |
| Hyperparameters | recorded key/values | `--hyperparams` |
| Compute | `resources` — the scheduler-neutral request as submitted (`EXAMLOPS_HPC_*` keys), the scheduler and the job id; `hardware` — arch, OS, CPUs, memory, GPUs from `nvidia-smi` | pipeline / `--resources --scheduler`; hardware collected |
| Determinism | RNG seeds | `EXAMLOPS_SEED` / `--seed` |
| Provenance | A2 lineage run id | pipeline (found from the MLflow run) / `--lineage-run-id` |
| Supply chain | `bom` — hash of the version's D3 AI-BOM; generated from the recorded package set when the version has none, an existing BOM is linked, never overwritten | collected |

The manifest is hashed canonically and **signed** with the D3 HMAC key; the build is
audited (D4) and the manifest is versioned.

What a collector could not observe is recorded as such, never guessed: a host without
`nvidia-smi` is `gpu_probe: "unavailable"` (not "no GPUs"), a bundle built at promotion records
`hardware.captured: false` (the promoting host did not train the model), so does a pipeline
bundle whose training ran as a Slurm or Flux job (the compute node the scheduler chose is not
the host that built the bundle; the job id is kept), the mock scheduler —
which trains inline — records an empty request, and a malformed `EXAMLOPS_IMAGE_DIGEST` is not
recorded at all.

`EXAMLOPS_IMAGE_DIGEST` is read by the bundle; an image cannot know its own digest at build
time, so the deployment that runs the image has to pass it in (Compose/Helm). The shipped
Compose file and chart do not set it yet — until they do, a pipeline-built bundle records
`image_digest: null` unless the operator exports the variable. A bundle whose hardware is not
captured (promotion, a Slurm/Flux job) does not read it either: that process's image is not the
one that trained.

## Honesty about determinism (R4)

This tool **never claims bit-exactness**. GPU kernel scheduling, non-deterministic cuDNN
ops, and reduction order make exact reproduction unattainable in general. Metric
verification therefore uses a **documented relative tolerance** (default `rmse` within 5%),
and every reproduce result carries the non-determinism caveat and `bit_exact: false`.

Signing degrades with the same honesty: with no signing key available the bundle is stored
**unsigned and marked as such** rather than pretending it was signed.

## Build a bundle

At training or promotion time:

```bash
exa reproduce build JPCP 17 \
    --dataset PM100 --revision <A1-rev> \
    --hyperparams '{"lr": 0.01, "epochs": 50}' \
    --metrics '{"rmse": 4.87}' \
    --seed 42 --image-digest sha256:abc…
# Built bundle v1 for JPCP/17 (hash 9f2c8a1b4e6d0f33…)
```

Set `EXAMLOPS_SIGNING_KEY` (or store the `model-signing/key` secret) so the bundle is
signed; otherwise you'll see an `unsigned` warning.

Add what only the operator knows:

```bash
exa reproduce build JPCP 17 --feature-view jobs \
    --resources '{"nodes": 1, "gpus": 2, "partition": "gpu"}' --scheduler flux \
    --lineage-run-id <A2-run-id>
```

An unknown `--feature-view` refuses the build (exit 1) — a bundle must not claim to pin a view
that does not exist.

## Reproduce & metric-verify

```bash
# Plan + input verification only (no re-run executed here):
exa reproduce run JPCP 17

# With metrics from an actual re-run, verify they match within tolerance:
exa reproduce run JPCP 17 --observed '{"rmse": 4.9}'
#   · checkout code 1df6292…
#   · restore dataset PM100@<rev>
#   · rebuild env from uv.lock
#   · re-run with seeds {'global': 42}
#   · <non-determinism caveat>
# Metrics match recorded values within tolerance (not bit-exact).
```

## Really rebuild: `--execute` (ADR 0038 clause 2)

Without `--execute`, `run` is a plan (above). With it, `exa reproduce run <model> <version>
--execute` performs five ordered steps, each a real check reported with its real outcome; the
first failure stops the run (exit 1) and later steps show `not_run`:

| # | Step | What is actually done | Fails when |
|---|---|---|---|
| 1 | `code` | detached `git worktree` at the bundle's commit (`--repo`, default `.`), **and a second one of the model library at its recorded commit**, which training then imports | no commit recorded, commit not in the repo (never falls back to `HEAD`), recorded library commit in no known checkout, or a dirty library without `--allow-dirty-code` |
| 2 | `dataset` | pinned revision must be recorded; a lakeFS revision's commit must exist in lakeFS; with `--data-path` the local data is hashed against it; **with `--restore-dataset DIR` the revision is put back** (below) | revision unrecorded, lakeFS commit gone/unconfirmable, data hash differs, restore failed. Without `--data-path` (or with `--dummy`) content is **not** verified and the step says `skipped` |
| 3 | `env` | recorded `uv.lock`/`requirements.txt` sha256 vs the file in the checkout; the recorded package set vs this interpreter, **or — with `--rebuild-env` — installed into a fresh venv that step 4 then runs on**; the recorded container image digest against the local Docker runtime | hash or a package differs, a recorded package cannot be resolved, the rebuilt Python's `major.minor` differs, or the recorded image is absent/mismatched. `--allow-env-drift` continues and reports `drift_allowed` |
| 4 | `train` | the pipeline training flow runs in a subprocess **inside the worktree**, pinned to the dataset revision and recorded seed (`EXAMLOPS_SEED`), with the **recorded resources re-requested** as `EXAMLOPS_HPC_*`; scheduler per `--scheduler` (mock unless set) | non-zero exit, timeout, no `EXAMLOPS_REPRO_METRICS=<json>` line, or `--scheduler recorded` on a bundle that recorded none |
| 5 | `compare` | produced vs recorded metrics, relative tolerance `--rtol` (default: the bundle's, else 0.05) | any recorded metric missing, non-finite or out of tolerance; a bundle with no recorded metrics |

```bash
exa reproduce run JPCP 17 --execute --dummy --rtol 0.05
exa reproduce run JPCP 17 --execute --data-path ./data/PM100 --json
# custom trainer (must print EXAMLOPS_REPRO_METRICS={"rmse": 4.9}); runs in the checkout:
exa reproduce run JPCP 17 --execute --train-cmd "python train.py"
# put the recorded environment back, and train on it — not on the caller's interpreter:
exa reproduce run JPCP 17 --execute --rebuild-env --dummy
# put the pinned dataset back, and train on the scheduler that ran the original:
exa reproduce run JPCP 17 --execute --restore-dataset ./restored --scheduler recorded
```

### `--restore-dataset`: putting the data back

| Revision kind | What `--restore-dataset DIR` does |
|---|---|
| dataplane snapshot | materialises the snapshot into `DIR/<revision>/` — the revision id must hash back from the manifest and every part is checksum-verified (the training-time check) |
| lakeFS commit (`lakefs://<repository>/<commit>`) | confirms the commit exists, lists every object at it (paged) and downloads each into `DIR`, checking its size and — when lakeFS reports a plain MD5 ETag — its MD5 |
| content hash | **refused**: a content revision records a hash, not a source, so there is nothing to restore from; `--data-path` verifies local data against it instead |

`DIR` must be empty (or absent) — a restore never mixes with other data — and on any failure
what was written is removed, so a half-restored directory never looks like the pinned
revision. The restored path reaches the training subprocess as `EXAMLOPS_REPRO_DATA_DIR`
(a custom `--train-cmd` reads it; the default pipeline flow reads its own backend, pinned to
the same revision). A lakeFS restore is bounded (100 000 objects, 50 GiB by default), refuses
object paths that would escape `DIR`, and authenticates with
`EXAMLOPS_LAKEFS_ACCESS_KEY_ID` / `EXAMLOPS_LAKEFS_SECRET_ACCESS_KEY` (HTTP basic) against
`EXAMLOPS_LAKEFS_ENDPOINT`; credentials never appear in a message.

### `--scheduler`: re-running with the recorded resources

The bundle's `resources.requested` is always exported to the training subprocess as
`EXAMLOPS_HPC_*`, so a rebuild on a real scheduler asks for what the original asked for; when a
request was recorded, the caller's own `EXAMLOPS_HPC_*` and legacy `EXAMLOPS_SLURM_*` resource
variables are cleared first, so nothing the original did not ask for joins it. Where
it runs: `--scheduler recorded` uses the bundle's scheduler, `mock`/`slurm`/`flux` name one,
and no option keeps the caller's `EXAMLOPS_HPC_SCHEDULER` (mock unless set). The scheduler and
the re-requested variables are in the `train` step detail and in `--json` (`scheduler`,
`resources`). Hardware differences from the record (architecture, GPU models) are reported in
the `env` step detail and never fail it — metrics are compared within a tolerance precisely
because the hardware may differ.

### Dataset and environment checks (what "verified" means)

- **Dataplane snapshot** (ADR 0130): a bundle whose `dataset_source` is a dataplane snapshot is
  verified against the snapshot manifest — the revision id must hash back from the manifest's
  digests and every part is downloaded and checksummed (the same `materialize` check training
  runs). A missing snapshot, an unreachable store or a modified part fails (exit 1); nothing is
  reported `ok` without that check. The download goes to a temporary directory. This is done even
  with `--dummy`.
- **Package level**: the recorded package set is compared with the current interpreter, per
  package (`changed`, `missing`; extra packages are reported, not drift). Drift fails `verify` and
  the `env` step unless `--allow-env-drift`, then it is a warning / `drift_allowed`. A bundle from
  before this feature has no package set and only gets the lockfile check.
- **Dirty code**: a bundle built from a dirty tree (`code_dirty: true`) cannot be rebuilt from its
  commit; `--execute` fails the `code` step unless `--allow-dirty-code`, and `verify` warns.

### `--rebuild-env`: putting the environment back, not just diffing it

Comparing the recorded package set with the caller's interpreter answers *"is this machine still
the machine?"*. `--rebuild-env` answers the question the ADR actually asks: it creates a **fresh,
isolated virtualenv** (`uv venv`) and installs exactly the recorded distributions at the recorded
versions (`uv pip install --no-deps` — a recorded set is a `pip freeze`, an already-closed
dependency set), then step 4 trains **on that interpreter**. The interpreter is named in the
`train` step detail and in `--json` (`environment.python`, `environment.venv`,
`environment.rebuilt`).

What it will not do:

- **It never mutates the caller's environment**, and never touches the repository's shared
  `.venv`. The venv is thrown away with the worktree.
- **It never substitutes a different environment and calls it reproduced.** If a recorded package
  cannot be resolved the `env` step fails and names the offending packages
  (`environment.unsatisfied`); training does not run. If the rebuilt interpreter's `major.minor`
  differs from the recorded one, that is a failure too; a patch-level difference is stated in the
  step detail.
- Workspace-local distributions (`examlops`, `examlops-pipelines`, `examlops-serving`, the
  upstream model library) are on no index. They are skipped and named in the step detail — the
  checkout on `PYTHONPATH` supplies them, which is also why a rebuild still uses whatever model
  library is on disk.
- It needs `uv` on `PATH`. Without it the step fails rather than falling back to the caller's
  environment.

### Container image digest

When a bundle recorded an `image_digest` (from `EXAMLOPS_IMAGE_DIGEST`, or from `--image-digest` on
`exa reproduce build`), `--execute` asks the local Docker runtime about it — read-only
(`docker version`, `docker image inspect`); nothing is pulled, built, run or removed. The answer
uses the platform's resolution vocabulary and is reported in `--json` as
`environment.image_digest_status`:

| Status | Meaning | Effect on the `env` step |
|---|---|---|
| `unchecked` | the bundle recorded no digest | none — nothing was claimed |
| `verified` | the recorded image is present locally and its digest matches | passes |
| `mismatch` | a local image answers to the recorded reference but carries a different digest — what would run today is not what was recorded | **fails** (`--allow-env-drift` → `drift_allowed`) |
| `absent` | the runtime answered and the recorded image is not present | **fails** (`--allow-env-drift` → `drift_allowed`) |
| `unverifiable` | no reachable Docker daemon (no binary, daemon down, permission denied, timeout) | does not fail — the check could not run — but it is printed as a warning and carried in `--json`, never reported as a success |

A recorded value of the form `repo[:tag]@sha256:…` is the one that can genuinely mismatch: the
*name* is inspected and the digest it resolves to today is compared with the recorded one. A bare
`sha256:…` can only be present or absent, because inspecting by digest is self-answering.

### Automatic bundles

Set `EXAMLOPS_REPRO_AUTO_BUNDLE=1` and the pipeline builds a bundle itself — at the end of a
successful training run (recording metrics, dataset pin, `EXAMLOPS_SEED` if set — applied to
`random`/NumPy/PyTorch at the start of the flow — and the invocation) and, for a version without one,
on `exa pipeline promote`. A promotion-time bundle captures the *promoting* checkout, not the one
that trained the version (`trigger: promote` says so). Off by default. A failure to build never
fails the run: it increments `examlops.reproducibility.auto.failures()`, is logged and audited
as `repro_auto_bundle_failed`. Inspect with `exa reproduce list` / `exa reproduce show <model> <ver>`.

### Default training path

Without `--train-cmd` the rebuild runs the real `training_flow` (the bundle's `run_spec`) in the
worktree, against a temporary SQLite MLflow store and platform DB so the live registry is never
touched (`EXAMLOPS_REPRO_MLFLOW_URI` overrides). It needs no Prefect/MLflow server. Verified: the
opt-in test `test_live_default_path_end_to_end` (`make reproduce-live`, `EXAMLOPS_REPRO_LIVE=1`, ~30 s) runs bundle →
`--execute --dummy` end to end on this repo; it is not in the default suite because it trains a model.

Limits, stated plainly: a rebuild trains on the mock scheduler unless `--scheduler` (or
`EXAMLOPS_HPC_SCHEDULER`/`EXAMLOPS_SLURM_MODE`) says otherwise — the recorded resources are
exported either way, but only a real scheduler honours them; the default pipeline flow reads
its own dataset backend, not the `--restore-dataset` copy; a model library recorded only by
distribution version (not a git checkout) is rebuilt with the library on disk, and the step
says so; seeds only take effect where the code reads `EXAMLOPS_SEED` (the pipeline flow and
custom trainers); results are compared within tolerance, never bit-exact.

## Verify a bundle hasn't rotted (CI gate)

```bash
exa reproduce verify JPCP 17
# JPCP/17 is reproducible — all inputs present + hashes match.

# If the dataset revision was purged, the commit is unreachable, or uv.lock changed:
exa reproduce verify JPCP 18
# JPCP/18 NON-reproducible: dataset: dataset revision no longer recorded (purged?)
```

`verify` **exits 1** when a bundle is non-reproducible, so it drops straight into a CI job
that fails the build if a promoted model can no longer be reproduced.

`verify` re-checks every recorded input: the platform commit **and the model-library commit**
(looked up in `EXAMLOPS_MODELZOO_DIR` when set, else the recorded path), the dataset revision
(a dataplane snapshot against its manifest, a **lakeFS commit against lakeFS**), the lockfile,
the package set, each **feature view's definition hash**, the **A2 lineage run**, and the
**AI-BOM hash** — a BOM regenerated after the bundle is rot, not a pass.

## Governance (R6)

`technical_evidence(model, version)` shapes a bundle into the structure consumed by **D1**
EU-AI-Act technical documentation and **D2** control evidence, and both call it:

- `exa compliance technical-file <model>` has a **Reproducibility (Annex IV §2)** section
  built from the model's newest bundle, *re-verified* — a bundle whose inputs have rotted is
  shown with its problems and counted as an evidence gap. `repro_bundles` sits outside the
  audit hash chain, so a verified section is still marked *not tamper-evident*; the bundle's
  own signature is what vouches for it.
- `exa governance report` gains control **MEASURE-2.1** (catalogue 1.1.0), satisfied only by a
  bundle that still verifies.

Bundles are looked up by the model id they were built under, then its lower-cased form
(training builds them under the MLflow id, `jpcp`; compliance systems are usually registered
under the registry name, `JPCP`).

## Related

- **A1** dataset revisions — the bundle pins to a revision and `verify` detects a purge.
- **A2** lineage — the manifest records the lineage run id.
- **A3** feature store — feature-view versions are captured.
- **D3** supply-chain security — the manifest is signed with the same HMAC key.
- **D1 / D2** governance — bundles feed technical docs + control evidence.
