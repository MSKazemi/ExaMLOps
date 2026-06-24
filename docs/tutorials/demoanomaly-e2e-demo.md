# End-to-End Demo: `DemoAnomaly`

A single dummy model — **`DemoAnomaly`** — driven through **every** part of
ExaMLOps, so you can prove the whole platform works on a fresh checkout.

**The loop it exercises:**

```
author model → push to ModelZoo (merge request) → AI-production reacts →
retrain (Prefect) → register (MLflow) → serve (Ray) → infer (SeanerBUS) →
drift → make it "stale" again → retrain → diff versions
```

**Why it's safe to run anytime:** `DemoAnomaly` trains on
`SyntheticAnomalyDataset`, which **generates its data in Python** — no Zenodo /
MinIO / dataplane download. So the core demo is fully offline and reproducible.

---

## The demo in two acts

The headline of this demo is the **cross-repo handoff**: a model author merges to
the **ModelZoo** library, and the **AI-production** platform reacts on its own —
detecting the change, retraining, registering, and serving the new version. That
is the story to lead with.

| Act | What it shows | Needs |
|---|---|---|
| **Act 0 — Pre-flight** | The model is wired in and trains offline on a fresh checkout | nothing (offline) |
| **Act 1 — The headline** | A ModelZoo merge drives production: detect → retrain → register → serve → diff | `make stack-up` + control plane |
| **Going deeper** | Live inference over SeanerBUS, drift, serving internals | the bus + monitoring |

> Run Act 0 first as a 30-second sanity check so a live demo never dies on stage,
> then make Act 1 the main event.

---

## Act 0 — Pre-flight (offline, ~30 s, no services)

Run these from the repo root with the venv active (`source .venv/bin/activate`):

```bash
exa pipeline list                                                       # 1. model shows up
exa pipeline validate                                                   # 2. config is valid
pytest tests/unit/test_demoanomaly.py tests/unit/test_registry_integrity.py -q   # 3. tests pass
EXAMLOPS_SLURM_MODE=mock exa pipeline run \
    --model DemoAnomaly --dataset SyntheticAnomalyDataset --dummy        # 4. trains → Accuracy 1.0000
```

If step 4 prints `[evaluate] Accuracy: 1.0000` and the flow finishes, the model
is fully wired. Everything below adds the **live** platform behaviour.

---

## Act 1 — The headline: a ModelZoo merge drives production

> A research-computing operator wants early warning when an HPC job behaves
> abnormally — odd resource profile, runaway power draw, a mis-scheduled job.
> Each job is a **384-dimensional embedding**. `DemoAnomaly` is an unsupervised
> **IsolationForest** that flags anomalous jobs (`is_anomaly = 1`) so they can be
> triaged before wasting node-hours. It is retrained nightly **and** whenever its
> source code changes in the ModelZoo.

### 1.1 Where the model lives (two repos)

The model **belongs in the ModelZoo repo**, not in this AI-production repo. The
authoring flow is:

```
develop model in ModelZoo → push + merge request → merge to ModelZoo main
      → AI-production detects the new commit → retrains locally → serves
```

**A. ModelZoo repo** — `git@gitlab.seanergys.fz-juelich.de:software/modelzoo.git`:

| What | Path (inside the ModelZoo repo) |
|---|---|
| **Model** — IsolationForest wrapper + `MODEL_VERSION` knob | `seanergys_modelzoo/models/tasks/anomaly_detection/demoanomaly/demoanomaly_model.py` |
| **Synthetic dataset** — generates dummy data in-code | `seanergys_modelzoo/datasets/synthetic_anomaly.py` |
| **Dataset + model test** | `tests/unit/test_synthetic_anomaly.py` |

**B. AI-production repo** (this repo) — consumes the ModelZoo model:

| What | Path |
|---|---|
| **Config shim** — model-bound transforms + inference contract | `pipelines/model_configs/demoanomaly_config.py` |
| **YAML registry** — single source of truth (datasets, lifecycle, serving, UUID) | `pipelines/models/demoanomaly.yaml` |
| **SeanerBUS request generator** | `platform/clients/seanerbus_demoanomaly_req.py` |
| **Pipeline glue** — register dataset, pass `split` through, log `MODEL_VERSION` | `pipelines/pipeline_generator.py` |
| **Pipeline-side test** | `tests/unit/test_demoanomaly.py` |
| **This walkthrough** | `docs/tutorials/demoanomaly-e2e-demo.md` |

> **What "consume" means — important and easy to get wrong.** The ModelZoo ships
> **source code**, not a trained artifact. AI-production does **not** download a
> trained model from the ModelZoo. It detects the new ModelZoo commit, **retrains
> the model itself** on synthetic data, and the resulting artifact is stored in
> **MLflow / MinIO**. So the correct phrasing is *"AI-production detects the new
> ModelZoo commit and retrains"* — never *"downloads the model from the pool."*

> The model + dataset also exist under this repo's `modelzoo/` directory as a
> **vendored snapshot** for local execution/testing; the source of truth is the
> ModelZoo repo above. **This walkthrough assumes the snapshot is already in sync**
> (i.e. the `exa`/CI step that refreshes it from upstream has already run), so
> `DemoAnomaly` is present locally.

### 1.2 Author the model, then push + merge request

Develop the model in the ModelZoo repo and bump its staleness knob so the
downstream platform has a reason to react:

```python
# (ModelZoo) seanergys_modelzoo/models/tasks/anomaly_detection/demoanomaly/demoanomaly_model.py
MODEL_VERSION = "v2"     # was "v1" — the human-readable marker logged to MLflow
```

Then push and open a merge request, and merge to `main`:

```bash
git -C /home/mohsen/scratch/seanergys/modelzoo add -A
git -C /home/mohsen/scratch/seanergys/modelzoo commit -m "DemoAnomaly v2"
git -C /home/mohsen/scratch/seanergys/modelzoo push                  # → open MR → merge to main
```

### 1.3 AI-production reacts — three ways to pick up the merge

A merge to ModelZoo `main` is detected by the **control plane**, which marks every
deployed model **stale** (tracked in its `model_freshness` table). There are three
equivalent ways to make that detection happen — pick one for the demo:

```bash
# (a) Trigger it NOW — one manual poll cycle (the "do it on demand" path):
exa modelzoo sync                # → New commit <sha> detected — N model(s) marked stale

# (b) Or do nothing and WAIT ~5 minutes — the control plane polls on its own.
#     MODELZOO_POLL_SECONDS defaults to 300 s (5 min). No command needed.

# (c) Or simulate the push webhook (no remote needed; uses MODELZOO_WEBHOOK_SECRET):
SECRET="$MODELZOO_WEBHOOK_SECRET"
BODY='{"ref":"refs/heads/main","head_commit":{"id":"demo-v2-abc123"},"pusher":{"name":"demo"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')"
curl -sS -X POST http://localhost:18002/webhooks/modelzoo/github \
  -H "X-Hub-Signature-256: $SIG" -H 'Content-Type: application/json' -d "$BODY"
# → {"event_id": …, "models_marked_stale": N, …}
```

### 1.4 Stale → retrain → register → serve → diff

```bash
exa modelzoo status                                                # DemoAnomaly → STALE (stale_since …)
exa retrain DemoAnomaly --dataset SyntheticAnomalyDataset --dummy   # registers v2, clears staleness
exa modelzoo status                                                # DemoAnomaly → current
exa serve reload                                                   # Ray Serve hot-loads the new Production alias
exa models diff demoanomaly 1 2                                    # compare v1 vs v2 (incl. model_version)
```

### 1.5 Hands-free: let the 5-minute poll do everything

For the most impressive cut — exactly the "merge, then wait five minutes" flow —
turn on auto-retrain once, then the poller does detect → retrain → register with
**zero manual steps**:

```bash
exa modelzoo config-set auto_retrain true     # poller retrains stale models automatically
# …now merge to ModelZoo and wait ≤5 min: the new version trains, registers, and serves itself.
exa modelzoo config                           # confirm: auto_retrain=true, poll_interval_seconds=300
```

> `exa modelzoo events` shows the detected commits and what each one triggered.

---

## How a request flows

**Inference path** (a job arrives on the bus):

```
SeanerBUS ──HpcJobV1──▶ seanerbus-bridge ──POST /infer-pipeline/infer──▶ InferencePipelineIngress
                                                                              │
                                                       FeatureTransformer  (checks 384 dims)
                                                                              │
                                                       ModelRouter  (alias → MLflow version)
                                                                              │
                                              MultiModelServer ─▶ IsolationForestClassifier.predict
                                                                              │
                                              HpcInferenceResV1 (is_anomaly) ──▶ back to the bus
```

**Training path** (`exa pipeline run` / `exa retrain`):

```
training_flow (Prefect):
  data_extraction → slurm_submit (mock) → slurm_wait → result_fetch
      → evaluate (accuracy) → MLflow log + register (tags model_version) → promote (lifecycle gates)
```

---

## Prerequisites

| For… | You need |
|---|---|
| Act 0 (offline) | repo bootstrapped: `uv pip install -e ".[dev]"` (root) + `poetry install --with ci` (in `modelzoo/`) |
| Act 1 (headline) | `make stack-up` (MLflow, Ray, MinIO, Prefect) + `make control-plane-up` |
| SeanerBUS loop | the `../seanerbus` repo running + `make seanerbus-up` |

---

## Deploy + serve (needs `make stack-up`)

**Deploy the nightly schedule (Prefect):**

```bash
# Optional — only needed if your Prefect server is NOT on the default host:
# export PREFECT_API_URL=http://localhost:14200/api
exa pipeline deploy                                  # serves examlops-demoanomaly-nightly
```

> ✅ `pipelines/deploy.py` applies the resolved `PREFECT_API_URL`
> (default `http://localhost:14200/api`) into the environment before `.serve()`,
> so the SDK always targets the configured server. Previously an unset variable
> made `.serve()` silently fall back to a throwaway **ephemeral** server
> (`Cannot schedule flows on an ephemeral server…`) and the deployment never
> landed on `:14200`. You only need to export the variable to point at a
> non-default host.

**Load it into Ray Serve and smoke-test inference:**

```bash
exa serve reload         # hot-load the Production alias into Ray Serve
exa serve check          # health + model list + one prediction per model
exa serve infer-check    # POST a synthetic job to /infer-pipeline/infer
```

---

## Drive live inference over SeanerBUS (needs the bridge)

```bash
# 20 requests at 5/s, ~30% of them anomalous embeddings:
python platform/clients/seanerbus_demoanomaly_req.py --rate 5 --count 20 --anomaly-frac 0.3
```

Each line prints the job, whether the model flagged it, the serving version, and
a final tally (`sent / injected_anomalies / flagged_by_model`). Flags:
`--rate` (req/s), `--count`, `--anomaly-frac`, `--alias`, `--uuid`, `--seed`.

**Then inspect drift:**

```bash
exa drift status                 # prediction drift across models
exa drift baseline DemoAnomaly   # snapshot current prediction stats as a baseline
exa drift input status           # embedding-distribution drift (norm / mean / std)
```

---

## How staleness actually works (mechanics behind Act 1)

The control plane marks deployed models stale on **any ModelZoo commit** (tracked
in its `model_freshness` table); a retrain clears it. `MODEL_VERSION` is the
human-readable marker that rides along — it is logged to MLflow on every training
run so you can tell versions apart (`exa models diff` reads it). So bumping the
number *and committing* is what actually triggers staleness; the retrain is what
produces and registers the new version.

| Knob | Effect |
|---|---|
| `MODELZOO_POLL_SECONDS` (default `300`) | how often the control plane auto-polls ModelZoo for new commits |
| `exa modelzoo sync` | force one poll cycle immediately (the manual trigger) |
| `exa modelzoo config-set auto_retrain true` | poller retrains stale models automatically (hands-free) |
| webhook `POST /webhooks/modelzoo/github` | push-driven detection without polling |

---

## Reset the demo — return to a clean slate

The demo is meant to be **repeatable**: run it (Act 0 + Act 1), then tear the
runtime traces back down, then re-run. The pattern is **test it → remove it →
test it**. The rule for the teardown:

> **Keep the model _source_ in `modelzoo/`. Remove every _runtime_ trace** —
> the Prefect pipeline deployment, the MLflow registered versions, the Ray Serve
> deployment, and the drift / platform-DB rows. Nothing trained or deployed
> survives; only the code in the ModelZoo does.

### Level 1 — Runtime reset (recommended; keeps the demo re-runnable)

Removes everything that got trained/deployed, but leaves the source **and** the
AI-production config (YAML + shim) in place, so you can re-run Act 1 instantly.

```bash
# 1. Prefect — delete the nightly deployment (flow stays, deployment goes):
prefect deployment delete "training_flow/examlops-demoanomaly-nightly"

# 2. MLflow — delete the registered model + all its versions (note: lowercase name):
python -c "from mlflow import MlflowClient; MlflowClient().delete_registered_model('demoanomaly')"

# 3. Ray Serve — reload so the router drops the now-absent model:
exa serve reload                       # DemoAnomaly disappears from the hot set
exa serve check                        # confirm it is no longer listed

# 4. Drift + platform DB — clear snapshots, baselines input-drift, and auto-retrain:
exa drift reset DemoAnomaly            # clears prediction-drift snapshots
exa drift input reset DemoAnomaly      # clears input-embedding drift snapshots
exa drift auto-retrain disable DemoAnomaly   # remove any auto-retrain config
exa pipeline promote-delete DemoAnomaly      # remove saved promotion rules (if any)
```

### Level 2 — Full de-integration (leave ONLY the ModelZoo source)

Do Level 1 first, then also remove the AI-production *consumption* so `DemoAnomaly`
exists **only** as source in the ModelZoo. After this, `exa pipeline list` no
longer shows it.

```bash
# Remove the AI-production glue (this repo). The ModelZoo source is NOT touched.
rm pipelines/models/demoanomaly.yaml                 # YAML registry entry
rm pipelines/model_configs/demoanomaly_config.py     # config shim
rm platform/clients/seanerbus_demoanomaly_req.py     # bus request generator
# Then revert the DemoAnomaly-specific lines in pipelines/pipeline_generator.py
# (dataset registration + split pass-through) and remove tests/unit/test_demoanomaly.py.
```

> **Kept on purpose:** `modelzoo/seanergys_modelzoo/models/tasks/anomaly_detection/demoanomaly/`
> and `modelzoo/seanergys_modelzoo/datasets/synthetic_anomaly.py` (+ their test).
> That is the "model lives in the ModelZoo" end-state you want for the tutorial.

### Verify the clean slate

```bash
exa pipeline list          # Level 1: still listed   |  Level 2: DemoAnomaly gone
exa serve check            # DemoAnomaly NOT in the served model list
exa modelzoo status        # no stale/served DemoAnomaly row
# ModelZoo source is intact and still tests green:
cd modelzoo && python -m pytest tests/unit/test_synthetic_anomaly.py -q && cd ..
```

> Steps 1–3 of Level 1 require the live stack (`make stack-up` + control plane);
> with services down they will fail to connect — that is expected. The drift/DB
> steps (4) and the ModelZoo source test run offline.

---

## What CI / tests cover

| Test | Checks |
|---|---|
| `tests/unit/test_registry_integrity.py` | YAML fields + inference contract + unique `model_id` (catches half-applied scaffolding) |
| `tests/unit/test_demoanomaly.py` | model registers, config loads, inference params complete, instantiates |
| `modelzoo/tests/unit/test_synthetic_anomaly.py` | dataset shapes/labels/determinism/splits + IsolationForest accuracy ≥ 0.9 |
| modelzoo smoke (`test_models_smoke.py`) | auto-discovers `DemoAnomaly` + `SyntheticAnomalyDataset`, instantiates offline |

GitLab CI runs these under the `test:modelzoo` and `test:examlops` jobs.

## Verification matrix (offline — already confirmed)

| Check | Command | Expected |
|---|---|---|
| Registers | `exa pipeline list` | `DemoAnomaly … datasets=['SyntheticAnomalyDataset']` |
| Contract | `exa pipeline validate` | registry-integrity passes |
| Unit (pipeline) | `pytest tests/unit/test_demoanomaly.py` | 4 passed |
| Unit (modelzoo) | `poetry run pytest tests/unit/test_synthetic_anomaly.py` | 8 passed |
| Smoke + unit (modelzoo) | `python -m pytest tests/smoke/ tests/unit/` | 42 passed, 4 skipped |
| Train (mock) | `EXAMLOPS_SLURM_MODE=mock exa pipeline run --model DemoAnomaly --dataset SyntheticAnomalyDataset --dummy` | `Accuracy: 1.0000`, flow Completed |

> **MLflow registration — keep the venv in sync with `uv.lock`.** MLflow infers
> pip requirements via `uv export` (which reads `uv.lock`) *and* from the model's
> installed libs. If those disagree — e.g. `uv.lock` pins `scikit-learn==1.8.0`
> but the venv drifted to `1.9.0` — MLflow aborts `register_model` with
> *"requirements versions are incompatible"* and the version is never registered.
> Fix: align the venv to the lock (`uv pip install "scikit-learn==1.8.0"`, or
> `uv sync --frozen`). Verified working: with the venv aligned, RUN-1 registered
> `demoanomaly` **v1** and a `MODEL_VERSION=v2` bump + re-run registered **v2**,
> each promoted Staging→Canary→Production and tagged `model_version` accordingly.
> (`exa models diff` itself talks to MLflow over HTTP, so it needs the live MLflow
> server, not a local sqlite store.)
