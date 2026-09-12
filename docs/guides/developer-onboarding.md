---
description: "Developer onboarding for ExaMLOps: the journey of a model, what every directory is for, the platform ⟂ use-case rule, and how to start changing code without breaking anything."
---

# Developer Onboarding — How ExaMLOps Works and How to Work On It

**Who this is for:** a colleague who has never opened this repository and wants to
understand what is here, how the pieces fit together, and how to start changing code
without breaking anything.

**Plain-language promise:** no jargon that is not explained on the spot.

---

## 1. What this project is, in one paragraph

ExaMLOps is a **platform that takes care of a machine-learning model's whole life on a
supercomputer**. You register a model once. From then on, the platform trains it on the
HPC cluster, keeps every version, refuses to put anything into production until a human
approves it, serves it for real inference requests, and watches it afterwards for cost,
carbon, drift and quality. Everything can be driven from a command line (`exa`), from a
web dashboard, or by talking to an agent in plain English.

It is in production at **LuxProvide (MeluXina)** for the EuroHPC **SEANERGYS** project.

---

## 2. The journey of a model — the mental model to keep

This is the single most useful thing to understand. Everything in the repo serves one of
these six steps.

| # | Step | What happens | Who does it |
|---|---|---|---|
| 1 | **Register** | You describe your model in one YAML file | a developer |
| 2 | **Train** | A pipeline runs training as a job on the HPC cluster | the platform (Prefect + Slurm/Flux) |
| 3 | **Version** | Every trained model is stored with its metrics and its data | the platform (MLflow) |
| 4 | **Approve** | Nothing goes live until a sysadmin says yes | a human |
| 5 | **Serve** | The approved version answers inference requests | the platform (Ray Serve) |
| 6 | **Watch** | Drift, cost, carbon, quality, audit trail | the platform, continuously |

If you ever get lost in the code, ask: *"which of these six steps is this file about?"*

---

## 3. The map — what is in each directory

The repository has **four main code areas** plus content and docs.

| Directory | What lives there | Think of it as |
|---|---|---|
| `platform/` | The product itself: the `exa` CLI, the dashboard, the control-plane API, the Skipper agent, infrastructure glue | **the platform** |
| `pipelines/` | The training engine — how a training run is built and submitted | **the training engine** |
| `serving/` | Ray Serve model server and the inference pipeline | **the serving engine** |
| `usecases/` | The actual models and datasets of a given deployment (e.g. `usecases/seanergy`) | **the content** |
| `modelzoo/` | The upstream model library — **read-only here, never edit it** | **someone else's library** |
| `tests/` | ~312 unit test files, plus integration tests | **the safety net** |
| `docs/` | Everything a user or operator reads (89 guides + tutorials + reference) | **the manual** |

Inside `platform/`:

| Path | Contents |
|---|---|
| `platform/cli/src/examlops/` | The `examlops` Python package — this is where most platform logic lives |
| `platform/services/dashboard/` | FastAPI backend + React frontend |
| `platform/services/control_plane/` | The API other systems call to trigger retraining, and the home of the approval gate — see §5 |
| `platform/services/agent/skipper/` | Skipper, the management agent |
| `platform/infra/`, `platform/ci/` | Docker/Helm/scheduler glue and CI guard scripts |

### The one rule about this layout

> **The platform never mentions a concrete model or dataset.**
> `platform/` and `pipelines/` do not know that "JPCP" exists. They load whatever
> use-case pack they are pointed at (`EXAMLOPS_USECASE_DIR`, default `usecases/seanergy`).
> This is what lets the same platform serve a completely different project tomorrow.
> A CI check fails the build if someone breaks this rule.

---

## 4. Three ways to use the platform

All three do the same things — pick whichever suits you.

1. **The CLI — `exa`.** The main interface. **401 commands**, grouped into 12 panels that
   follow the model journey above.
   ```bash
   exa --help          # see the 12 panels
   exa status          # is everything healthy?
   exa explain drift   # plain-English explanation of any command
   exa doctor          # diagnose my own setup
   ```
2. **The dashboard** — a web UI at `http://localhost:18099` for people who prefer clicking.
3. **Skipper, the agent** — ask in plain English: `exa ask "which models are drifting?"`

---

## 5. The control plane — the platform's front door

### What it is, in one sentence

The control plane is a **small web API that other systems knock on when they want the
platform to do something** — above all: *"please retrain this model"*.

### Why it exists

Training is complicated. It involves Prefect, the HPC scheduler, MLflow, credentials and a
registry of what may be trained on what. You do **not** want every client — a CI pipeline, a
partner's application, a script, the dashboard — to know all that or to hold those
credentials.

So the platform puts one guarded door in front of it. A client sends a plain HTTP request:

```
POST /retrain   {"model_name": "JPCP", "dataset_name": "PM100Dataset"}
```

and the control plane does the rest: checks the caller is allowed, checks the model and
dataset actually exist, and schedules the Prefect training run. The caller never touches
Prefect.

### The three jobs it does

1. **Take retraining requests.** `POST /retrain` — validate, then schedule. It de-duplicates
   repeated requests, supports an idempotency key so a retry does not train twice, and has a
   circuit breaker so a broken Prefect does not get hammered.
2. **Hold the approval gate.** This is step 4 of the model journey (§2), and this service is
   where it physically lives:
   - CI reports a change → `POST /api/changes` records it as **pending** (nothing trains yet)
   - a human lists them → `GET /approvals`
   - a human decides → `POST /approve/{model}` (training fires immediately) or
     `POST /reject/{model}` (with a reason)
3. **Watch the ModelZoo.** GitLab/GitHub push webhooks mark models as **stale** so everyone
   can see which models are behind the upstream library.

It also answers `/health`, `/status` and `/metrics` (Prometheus), so monitoring can tell
whether the platform is alive.

### Where it lives

| | |
|---|---|
| Code | `platform/services/control_plane/app.py` (FastAPI) |
| Port | `http://localhost:18002` |
| Start it | `make control-plane-up` · logs: `make control-plane-logs` |
| Its own tests | `platform/services/control_plane/tests/` (`make ci-control-plane`) |
| API contract | `api-contract.json` — committed, regenerate with `make openapi-export` |

### The endpoints you will actually use

| Method | Path | Needs auth | Purpose |
|---|---|---|---|
| GET | `/health` | no | Is it alive, and are its dependencies alive |
| GET | `/models` | no | Which model → dataset combinations exist |
| POST | `/retrain` | **write** | Schedule a training run |
| GET | `/approvals` | read | List pending / approved / rejected changes |
| POST | `/approve/{model}` | **write** | Approve, and fire training now |
| POST | `/reject/{model}` | **write** | Reject, with an optional reason |
| GET | `/metrics` | no | Prometheus metrics for the approval gate |

Full list and request/response shapes: `docs/guides/control-plane.md`.

### Security — read this before you deploy anything

Every **write** endpoint requires a bearer token, supplied as `CONTROL_PLANE_TOKEN`.

- If the token is **not set**, the write endpoints refuse to work. That is deliberate.
- If the token is a **known placeholder** (`changeme` and friends), the service **fails at
  startup** rather than pretending to be secure.
- Reads are scoped to the caller's tenant — one client cannot list another's approvals.

### You are already using it

The CLI is just a friendly wrapper around this API — `exa retrain` calls `POST /retrain`
and `exa approvals approve` calls `POST /approve/{model}`. So:

```bash
make control-plane-up
curl http://localhost:18002/health          # the raw API
exa retrain JPCP --dataset PM100Dataset --dummy   # the same thing, via the CLI
exa approvals list
```

If `exa` ever reports *"Is the control plane running?"*, this is the service it means.

---

## 6. The training pipeline — the one flow that trains everything

### There is only one flow

Step 2 of the model journey (§2) is a **single generic Prefect flow**, not one pipeline per
model:

| | |
|---|---|
| Code | `pipelines/pipeline_generator.py` → `training_flow` |
| Signature | `training_flow(model_name, dataset_cls_name, is_dummy=False, backend_name=None)` |
| Deployments | `pipelines/deploy.py` — builds one Prefect deployment per enabled model |

The flow does not know that "JPCP" exists (§3). It receives the model name, looks it up in
the registry that auto-discovery plus the use-case pack's YAML built, and runs. **Adding a
model therefore never means writing a pipeline.**

### What one run actually does — eight steps

| # | Prefect task | What it does |
|---|---|---|
| 1 | `data_extraction_task` | build the model object and the training dataloader from the pack YAML |
| 2 | `data_contract_gate` | validate the dataset against its data contract — fails closed |
| 3 | `slurm_submit_task` | submit the training job to the cluster (or run it inline — see below) |
| 4 | `slurm_wait_task` | poll until the job reaches a terminal state |
| 5 | `result_fetch_task` | bring the trained estimator back and rebuild the model object |
| 6 | `evaluate_task` | score it on the validation split (RMSE / MAPE / MSE) |
| 7 | `log_mlflow_task` | register the version in MLflow with metrics, params, dataset revision, HPC job id |
| 8 | `promote_task` | move the alias (`Staging` / `Canary` / `Production`) if the metric clears the model's threshold |

**Inline or on the cluster.** By default `EXAMLOPS_SLURM_MODE=mock`, which trains inline in
the same process — no scheduler needed, and that is what every `--dummy` run does. Set
`EXAMLOPS_HPC_SCHEDULER=slurm` or `=flux` to submit a real batch job instead.

### Where the schedule is configured — not in Python

Each model's YAML in `usecases/<pack>/models/<name>.yaml` carries a `prefect:` block, and
that is the only place scheduling is decided:

```yaml
prefect:
  schedule: "0 2 * * *"                 # nightly at 02:00 UTC; omit for manual-only
  deployment_name: examlops-jpcp-nightly
  work_pool: default-agent
  concurrency_limit: 1
```

`pipelines/deploy.py` reads those blocks and registers one deployment per enabled model,
named `examlops_training_<MODEL>`, tagged with the model's Project.

### Four ways to trigger a run

**1. Locally — the everyday one.** No Prefect server required in mock mode:

```bash
exa pipeline run --model JPCP --dataset PM100Dataset --dummy      # fastest, no downloads
exa pipeline run --model JPCP --dataset PM100Dataset --backend minio
exa pipeline run --cluster auto --gpus 1                          # let placement pick a cluster
exa pipeline run --dummy                                          # every model × dataset
```

`exa pipeline run` is a thin wrapper around the generator script, so the raw form works
identically and is useful when debugging:

```bash
python pipelines/pipeline_generator.py --list
python pipelines/pipeline_generator.py --model JPCP --dataset PM100Dataset --dummy
```

**2. On a schedule — register deployments with the Prefect server.**

```bash
exa pipeline deploy                 # cron schedules taken from each model's YAML
exa pipeline deploy --no-schedule   # register for manual triggering only
```

> **Gotcha:** this command **blocks**. Under the hood it calls Prefect's `serve()`, which
> keeps running to serve the deployments it just registered. Run it where it can stay
> running (a container or a service), not in a terminal you are about to close.

Then trigger a registered deployment by hand, from the Prefect UI
(http://localhost:14200 → Deployments) or from the command line:

```bash
prefect deployment run "examlops_training_JPCP/examlops-jpcp-nightly"
```

**3. Through the control plane — how other systems do it.** This is the production path
described in §5: `POST /retrain` resolves the deployment through the Prefect API and creates
a flow run, with an idempotency key so a retry never trains twice.

```bash
exa retrain JPCP --dataset PM100Dataset          # the CLI wrapper around POST /retrain
```

The same door is used by CI, the dashboard, approval (`exa approvals approve JPCP` fires
training immediately) and partner systems.

**4. Automatically — the platform triggering itself.**

```bash
exa drift trigger --dry-run     # preview retrains for models that have drifted
exa drift trigger               # fire them (cooldown-aware)
exa autopilot run --dry-run     # the whole closed loop: detect → retrain → validate → promote
```

Both funnel into path 3, so everything still passes the same guarded door and lands in the
audit log.

### Watching a run

| Where | What you see |
|---|---|
| The terminal | every task prints as it goes — the fastest feedback |
| Prefect UI — http://localhost:14200 | the run graph, task states, retries, logs |
| MLflow — http://localhost:15000 | the registered version, its metrics and artifacts |
| `exa models list` / `exa audit --last 1d` | what got registered and who triggered it |

---

## 7. Get it running — first 20 minutes

You need: **Python 3.12+**, **Docker**, **git**, and (for the dashboard UI) **Node.js**.

```bash
git clone <repo-url> && cd examlops

make install-dev        # create .venv and install everything (uses uv)
source .venv/bin/activate

make stack-up           # start Postgres, MLflow, Prefect, Ray, MinIO, dashboard...
exa status              # should now show services as healthy
```

Then open:

| Service | URL |
|---|---|
| Dashboard | http://localhost:18099 |
| MLflow (model versions) | http://localhost:15000 |
| Prefect (training runs) | http://localhost:14200 |
| Ray Serve (inference API) | http://localhost:18001 |
| Grafana (metrics) | http://localhost:13000 |

> All host ports use a **+10000 offset** (MLflow's normal 5000 becomes 15000) so the stack
> never collides with something else on your machine.

Run your first training end-to-end without any real data:

```bash
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
```

Useful housekeeping:

```bash
make stack-logs     # watch what the containers are doing
make stack-down     # stop containers, keep the data
make help           # every available target, grouped
```

---

## 8. The everyday development loop

This is the cycle for **any** change, big or small.

```
1. branch  →  2. change  →  3. test fast  →  4. gate  →  5. update docs  →  6. commit  →  7. push
```

| Step | Command | Why |
|---|---|---|
| 1. Branch | `git switch -c fix/short-description` | never work directly on `main` |
| 2. Change | edit code **and its test in the same pass** | a change without a test is not finished |
| 3. Test fast | `make test-fast` | the whole unit suite (**3388 tests**) in ~70 seconds, in parallel |
| 4. Gate | `make gate` | ~2 min: lint · format · type-check · unit tests · docs build |
| 5. Docs | update the guide / CHANGELOG that your change affects | see §10 |
| 6. Commit | `git commit` with a scoped message | see §11 |
| 7. Push | `git push` | CI runs the same checks again |

Other test commands you will want:

```bash
make test                  # everything, including integration tests
make test-failed           # re-run only what failed last time
.venv/bin/pytest tests/unit/test_pipeline.py -v    # one file
make dashboard-check       # dashboard backend + frontend tests
make preflight             # the full CI mirror — run before an important push
```

> **Why tests run in parallel:** every test gets its own private database, so tests never
> interfere with each other. If a test only passes when run alone, that is a bug **in the
> test**, not in the parallel runner.

---

## 9. The four most common tasks

### A. Add a new model

```bash
exa scaffold MyModel --task anomaly_detection --type classification
```

This creates the model class, a config file, a unit test, and a YAML file. Then you edit
them. The YAML file in `usecases/<pack>/models/<name>.yaml` is the **single source of
truth** for that model's configuration — nothing else. A CI guard fails the build if the
scaffolding is only half-finished.

Full walkthrough: `docs/guides/add-a-new-model.md`.

### B. Change the CLI

Code lives in `platform/cli/src/examlops/cli/commands/`. After adding a command:

```bash
make docs-cli       # regenerate the command reference from the live CLI
exa <your-command> --help    # check it reads well
```

### C. Change the dashboard

```bash
# backend
cd platform/services/dashboard/backend && uvicorn main:app --reload --port 8099
# frontend (separate terminal)
cd platform/services/dashboard/frontend && npm install && npm run dev
```
Then `make dashboard-check` before committing.

### D. Change documentation

```bash
make docs-serve     # live preview at http://localhost:8080
make docs-build     # strict build — a broken link fails the build
```

---

## 10. Non-negotiables — the rules that keep this project healthy

1. **Never claim something works without running the command and reading its output.**
   No "should be fine".
2. **A behaviour change ships with its test.** Same commit.
3. **Documentation is part of the change**, not a follow-up. If you changed a command, its
   help text, its guide and the CHANGELOG all change too.
4. **`modelzoo/` is read-only.** It is an upstream library; changes go upstream.
5. **The approval gate is real.** Nothing reaches production without a sysadmin approving
   it (`exa approvals list` / `approve` / `reject`).
6. **No secrets in the repo.** Real values live only in ignored `.env` files.
7. **The platform ⟂ use-case boundary** (§3) is enforced by CI — do not import a concrete
   model into platform code.

---

## 11. Commits and pull requests

Use **scoped Conventional Commits**:

```
fix(serving): handle an empty registry
feat(cli): add exa drift trigger --dry-run
docs(guides): explain the approval gate
test(guards): cover stale paths
```

A pull request should state: the problem, the solution, which services are affected, the
linked issue or design record, and **the exact commands you ran to verify it**. Add
screenshots for UI changes, and say clearly if a migration or redeploy is needed.

---

## 12. Where to look things up

| Question | Where |
|---|---|
| How do I start? | `docs/guides/quickstart.md` |
| How does it all fit together? | `docs/guides/architecture.md` |
| What does command X do? | `exa explain X`, or `docs/reference/cli-commands-guide.md` |
| What does dashboard button Y do? | `docs/dashboard/usage-guide.md` |
| How do I add a model? | `docs/guides/add-a-new-model.md` |
| How does training actually get triggered? | §6 of this guide, then `docs/guides/hpc-training-workflow.md` |
| How does testing work? | `docs/guides/testing.md` |
| What changed recently? | `CHANGELOG.md` |
| What targets exist? | `make help` |
| Why was it built this way? | the ADRs (architecture decision records) in the internal repository — 114 of them, each one decision |

---

## 13. Small glossary, in plain words

| Term | Plain meaning |
|---|---|
| **MLflow** | The filing cabinet for trained models — every version, its metrics, its files |
| **Prefect** | The thing that runs training steps in the right order and retries them |
| **Ray Serve** | The web server that keeps models loaded and answers inference requests |
| **Slurm / Flux** | The supercomputer's job queue — you submit a job, it runs when resources free up |
| **MinIO** | Local storage that behaves like Amazon S3 — holds datasets and model files |
| **Control plane** | A small guarded API that lets other systems ask for a retraining, and where approvals are held (§5) |
| **Drift** | The world changed, so the model's answers are quietly getting worse |
| **Approval gate** | A human must say yes before a model version becomes "production" |
| **Registry** | The list of models the platform knows about |
| **Alias / stage** | A label like `Production` or `Staging` pointing at one model version |
| **Audit log** | A tamper-evident record of who did what, when |
| **FinOps / Green-AI** | Tracking what the compute cost, in money and in carbon |
| **Use-case pack** | A folder of models + datasets for one project, plugged into the platform |
| **ADR** | A short document recording one architecture decision and why |
| **Skipper** | The chat agent that can operate the platform for you |

---

## 14. A five-minute demo you can give to anyone

```bash
exa status                                        # 1. the platform is alive
exa pipeline run --model JPCP --dataset PM100Dataset --dummy   # 2. train something
exa models list                                   # 3. it is versioned
exa approvals list                                # 4. it is waiting for a human
exa approvals approve JPCP                        # 5. a human says yes
exa predict ...                                   # 6. it now answers requests
exa drift status                                  # 7. and we are watching it
```

That sequence *is* the product.

---

*Version at time of writing: v0.59.1.*
