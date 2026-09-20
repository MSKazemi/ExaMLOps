# Reproducibility Bundles (A8)

> Next-Gen 40 · feature **A8** · ADR 0038 · spec `design/vision/specs/A8-reproducibility-bundles.md`

A reproducibility **bundle** is a *signed manifest* that captures **every input** to a
model version, so that months later you can answer two questions with evidence:

1. **Can I rebuild this?** — `exa reproduce run` documents the exact rebuild plan and, given
   re-observed metrics, checks they match the recorded ones within a documented tolerance.
2. **Is this still reproducible?** — `exa reproduce verify` checks that every referenced
   input still exists and its hash still matches, flagging a bundle that has *rotted*.

## What the manifest captures (R1)

| Input | Captured as |
|---|---|
| Code | `git rev-parse HEAD` commit SHA |
| Dataset | A1 dataset revision id (`<name>@<rev>`) |
| Features | A3 feature-view versions |
| Environment | `uv.lock` SHA-256, the installed package set (`name==version`), Python version, container image digest |
| Code state | `code_dirty` — whether tracked files differed from the commit (`null` = unknown) |
| Dataset source | for a dataplane snapshot, the source key (`dataset_source`) |
| Hyperparameters | recorded key/values |
| Compute | scheduler resources + hardware (phase 23) |
| Determinism | RNG seeds |
| Provenance | A2 lineage run id |

The manifest is hashed canonically and **signed** with the D3 HMAC key; the build is
audited (D4) and the manifest is versioned.

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
| 1 | `code` | detached `git worktree` at the bundle's commit (`--repo`, default `.`) | no commit recorded, commit not in the repo (never falls back to `HEAD`) |
| 2 | `dataset` | pinned revision must be recorded; with `--data-path` the local data is hashed against it | revision unrecorded, or data hash differs. Without `--data-path` (or with `--dummy`) content is **not** verified and the step says `skipped` |
| 3 | `env` | recorded `uv.lock`/`requirements.txt` sha256 vs the file in the checkout, plus the recorded package set vs this interpreter | hash or a package differs, or no lock hash was captured; `--allow-env-drift` continues and reports `drift_allowed`. A container image digest is shown but cannot be verified here |
| 4 | `train` | the pipeline training flow runs in a subprocess **inside the worktree**, pinned to the dataset revision and recorded seed (`EXAMLOPS_SEED`), mock scheduler unless set | non-zero exit, timeout, or no `EXAMLOPS_REPRO_METRICS=<json>` line |
| 5 | `compare` | produced vs recorded metrics, relative tolerance `--rtol` (default: the bundle's, else 0.05) | any recorded metric missing, non-finite or out of tolerance; a bundle with no recorded metrics |

```bash
exa reproduce run JPCP 17 --execute --dummy --rtol 0.05
exa reproduce run JPCP 17 --execute --data-path ./data/PM100 --json
# custom trainer (must print EXAMLOPS_REPRO_METRICS={"rmse": 4.9}); runs in the checkout:
exa reproduce run JPCP 17 --execute --train-cmd "python train.py"
```

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

Limits, stated plainly: lakeFS refs are not restored or verified (only the recorded revision is
checked); the container image digest is recorded, never verified; the upstream modelzoo version is
not captured (a rebuild uses the modelzoo on disk); scheduler resources are not re-requested; a
rebuild trains on the mock scheduler unless `EXAMLOPS_SLURM_MODE` is set; seeds only take effect
where the code reads `EXAMLOPS_SEED` (the pipeline flow and custom trainers); results are compared
within tolerance, never bit-exact.

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

## Governance (R6)

`technical_evidence(model, version)` shapes a bundle into the structure consumed by **D1**
EU-AI-Act technical documentation and **D2** control evidence — a promoted model carries a
signed, verifiable record of exactly how it was produced.

## Related

- **A1** dataset revisions — the bundle pins to a revision and `verify` detects a purge.
- **A2** lineage — the manifest records the lineage run id.
- **A3** feature store — feature-view versions are captured.
- **D3** supply-chain security — the manifest is signed with the same HMAC key.
- **D1 / D2** governance — bundles feed technical docs + control evidence.
