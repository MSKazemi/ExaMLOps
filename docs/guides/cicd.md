# CI/CD — GitLab Pipeline

ExaMLOps uses GitLab as its primary CI/CD host. The pipeline runs on every push and merge request, covers all four source areas (modelzoo, infra, examlops, integration), and deploys automatically to `lxp-cpu01` after a successful run on `main`.

The implementation lives in `.gitlab-ci.yml` at the repo root. GitHub Actions workflows (`.github/workflows/`) remain in the repo as a reference but are no longer the source of truth.

---

## Pipeline overview

Four stages arranged as a DAG. The six test jobs run in parallel; deploy and post-deploy fire only on `main` after all tests pass.

```
sanity:python-syntax  ─┐
sanity:check-structure ─┼─► test:modelzoo          ─┐
                        │   test:infra:compose      │
                        │   test:infra:slurm-lint   ├─► deploy:lxp ──► post-deploy:lxp:notify-model-changes
                        │   test:infra:alert-rules  │                 └► post-deploy:lxp:retrain-push-models
                        └─► test:examlops           │
                            test:integration        ─┘
```

| Stage | Runs on | Purpose |
|---|---|---|
| `sanity` | all branches + MRs | Syntax check + directory structure guard — blocks everything on failure |
| `test` | all branches + MRs | Six parallel jobs covering all test types |
| `deploy` | `main` only | SSH deploy to lxp-cpu01 after all tests pass |
| `post-deploy` | `main` only | Notify Control Plane of model changes + trigger Prefect retraining |

Push-cancellation: `workflow: auto_cancel: on_new_commit: interruptible` cancels in-progress runs when a new commit arrives on the same branch. All test jobs are marked `interruptible: true`.

---

## Stage: sanity

### sanity:python-syntax
Runs `python -m py_compile` over every `.py` file in:
`platform/`, `pipelines/`, `serving/`, `tests/`, `tools/`, `modelzoo/seanergys_modelzoo/`

Catches bare syntax errors before installing any dependencies.

### sanity:check-structure
Asserts that the Phase 15 monorepo layout is intact:

```
platform/cli/src/examlops          (examlops CLI package)
platform/services/dashboard        (dashboard service)
platform/services/control_plane    (control plane service)
platform/infra/docker-compose/docker-compose.yml
pipelines/models/                  (per-model YAML configs)
```

---

## Stage: test

### test:modelzoo
**Image:** `python:3.12-slim` | **Toolchain:** Poetry

Installs the `seanergys-modelzoo` package with dev + ci extras from `modelzoo/`, then runs:

| Check | Command |
|---|---|
| Lint | `ruff check seanergys_modelzoo ci tests` |
| Format | `ruff format --check …` |
| Type check | `mypy … --ignore-missing-imports` (informational, `\|\| true`) |
| Unit tests | `pytest tests/unit/` — 7 files |
| Smoke tests | `pytest tests/smoke/` — 2 files (datasets + models, no network) |
| Integration tests | `pytest tests/integration/` — 3 files (see note below) |
| Import check | `from seanergys_modelzoo.logger …` |

> `test:modelzoo` is `allow_failure: true`: `modelzoo/` is a read-only mirror of the upstream
> `seanergys-modelzoo` repo (which owns its own formatting and tests), so its result surfaces
> problems without gating the ExaMLOps deploy — matching the `optional: true` in `deploy:lxp`'s needs.

**Integration test behaviour:**
- `test_model_pipeline.py` — uses `is_dummy=True`, always runs fully offline.
- `test_model_train.py` / `test_model_save_load.py` — use fixture files from `modelzoo/tests/fixtures/sample_data/`. They **auto-skip** when fixtures are absent (run `make sample-data` inside `modelzoo/` to generate them). `sample_pm100.parquet` ships in the repo so JPCP-based cases run without extra setup.

### test:infra:compose
**Image:** `docker:25` + `docker:25-dind` service

Validates all three Compose profiles:
```bash
docker compose -f platform/infra/docker-compose/docker-compose.yml config --quiet
docker compose … --profile monitoring config --quiet
docker compose … --profile dev config --quiet
```

> **Runner requirement:** The GitLab runner executing this job must be configured with `privileged = true` in its `config.toml` to enable Docker-in-Docker.

### test:infra:slurm-lint
**Image:** `python:3.12-slim`

```bash
ruff check platform/infra/slurm-adapter/
```

### test:infra:alert-rules
**Image:** `prom/prometheus:v2.54.1` (has `promtool` on PATH, no DinD needed)

```bash
/bin/promtool check rules platform/infra/docker-compose/alert_rules.yml
```

Validates all PromQL expressions and rule syntax for the six alert rules (RayServeHighErrorRate, RayServeHighLatencyP99, RayServeNoModelsLoaded, RayServeReloadFailures, ApprovalsStale, TargetDown).

### test:examlops
**Image:** `python:3.12-slim` | **Toolchain:** uv

Installs the full workspace dev environment (`uv pip install -e ".[dev]"`), then runs:

| Check | Command |
|---|---|
| Lint | `.venv/bin/ruff check platform/cli/src/ tests/ pipelines/ serving/ platform/services/` |
| Format | `.venv/bin/ruff format --check …` (enforced — a hard failure) |
| Type check | `.venv/bin/mypy … --ignore-missing-imports` (informational, `\|\| true`) |
| Unit tests | `.venv/bin/pytest tests/unit/` covering CLI, control plane, pipelines, serving, inference pipeline, framework adapter, registry integrity, agent tools, and more |
| Dashboard tests | `pytest platform/services/dashboard/backend/tests/` using SQLite in-memory DB |

`ruff` is pinned to an exact version (`ruff==0.15.6` in `[dev]`) so `ruff format --check` is
deterministic — a loose lower bound let CI install a newer formatter than the code was written
with, breaking the gate on every ruff release. Run `ruff format` locally before pushing.

Dashboard tests run against a fully in-memory setup; the conftest at `platform/services/dashboard/backend/tests/conftest.py` provides all required secrets (DATABASE_URL, JWT secrets, Fernet key). The job sets `EXAMLOPS_DOCS_ROOT="$CI_PROJECT_DIR"` (the docs router's auto-detection of the project root fails in CI's checkout layout) and skips `test_storage.py` (a moto/aiobotocore `raw_headers` version incompat — test-infra only, not a product bug).

The `before_script` uses `uv venv --clear .venv`: the `uv` cache restores `.venv` between runs, so a plain `uv venv .venv` intermittently failed with "a virtual environment already exists".

### test:integration
**Image:** `python:3.12` (full image — Ray needs system libs) | **Toolchain:** uv

```bash
.venv/bin/pytest tests/integration/ -v --tb=short
```

Runs `test_inference_pipeline_e2e.py`, which:
1. Starts a real Ray + Serve cluster with three deployments (`InferencePipelineIngress → FeatureTransformer → ModelRouter`)
2. Spins up a threading HTTP mock server as a stand-in for the downstream `MultiModelServer`
3. Sends live HTTP POST requests and verifies prediction responses, 422 validation errors, and 404 model-not-found cases

**Timeout:** 15 minutes.

**`allow_failure: true`** — This job is allowed to fail without blocking deploy because Ray requires ≥ 4 CPUs and GitLab shared runners typically provide 2. Once a dedicated runner with ≥ 4 CPUs is registered, set `allow_failure: false` in `.gitlab-ci.yml` to make this test blocking.

---

## Stage: deploy

### deploy:lxp
**Image:** `ubuntu:22.04` | **Runs on:** `main` only

Connects to `lxp-cpu01` via SSH and runs a rolling deploy:

```bash
# If repo not cloned yet
git clone $LXP_DEPLOY_REPO $LXP_DEPLOY_PATH

# Else update
cd $LXP_DEPLOY_PATH && git pull origin main

# Restart services
docker compose -f platform/infra/docker-compose/docker-compose.yml \
               -f platform/infra/docker-compose/docker-compose.lxp.yml up --build -d
```

The job registers a GitLab **Environment** (`production-lxp`) so every deploy is recorded in the GitLab UI under **Deployments → Environments**, with a link to the dashboard at `http://$LXP_HOST:18099`.

---

## Stage: post-deploy

Both jobs run in parallel after `deploy:lxp`.

### post-deploy:notify-model-changes
Calls `platform/ci/notify_model_changes.py` to detect which model files changed in this push and POST them to `Control Plane /api/changes` as pending approvals.

GitLab CI variables used (equivalent to GitHub's `event.before` / `sha` / `head_commit.message`):

| GitLab variable | Passed as |
|---|---|
| `$CI_COMMIT_BEFORE_SHA` | `--before` |
| `$CI_COMMIT_SHA` | `--after` |
| `$CI_COMMIT_MESSAGE` | `--commit-msg` |

Fails silently (`|| true`) if the Control Plane is unreachable — never blocks post-deploy.

### post-deploy:retrain-push-models
POSTs to `Control Plane /retrain` for each registered model (JPCP, MACK, MCBound) with dataset `FDataDataset`. Requires `CONTROL_PLANE_URL` and `CONTROL_PLANE_TOKEN` to be set; skips gracefully if either is absent. Exits non-zero if any model trigger fails.

---

## Required CI/CD variables

Set these in **GitLab → Project → Settings → CI/CD → Variables** before the first pipeline run.

| Variable | Mask | Protect | Value |
|---|---|---|---|
| `LXP_SSH_KEY` | ✅ | ✅ | ED25519 private key for lxp-cpu01 (see setup below) |
| `LXP_HOST_KEY` | ✅ | | One line from `ssh-keyscan <REMOTE_HOST>` |
| `LXP_USER` | | | SSH username on the deploy node |
| `LXP_HOST` | | | `<REMOTE_HOST>` |
| `LXP_DEPLOY_PATH` | | | Absolute repo path on lxp-cpu01, e.g. `$EXAMLOPS_DEPLOY_PATH` |
| `LXP_DEPLOY_REPO` | | | GitLab SSH URL of this repo |
| `LXP_MODELZOO_REPO` | | ✅ | Clone URL of the **upstream** `software/modelzoo` repo. `seanergys_modelzoo` is not vendored in this repo (ADR 0094) — the deploy job fetches it into `$LXP_DEPLOY_PATH/modelzoo`, which is where `EXAMLOPS_MODELZOO_DIR` resolves by default. Unset ⇒ not fetched, and training/serving fail until it is present. |
| `LXP_CONTROL_PLANE_URL` | | | `http://lxp-cpu01:18002` |
| `LXP_CONTROL_PLANE_TOKEN` | ✅ | ✅ | Bearer token set in Control Plane's `CONTROL_PLANE_TOKEN` env var |

**Masked** variables are hidden in job logs. **Protected** variables are only injected into pipelines running on protected branches (e.g. `main`).

---

## One-time lxp-cpu01 server setup

The deploy job SSHes into lxp-cpu01 and the server must be able to pull from the GitLab repo. Two keys are involved:

### 1. CI runner → lxp-cpu01 (deploy SSH key)

```bash
# On your local machine: generate a dedicated deploy key
ssh-keygen -t ed25519 -C "gitlab-ci-deploy" -f ~/.ssh/examlops_deploy

# Add the PUBLIC key to lxp-cpu01
ssh-copy-id -i ~/.ssh/examlops_deploy.pub <DEPLOY_USER>@<REMOTE_HOST>

# Store the PRIVATE key in GitLab CI variable LXP_SSH_KEY
cat ~/.ssh/examlops_deploy
```

Grab the host key for `LXP_HOST_KEY`:
```bash
ssh-keyscan <REMOTE_HOST>
# Copy one ed25519 or ecdsa line → store as LXP_HOST_KEY
```

### 2. lxp-cpu01 → GitLab (deploy read key)

lxp-cpu01 needs to `git clone / git pull` from the GitLab repo. The cleanest way is a GitLab deploy key:

```bash
# On lxp-cpu01: generate a key if one doesn't exist
ssh-keygen -t ed25519 -C "lxp-deploy" -f ~/.ssh/gitlab_deploy

# Copy the PUBLIC key
cat ~/.ssh/gitlab_deploy.pub
```

Register this public key in **GitLab → Project → Settings → Repository → Deploy keys** (read-only access). Then on lxp-cpu01:

```bash
# ~/.ssh/config
Host gitlab.example.com
  HostName gitlab.example.com
  User git
  IdentityFile ~/.ssh/gitlab_deploy
```

Test with: `ssh -T git@gitlab.example.com`

---

## Enabling blocking integration tests

Once a GitLab runner with ≥ 4 CPUs is available, remove `allow_failure: true` from `test:integration` in `.gitlab-ci.yml`:

```yaml
# Before
test:integration:
  allow_failure: true   # remove this line

# After
test:integration:
  timeout: 15 minutes
```

The deploy stage will then only proceed if the Ray Serve end-to-end test passes.

To register a self-hosted runner on lxp-cpu01:

```bash
# On lxp-cpu01
docker run --rm -it \
  -v /srv/gitlab-runner/config:/etc/gitlab-runner \
  -v /var/run/docker.sock:/var/run/docker.sock \
  gitlab/gitlab-runner register
```

Use the **Docker** executor with `privileged = true` (needed for `test:infra:compose`).

---

## Cache strategy

| Cache key | Contents | Used by |
|---|---|---|
| `$CI_COMMIT_REF_SLUG-poetry` | `.cache/pip` | `test:modelzoo` |
| `$CI_COMMIT_REF_SLUG-uv` | `.cache/uv`, `.venv` | `test:examlops` (pull-push), `test:integration` (pull) |

Caches are per-branch. The first pipeline run on a new branch installs everything from scratch; subsequent runs reuse the cached venvs.

---

## Local CI mirror

The Makefile mirrors each CI job group so contributors can reproduce failures without pushing:

```bash
make ci              # run all three groups
make ci-modelzoo     # poetry: lint + unit + smoke tests
make ci-infra        # compose validation + slurm lint + alert-rules check
make ci-examlops     # uv: lint + mypy + unit + dashboard tests
```

Note: `make ci` does not run the Ray Serve integration test — run it directly with:
```bash
.venv/bin/pytest tests/integration/ -v --tb=short
```

---

## GitHub Actions retirement

Once the GitLab pipeline is live and passing on `main`:

1. Disable or delete `.github/workflows/ci.yml` and `.github/workflows/deploy.yml`
2. Update the repo README to point to the GitLab pipeline badge
3. Optionally keep GitHub as a read-only mirror via GitLab's **Settings → Repository → Mirroring repositories**

---

## Troubleshooting

**`test:infra:compose` fails with "Cannot connect to the Docker daemon"**
The runner is not running in privileged mode. Edit the runner's `config.toml`:
```toml
[[runners]]
  [runners.docker]
    privileged = true
```

**`test:integration` times out or crashes with OOM**
The runner has fewer than 4 CPUs or < 4 GB RAM. Either increase runner resources or leave `allow_failure: true` in place.

**`deploy:lxp` fails with "Host key verification failed"**
The `LXP_HOST_KEY` variable is empty or contains the wrong host key. Re-run `ssh-keyscan <REMOTE_HOST>` and update the variable.

**`post-deploy:lxp:retrain-push-models` fails with connection refused**
The Control Plane container on lxp-cpu01 did not start. Check `docker compose logs control-plane` on lxp-cpu01. The deploy job starts the stack with `up --build -d` but does not wait for health checks; a brief startup delay can cause this. Re-running the job manually after a minute usually succeeds.

**`sanity:check-structure` fails after a refactor**
The job asserts the Phase 15/16 directory layout. If you move or rename a top-level area, update the `script:` section of `sanity:check-structure` in `.gitlab-ci.yml` to match.
