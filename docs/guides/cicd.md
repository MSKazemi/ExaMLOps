# CI/CD

The repository contains two pipelines:

- **GitHub Actions** (`.github/workflows/`) is the CI for this repository. `ci.yml` gates every
  pull request and every push to `main`; see
  [GitHub Actions — the pull-request gate](#github-actions-the-pull-request-gate). Security
  scanning, releases and the OpenSSF Scorecard are covered in the [release process](release-process.md).
- **GitLab CI** (`.gitlab-ci.yml`) is the pipeline for a self-hosted install. On a GitLab instance
  it runs on every push and merge request, covers all four source areas (modelzoo, infra,
  examlops, integration), and deploys to the site's host after a green run on `main`. Most of
  this page describes that pipeline.

---

## Pipeline overview

Eight stages arranged as a DAG. The twelve check jobs run in parallel; build, deploy, smoke
and post-deploy fire only on `main`, and `release` fires only on a tag.

```
sanity:python-syntax   ─┐    test:modelzoo  (allow_failure — gates nothing by design)
sanity:check-structure ─┤
sanity:secret-scan     ─┼─►  test:infra:compose     ─┐
                        │    test:infra:slurm-lint   │   (tag only)
                        │    test:infra:alert-rules  ├─► release:gitlab
                        │    test:infra:helm         │
                        └─►  test:examlops           │
                             test:postgres           ├─► deploy:lxp ─► smoke:lxp ─► post-deploy:lxp:notify-model-changes
                             test:integration        │                              └► post-deploy:lxp:retrain-push-models
                             test:agent              │
                             test:frontend           │
                             test:control-plane     ─┘
                                                     │
                                                     └─► build:images (×11, opt-in)
                                                              ├─► publish:ghcr        (allow_failure)
                                                              ├─► publish:dockerhub   (allow_failure)
                                                              └─► deploy:lxp
```

> **`needs:` is the gate, not the stage order.** `deploy:lxp` and `release:gitlab` declare
> `needs:`, which makes them DAG jobs: GitLab starts them as soon as *the jobs they name*
> succeed, no matter what else in the pipeline has failed. A blocking job left out of that list
> turns the pipeline red **and lets production be deployed anyway**. Until 2026-08-20 two were
> missing — `test:postgres` and `sanity:secret-scan`, the latter being the job whose entire
> purpose is to stop a credential reaching a shared remote. Both lists now name every blocking
> `sanity`/`test` job, and `tests/unit/test_ci_gate_coverage.py` fails if a new one is added and
> not wired in (mark it `allow_failure: true` to say out loud that it only advises).

| Stage | Runs on | Purpose |
|---|---|---|
| `sanity` | all branches + MRs | Syntax check + directory structure guard — blocks everything on failure |
| `test` | all branches + MRs | Ten parallel jobs covering all test types (change-filtered off `main`) |
| `build` | `main` + tags, only when `EXAMLOPS_USE_REGISTRY` is set | Builds all eleven service images once and pushes them to the GitLab Container Registry |
| `publish` | after `build`, `allow_failure` | Copies those exact digests to GHCR and Docker Hub for public visibility |
| `release` | tags only | Turns the tag into a GitLab Release described by its CHANGELOG section |
| `deploy` | `main` only, never on a schedule | SSH deploy to lxp-cpu01 after every blocking check named in its `needs:` passes |
| `smoke` | after `deploy` | Post-deploy health gate with automatic rollback |
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

### sanity:secret-scan
Runs `exa secrets scan` over `platform/` and `pipelines/` — the repo's own scanner, so the rule
set is the one the CLI ships rather than a separate CI-only list.

**Blocking, and since 2026-08-20 actually blocking:** it is named in `deploy:lxp`'s and
`release:gitlab`'s `needs:`. Before that it could go red while the same pipeline deployed to
lxp-cpu01 and published a release — a credential-exposure gate that stopped nothing. Locally it
is step 3/15 of `make preflight`.

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
- `test_model_train.py` / `test_model_save_load.py` — use fixture files from `modelzoo/tests/fixtures/sample_data/`. They **auto-skip** when fixtures are absent. Generate them with
  `cd modelzoo && poetry run python scripts/create_sample_data.py`. (Several docstrings in that upstream
  tree still point at a Makefile target for this; `modelzoo/Makefile` is empty, so the script is the
  working route. modelzoo is read-only here, so the wording there is not ours to correct.) `sample_pm100.parquet` ships in the repo so JPCP-based cases run without extra setup.

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

### test:infra:helm
**Image:** `alpine/helm:3.16.3` (entrypoint cleared; `apk add make python3` for the guards)

```bash
make helm-validate     # lint · no-registry refusal check · render
pytest tests/unit/test_helm_chart.py tests/unit/test_dockerfile_build_context.py
make helm-package      # the same command a release would run, minus the copy
```

The chart is a published artifact strangers install, and until this job existed **nothing gated
it** — which is how four defects reached it, including a default that could never install and an
`appVersion` eleven releases behind. `make helm-validate` is called rather than inlined so CI and
`make preflight` cannot drift apart. The `kubectl apply --dry-run=client` step inside the target
self-skips here: it downloads the OpenAPI schema from a live apiserver, so it is not an offline
check and a runner has no cluster. Structure is covered by the pytest guards instead.

### test:docs
**Image:** `python:3.12-slim` | **Runs on:** `main`, tags, and any change to `docs/**`, `mkdocs.yml` or `.gitlab-ci.yml`

`mkdocs build --clean --strict`, so a broken link or a heading anchor that no longer exists fails
the build instead of reaching a reader. It is in both `deploy:lxp`'s and `release:gitlab`'s
`needs:`, which makes it a blocking gate — documentation cannot be broken on `main` and still ship.

`--strict` fails on a broken *link*. It says nothing about a page in no navigation; that is
`tests/unit/test_docs_are_reachable.py`, inside `test:examlops`. Locally: `make docs-build`, step
15/16 of `make preflight`.

### test:examlops
**Image:** `python:3.12-slim` | **Toolchain:** uv

Installs the full workspace dev environment (`uv pip install -e ".[dev]"`), then runs:

| Check | Command |
|---|---|
| Lint | `.venv/bin/ruff check platform/cli/src/ tests/ pipelines/ serving/ platform/services/ platform/clients/ usecases/` |
| Format | `.venv/bin/ruff format --check …` (enforced — a hard failure) |
| Type check | `.venv/bin/mypy … --ignore-missing-imports` (informational, `\|\| true`) |
| Unit tests | `.venv/bin/pytest tests/unit/` covering CLI, control plane, pipelines, serving, inference pipeline, framework adapter, registry integrity, agent tools, and more |
| Dashboard tests | `pytest platform/services/dashboard/backend/tests/` using SQLite in-memory DB |

`ruff` is pinned to an exact version (`ruff==0.15.6` in `[dev]`) so `ruff format --check` is
deterministic — a loose lower bound let CI install a newer formatter than the code was written
with, breaking the gate on every ruff release. Run `ruff format` locally before pushing.

Dashboard tests run against a fully in-memory setup; the conftest at `platform/services/dashboard/backend/tests/conftest.py` provides all required secrets (DATABASE_URL, JWT secrets, Fernet key). The job sets `EXAMLOPS_DOCS_ROOT="$CI_PROJECT_DIR"` (the docs router's auto-detection of the project root fails in CI's checkout layout) and skips `test_storage.py` (a moto/aiobotocore `raw_headers` version incompat — test-infra only, not a product bug).

The `before_script` uses `uv venv --clear .venv`: the `uv` cache restores `.venv` between runs, so a plain `uv venv .venv` intermittently failed with "a virtual environment already exists".

### test:postgres
**Image:** `python:3.12-slim` | **Service:** `postgres:16-alpine` | **Toolchain:** uv

`EXAMLOPS_DB_BACKEND=postgres` is a supported production engine, and until this job existed
nothing in CI ever ran on it — every other job used the SQLite default, so a dialect regression
could only be found by someone remembering to run `make test-postgres` locally.

Runs both suites that carry the platform's data layer against a real Postgres 16 service:

```bash
EXAMLOPS_POSTGRES_SCHEMA=exa_ci      pytest tests/unit/
EXAMLOPS_POSTGRES_SCHEMA=exa_ci_dash pytest platform/services/dashboard/backend/tests/
```

The dashboard suite is included deliberately: it is a separate app with its own connection
adapter, and every Postgres-specific defect found so far surfaced there first. The two suites get
separate schemas because each has its own truncate-based isolation fixture and sharing one would
let them race.

`psycopg` is installed in this job rather than added to the root `[dev]` extra — it is what makes
the Postgres engine *optional*, and installing a driver into every SQLite job would weaken that.

**Runs on:** `main` and tags always; on branches/MRs only when `examlops/storage/`,
`platform_db*.py`, the dashboard backend or `.gitlab-ci.yml` change. Locally: `make test-postgres`,
which spins a throwaway container and runs the same three suites plus the live round-trip test —
now also step 15/15 of `make preflight` (last, because it is the slow one, and gated on a docker
daemon rather than skipped silently).

**Blocking, and since 2026-08-20 actually blocking.** It was omitted from `deploy:lxp`'s and
`release:gitlab`'s `needs:`, so a dialect regression on the production engine turned the pipeline
red without stopping either. Both now require it.

---

### test:integration
**Image:** `python:3.12` (full image — Ray needs system libs) | **Toolchain:** uv

```bash
.venv/bin/pytest tests/integration/ -v --tb=short
# after the Redis service passes its readiness probe:
EXAMLOPS_REDIS_TEST_URL=redis://redis:6379/15 \
  .venv/bin/pytest tests/integration/test_redis_coordination_live.py -v --tb=short
```

Runs `test_inference_pipeline_e2e.py`, which:
1. Starts a real Ray + Serve cluster with three deployments (`InferencePipelineIngress → FeatureTransformer → ModelRouter`)
2. Spins up a threading HTTP mock server as a stand-in for the downstream `MultiModelServer`
3. Sends live HTTP POST requests and verifies prediction responses, 422 validation errors, and 404 model-not-found cases

It then runs the opt-in Redis contract against an ephemeral `redis:7.4-alpine` service. A bounded
PING loop must succeed first. The live checks cover lock contention, lease expiry/failover,
concurrent idempotency deduplication, and the Redis Streams event envelope. The URL is exported only
for this second invocation, so the normal integration suite remains offline and the live module
still skips when run locally without `EXAMLOPS_REDIS_TEST_URL`.

**Timeout:** 15 minutes.

**Blocking.** This job once carried `allow_failure: true` because Ray needs ≥ 4 CPUs and the shared runners provided 2; the active runner satisfies that, so a failure here now fails the pipeline and blocks deploy.

---

### test:deps:audit

`pip-audit` over the resolved dependency set, reported in the job log and kept as an
artifact.

**This is advisory, not a gate, and that is a deliberate state rather than an oversight.**
No baseline has been established: making it blocking today would redden the pipeline on
whatever advisories the current lockfile already carries, and the reliable outcome of a gate
that fails for reasons nobody chose is that somebody deletes the gate. The intended path is
to run it advisory, read the report, fix or explicitly accept each finding, and *then* set
`allow_failure: false`. Until that is done it is a report.

Runs on `main`, tags, the nightly schedule, and any change to a dependency manifest.

## Stage: build

Everything in this stage is inert until the project CI/CD variable `EXAMLOPS_USE_REGISTRY`
is set. Without it the pipeline behaves exactly as it did before the stage existed, and
`deploy:lxp` keeps building images on the node.

### build:images

Builds each service image once, from a tree that has already passed every blocking check,
and pushes it to the Seanergys GitLab Container Registry as
`$CI_REGISTRY_IMAGE/examlops-<service>:<sha>` (plus `:latest` on `main` and `:<tag>` on a
tag).

**Why it exists.** `platform/ci/lxp_release.sh` used to run `docker compose up --build` on
lxp-cpu01 on every deploy. That had three problems, and this job is the answer to all three:

1. **The node built from the internet.** LXP's container egress is firewalled (see
   `design/` and the sysadmin thread on the leftover firewalld `FORWARD` drop), so every
   deploy was one `apt-get` or `pip install` away from failing for a reason nothing in this
   repository controls.
2. **Rollback rebuilt.** `smoke:lxp` reactivates the previous release on a failed health
   gate — which re-ran the same build. The "return to the known-good release" path could
   therefore produce an image that had never existed before, at exactly the moment the
   platform was already unwell.
3. **Nothing was pinned.** Two deploys of the same commit could differ.

**How the service list stays honest.** The `parallel: matrix` names *only* service names.
`platform/ci/build_image.sh` then asks the compose file itself where that service's build
context and Dockerfile are, so CI cannot build an image from a different context than the
one the deploy runs. `tests/unit/test_ci_image_matrix.py` holds the matrix against the
compose file in both directions and fails if a buildable service is missing from either.

**`seanerbus-bridge` is deliberately not built here.** Its build context is the *parent* of
this repository — it needs the sibling `seanerbus` checkout, which a CI clone does not have.
It continues to build on the node. The same is true of the JupyterLab spawner image, which
`lxp_release.sh` builds directly rather than through compose.

**Runner requirement.** Docker-in-Docker, so the Seanergys runner must be privileged. If it
is not, replace the `dind` service with `kaniko` or `buildah`: only this job's
`before_script` changes, `build_image.sh` does not.

Layer cache lives in the registry beside the image
(`--cache-to type=registry,...,mode=max`), so a runner with a cold disk still reuses layers.

## Stage: scan

### scan:images

`trivy` against the images `build:images` just pushed, at HIGH and CRITICAL severity, one
report per service kept as an artifact.

It scans **by digest**, so the report describes the exact image the node is about to run
rather than a mirror or a rebuild of it. It runs before the publish stage for the same
reason.

Like `test:deps:audit` it is **advisory pending a baseline** — same reasoning, same path to
becoming a gate (`allow_failure: false` plus `--exit-code 1`). Inert unless
`EXAMLOPS_USE_REGISTRY` is set, since without it there are no built images to scan.

## Stage: publish

Mirrors, not delivery. Both jobs are `allow_failure: true` — an outage at GHCR, a rotated
Docker Hub token or a pull-rate limit must never turn a healthy production deploy red.

They copy **digests**, with `skopeo copy`, never rebuilds. The image a stranger pulls from
GHCR is therefore bit-identical to the one lxp-cpu01 is running, rather than a lookalike
built from the same source at a different moment.

### publish:helm

`make helm-package` has always produced a complete, publishable Helm repository in
`dist/helm` — chart tarball plus `index.yaml` — and nothing ever published it. The chart was
validated on every pipeline and installable by nobody.

This publishes it in **both shapes Helm consumers actually use**:

- **OCI** — `helm push` to `oci://$CI_REGISTRY_IMAGE/charts`, and to
  `oci://ghcr.io/mskazemi/charts` and `oci://docker.io/mskazemi/charts` when those tokens are
  set. This lands the chart in the same registries as the images it deploys, so one version
  is one artifact set.
- **A classic HTTP repo** — `index.yaml`, published by the `pages` job (in the `release`
  stage), because `helm repo add` still expects that shape.

`allow_failure: true`: distribution, not delivery. A missing mirror token is a configuration
choice and is reported as a skip, not a failure.

### pages

Publishes the documentation site to GitLab Pages, with the classic Helm repository under
`/charts`.

`test:docs` has always run `mkdocs build --strict` — proving the site builds, catching dead
links — and then thrown the result away. This publishes it, so:

```bash
helm repo add examlops $CI_PAGES_URL/charts
```

Unlike the registry mirrors this is **not** `allow_failure`. It depends on nothing outside
GitLab, so a failure here is a real problem with our own content. Its dependency on
`publish:helm` is `optional`, so when the chart job is skipped the docs still publish and the
job says which it did.

It sits in the **`release`** stage rather than `publish` for one reason: that keeps its
`needs:` on `publish:helm` pointing at a strictly earlier stage. Same-stage `needs:` is legal
only from GitLab 14.2, and the failure mode of guessing wrong on a self-managed instance is
that the pipeline is never *created* — nothing runs, and nothing reports that nothing ran.
`tests/unit/test_gitlab_ci_valid.py::test_no_job_needs_another_in_its_own_stage` keeps every
dependency pointing backwards so the assumption never has to be made.

### publish:ghcr

Copies every digest to `ghcr.io/mskazemi/examlops-<service>`. Runs only when `GHCR_TOKEN`
is set. This is the public-visibility half of the registry story: the Seanergys registry is
internal, so nothing built there is visible outside the institute.

### publish:dockerhub

The same copy to `docker.io/mskazemi/examlops-<service>`, gated on `DOCKERHUB_TOKEN`.

---

## Stage: release

### test:agent

Runs the Skipper agent's 206 tests (`platform/services/agent/tests`).

Until 2026-08-20 this suite ran in no pipeline and in no local gate: `make check` is
`lint typecheck test dashboard-check`, and `test` is `pytest tests/` at the repo root, so nothing
reached `platform/services/agent/tests`. A change to `skipper/` could break all 206 with every
gate green — the worst component for that to be true of, because an agent's regressions are the
hardest kind to notice by using it.

It is a separate job rather than part of `test:examlops` because it needs eleven
LangChain/LangGraph packages that the root `[dev]` extras deliberately do not carry. It installs
from `platform/services/agent/requirements.txt` (the pinned source of truth), plus `pytest-asyncio`
and an editable `platform/cli` — the tests import `examlops` the way the agent itself does.

It declares **no cache**: the shared `uv-$CI_COMMIT_REF_SLUG` key holds the `.venv` every other job
pulls, and pushing a langchain-laden one into it would slow them all down for nothing.

`deploy:lxp` and `release:gitlab` both require it. Locally: `make ci-agent` (or `make skipper-test`),
and it is step 10/15 of `make preflight`.

### test:frontend

Runs the dashboard frontend's lint, its 373 vitest tests, and `tsc -b && vite build`
(`platform/services/dashboard/frontend`).

Until 2026-08-20 the frontend ran in **no pipeline at all**, and `tsc -b` executed only inside the
image build in `deploy:lxp` — so a TypeScript error surfaced on the production node, after
`release:gitlab` had already tagged. That is the most expensive place a compile error can be
found. The 373 component tests gated nothing whatsoever.

It uses `node:24-alpine`, matching `Dockerfile.dashboard`'s builder stage, and `npm ci` rather
than the image build's `npm install --include=dev`, so CI is lockfile-exact. The whole job takes
roughly 70 s locally (`npm ci` 12 s · lint 16 s · vitest 29 s · build 15 s).

`deploy:lxp` and `release:gitlab` both require it. Locally: `make ci-frontend`, and it is step
11/15 of `make preflight`. `make dashboard-check` runs the same four steps as part of `make check`.

### test:control-plane

Runs the control plane service's own 82 tests (`platform/services/control_plane/tests`) — the
approval-gate reliability suite, weak-token rejection, the modelzoo webhooks, and model metadata.

Like `test:agent` and `test:frontend` before it, this suite ran in **no pipeline and no local
gate**: `make test` is `pytest tests/` at the repo root, and `test:examlops` runs `tests/unit/`,
so neither reaches `platform/services/`. It had also rotted past the point of running at all —
`pytest` could not even collect it (`ModuleNotFoundError: No module named 'model_meta'`), because
the service runs with its own directory as the working directory and its tests import
`model_meta`/`metrics` the same way. A `tests/conftest.py` puts that directory on `sys.path`, the
same idiom `platform/services/agent/tests/conftest.py` uses. With it, all 82 pass.

The service has **no `requirements.txt`** — its dependencies are an inline `pip install` line in
its Dockerfile — so the job installs what `app.py`/`metrics.py`/`model_meta.py` actually import
(`fastapi`, `uvicorn[standard]`, `prometheus_client`, `pyyaml`) plus the test runner. That set was
proved in a clean throwaway venv before being written here.

`deploy:lxp` and `release:gitlab` both require it. Locally: `make ci-control-plane`, and it is
step 12/15 of `make preflight`.

> The image build still uses `npm install --include=dev`, which does not honour the lockfile.
> Switching it to `npm ci` would make the deployed bundle reproducible; it is not done here
> because it cannot be proved without a Docker daemon.

### release:gitlab
**Image:** `registry.gitlab.com/gitlab-org/release-cli` | **Runs on:** tags only

The project had a full tag history and *zero* GitLab Releases, so tags carried no notes and
nothing linked a version to what changed in it. This job turns each tag into a Release whose
description is that version's own `CHANGELOG.md` section — extracted with `awk`, so there are no
hand-written notes to keep in sync.

If `CHANGELOG.md` has no section for the tag, the job **fails**. That is deliberate: it is the
cheapest possible check that the changelog was updated before tagging. It also makes the extractor
a release blocker, so it has to be right about a section that *is* there — and for two releases it
was not. The file carries both `## [0.46.0]` and `## [v0.48.0]` heading styles while the job strips
the leading `v` from the tag, so `v0.47.0` and `v0.48.0` extracted to nothing and would have failed
the tag pipeline after every job in the `needs:` list above had passed. The match now takes either
style, escapes the dots in the version, and collects every section a version heads.
`tests/unit/test_release_notes_are_extractable.py` runs the **real awk program parsed out of
`.gitlab-ci.yml`** against every tag, so the test cannot drift from the job.

`v0.29.0`, `v0.30.0` and `v0.36.0` have no CHANGELOG section at all; they predate this job and are
named as exemptions in that test, so no new tag can join them silently.

---

## Stage: deploy

### deploy:lxp
**Image:** `ubuntu:22.04` | **Runs on:** `main` only, never on a scheduled pipeline

Three rules, in order — GitLab takes the first match:

| Condition | Result | Why |
|---|---|---|
| `$CI_PIPELINE_SOURCE == "schedule"` | `never` | A nightly pipeline exists to *report* on `main`, not to ship it. A scheduled run satisfies the branch condition below, so without this it would redeploy production every night. |
| `main` **and** `$DEPLOY_REQUIRES_APPROVAL` set | `manual` | Production becomes a button rather than an automatic consequence of merging. This is the tier-free substitute for GitLab's Premium deployment approvals: it gates *when*, not *who*, which is the half that matters with one maintainer. |
| `main` | `on_success` | Default: merge to `main` deploys. |

`resource_group: production-lxp` is shared with `smoke:lxp`. There is one node, so a second
pipeline cannot change the active release while the first pipeline is still running its health
gate.


The runner creates an archive from the exact tested commit and copies it to the node. The node
extracts it into a commit-addressed directory and activates it:

```bash
git archive --format=tar.gz -o examlops-$CI_COMMIT_SHA.tar.gz $CI_COMMIT_SHA
scp examlops-$CI_COMMIT_SHA.tar.gz lxp:/tmp/
ssh lxp platform/ci/lxp_release.sh deploy $LXP_DEPLOY_PATH $CI_COMMIT_SHA /tmp/archive
```

The resulting layout separates source from mutable data:

| Path | Role |
|---|---|
| `$LXP_DEPLOY_PATH-releases/<commit>` | Exact application source for one tested commit |
| `$LXP_DEPLOY_PATH-current` | Symlink to the active production release |
| `$LXP_DEPLOY_PATH-state` | Persistent `platform.db`, authored providers, environment files, and upstream model library |
| `$LXP_DEPLOY_PATH` | Preserved legacy/user workspace; never reset or cleaned by CI |

Jupyter user homes and project workspaces remain Docker volumes. Notebook-authored source files may
change the active release directory, but they cannot contaminate the next release because each
deployment starts from a new archive.

The job registers a GitLab **Environment** (`production-lxp`) so every deploy is recorded in the GitLab UI under **Deployments → Environments**, with a link to the dashboard at `http://$LXP_HOST:18099`.

---

### Release retention on the node

`lxp_release.sh` keeps each deploy as an immutable directory under
`$LXP_DEPLOY_PATH-releases/<sha>`. Nothing removed them, which on an NFS share is a
slow-motion outage: the deploy that finally fills the volume is the one that fails, long
after the commits that consumed it.

Each activation now prunes to the **`EXAMLOPS_KEEP_RELEASES` most recent (default 5)**. Two
directories are never deleted even when they fall outside that budget — the running release,
and the one the symlink pointed at before this activation, because that is exactly what
`smoke:lxp` and `rollback:lxp` restore. When that happens the prune says so in the job log
rather than quietly reporting the budget as met.

Set it as a project CI/CD variable, or in the node's environment, to trade disk for rollback
depth.

### Deploy events in the audit chain

The platform keeps a tamper-evident, hash-chained audit log of what it does to itself.
Deployment was the one production change missing from it, so `exa audit` could report a model
promotion at 14:02 and say nothing about the release that changed underneath it at 14:00.

`platform/ci/record_deploy.py` now writes a `release_deploy` — or `release_rollback` — event
on every activation, carrying the release path, commit SHA, pinned image tag and deploy node,
attributed to the GitLab user who triggered it. Read it with `exa audit --last 7d` and check
it with `exa audit verify`.

An audit failure never fails a deploy. A missing row is a gap in the record; a deploy aborted
over telemetry is an outage. The script therefore catches everything, prints a warning into
the job log, and exits 0.

## Stage: smoke

### smoke:lxp

Waits `$SMOKE_STARTUP_WAIT`, then runs `platform/ci/smoke_check.sh` on the node, retrying up to
`$SMOKE_RETRY_COUNT` times. If every attempt fails it **reactivates the previous release directory**
recorded in the `prev_release.txt` artifact, re-checks health, and then exits 1 regardless
— a rollback is a recovery, not a success, and the bad commit must still fail the pipeline.

Two properties of that path are easy to lose and expensive to lose, so
`tests/unit/test_deploy_rollback.py` pins them:

* **No `--remove-orphans`.** On this compose file the flag deletes every profile service — the six
  monitoring containers, JupyterHub, vllm and the SeanerBUS bridge. `deploy:lxp` was changed to
  stop passing it (a9035877, "persist on-demand services across deploys"); the rollback kept its
  copy until 2026-08-20, so recovering from a bad deploy would have restored the previous code
  while destroying Grafana embeds, project workbenches and the bus tab — at the one moment
  production is already broken and nobody would connect the two.
* **An empty previous release aborts.** A missing or empty artifact never reaches the remote
  activation script.

Activation and rollback use the same script, including CLI refresh and optional profile services,
so the recovery path cannot silently omit deployment steps.


### rollback:lxp

A manual button, present on every `main` pipeline, that returns production to an earlier
release without an SSH session.

`smoke:lxp` already rolls back automatically when the post-deploy health gate fails. This
covers the other case, which had no tooling at all: **a release that passes every probe and
is found to be wrong later, by a human.** Undoing it meant SSH-ing to the node, knowing the
release directory layout, and typing the right SHA — at the moment someone is already under
pressure.

**Running it.** Click ▶ on the job. It prints the releases on the node (`*` marks the active
one) and, with `ROLLBACK_TO` unset, activates the most recent release that is not the running
one — the answer to "undo the last deploy". To go further back, set the job variable
`ROLLBACK_TO` to a SHA from that list.

**Two deliberate choices:**

- **`needs: []`.** You roll back *because* something went wrong, so the button must be
  clickable in a pipeline where other jobs are red. A `needs:` list would make it unavailable
  in exactly the situation it exists for.
- **`allow_failure: true`.** That is what stops an un-clicked button from blocking the
  pipeline. It does not mean a failed rollback is ignored: the job ends by running the same
  `smoke_check.sh` the deploy gate uses, so a rollback that does not restore health is red.

It shares `resource_group: production-lxp` with `deploy:lxp` and `smoke:lxp`, so it cannot
race a deploy whose health gate is still deciding.

The activation is recorded in the platform audit chain as `release_rollback`, attributed to
the GitLab user who clicked it.

## Stage: post-deploy

The two `post-deploy:lxp:*` jobs run in parallel after `deploy:lxp`. `notify:failure` shares the
stage but is not one of them — it fires only when something upstream failed.

### post-deploy:lxp:notify-model-changes
Calls `platform/ci/notify_model_changes.py` to detect which model files changed in this push and POST them to `Control Plane /api/changes` as pending approvals.

GitLab CI variables used (equivalent to GitHub's `event.before` / `sha` / `head_commit.message`):

| GitLab variable | Passed as |
|---|---|
| `$CI_COMMIT_BEFORE_SHA` | `--before` |
| `$CI_COMMIT_SHA` | `--after` |
| `$CI_COMMIT_MESSAGE` | `--commit-msg` |

Fails silently (`|| true`) if the Control Plane is unreachable — never blocks post-deploy.

### post-deploy:lxp:retrain-push-models
POSTs to `Control Plane /retrain` for each registered model (JPCP, MACK, MCBound) with dataset `FDataDataset`. Requires `CONTROL_PLANE_URL` and `CONTROL_PLANE_TOKEN` to be set; skips gracefully if either is absent. Exits non-zero if any model trigger fails.

### notify:failure
**Image:** `python:3.12-slim` | **Runs on:** `main` only, `when: on_failure`, and only if `NOTIFICATION_WEBHOOK_URL` is set

Runs `platform/ci/notify_failure.py` when something upstream in a `main` pipeline fails. Without
the variable the rule does not match and the job never appears — so an unset webhook is silence,
not an error.

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
| `LXP_CONTROL_PLANE_URL` | | | `http://lxp-cpu01:18002` |
| `LXP_CONTROL_PLANE_TOKEN` | ✅ | ✅ | Bearer token set in Control Plane's `CONTROL_PLANE_TOKEN` env var |
| `DEPLOY_REQUIRES_APPROVAL` | | | **Optional.** Any value turns `deploy:lxp` into a manual button. Unset ⇒ `main` deploys automatically. |

### Container registry (opt-in)

The `build` and `publish` stages do nothing until `EXAMLOPS_USE_REGISTRY` is set. Setting it
changes how production is deployed — the node pulls prebuilt images instead of building them
— so check the two prerequisites below on the real infrastructure first.

| Variable | Mask | Protect | Value |
|---|---|---|---|
| `EXAMLOPS_USE_REGISTRY` | | ✅ | **Optional.** Any value enables `build:images` and switches `deploy:lxp` to pull mode. |
| `GHCR_USER` / `GHCR_TOKEN` | ✅ (token) | | **Optional.** GitHub PAT with `write:packages`; enables `publish:ghcr`. |
| `DOCKERHUB_USER` / `DOCKERHUB_TOKEN` | ✅ (token) | | **Optional.** Docker Hub access token; enables `publish:dockerhub`. |

`CI_REGISTRY`, `CI_REGISTRY_USER`, `CI_REGISTRY_PASSWORD` and `CI_REGISTRY_IMAGE` are
predefined by GitLab — do not set them by hand.

**Prerequisites to confirm before setting `EXAMLOPS_USE_REGISTRY`:**

1. The **Container Registry** feature is enabled on the Seanergys project
   (Settings → General → Visibility → Container Registry). It is not on by default on every
   self-managed instance.
2. The **lxp-cpu01 docker daemon can reach** `registry.gitlab.seanergys.fz-juelich.de`.
   Verify on the node, not by assumption — container egress there has been blocked before
   by a leftover firewalld `FORWARD` drop:

   ```bash
   ssh lxp-cpu01 'docker login registry.gitlab.seanergys.fz-juelich.de'
   ```

3. A **cleanup policy** is configured on the registry (Settings → Packages and registries →
   Clean up image tags). Eleven images tagged with every commit SHA grows without bound
   otherwise. Keeping the most recent 10 SHA tags plus `latest` and every `v*` tag is a
   reasonable starting rule.

**Masked** variables are hidden in job logs. **Protected** variables are only injected into pipelines running on protected branches (e.g. `main`).

---

## One-time lxp-cpu01 server setup

The deploy job streams the tested GitLab checkout to lxp-cpu01, so the node itself needs no GitLab
credential. Only the CI runner-to-node SSH key is required.

### CI runner → lxp-cpu01 (deploy SSH key)

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
make ci                # every job group
make ci-modelzoo       # poetry: lint + unit + smoke tests (upstream; not run by preflight)
make ci-infra          # compose validation + slurm lint + alert-rules check
make ci-examlops       # uv: lint + mypy + unit + dashboard tests
make ci-agent          # the Skipper agent suite
make ci-frontend       # dashboard frontend: lint + vitest + build
make ci-control-plane  # the control plane's own suite
make test-postgres     # the whole suite again on a throwaway Postgres 16
```

Note: `make ci` does not run the Ray Serve integration test — run it directly with:
```bash
.venv/bin/pytest tests/integration/ -v --tb=short
```

### `make preflight` mirrors the blocking jobs, and proves that it does

`preflight` is fifteen steps covering every blocking `sanity`/`test` job. That claim used to
rest on someone remembering to extend it; `tests/unit/test_ci_gate_coverage.py` now enumerates
the blocking jobs out of `.gitlab-ci.yml` and fails if one has no recorded local mirror — so
adding a CI check forces a decision about running it locally rather than leaving the sentence
above quietly false. (It was: `sanity:check-structure`, `sanity:secret-scan` and `test:postgres`
were all missing on 2026-08-20.)

The one deliberate omission is `test:modelzoo`, which preflight names in its closing line —
it needs poetry and an upstream checkout, and it is `allow_failure: true` in CI anyway.

`test:postgres` needs a docker daemon. Without one, preflight **exits 1** rather than skipping;
`make preflight-nopg` runs everything else and ends in red with *"Preflight incomplete —
test:postgres did not run"*, so the omission cannot be mistaken for a pass.

### The gate pins its own toolchain

Every recipe reachable from `make check` invokes its tools through `$(VENV_BIN)` — an
**absolute** path to this repo's `.venv/bin` — rather than a bare `pip`/`pytest`/`ruff`.
A bare name binds to whatever the caller's shell exposes, which fails in two directions:
on a PEP-668 host the gate dies with `externally-managed-environment` for a reason that has
nothing to do with the change under test, and on a host whose system Python is writable it
*passes*, having installed a different dependency set and run a different interpreter than
the rest of the gate used. The second is the dangerous one — a green gate that measured
something else. `tests/unit/test_makefile_gate.py` fails if a bare invocation reappears.

For the same reason `dashboard-check` **fails** rather than skipping when a half cannot
run: no `.venv/bin/pytest` (run `make install-dev`) or no `npm` on `PATH` exits 1. If you
genuinely have no node on the host, run `make dashboard-check-backend` — it says in its own
output that the frontend half did not run, so the omission cannot be mistaken for a pass.

### The suite says when the tree moved under it

`pytest` reads `tests/conftest.py` once at startup and each test module once during collection.
A file saved a few seconds into a twenty-minute run therefore produces a result that belongs to
no version of the tree: part of the run measured the old file, the rest measured the new one.

`tests/conftest.py` stamps the session start and, in the terminal summary, names every tracked or
untracked `.py` written after it:

```
========================= tree changed during this run =========================
  tests/conftest.py
These were written after collection started, so this result may mix two versions of the tree.
Re-run on a settled tree before trusting it.
```

It **reports and never enforces** — the exit status is untouched, because a mid-run edit does not
make the result wrong, only unreliable. In CI nothing writes to the checkout while the suite runs,
so the section never appears. It exists for the local gate, where an editor save, a formatter, or a
second session working the same checkout can land inside the run window. `tests/unit/test_tree_change_reporter.py`
drives the hook with a stamp from the past and from the future, so a reporter that has quietly
stopped firing fails the build instead of reading as "the tree was settled".

### A unit test may not reach a running platform service

The neighbouring accident is a test that passes because something happens to be listening. Two
`exa chat` launcher tests were green for months on the developer's laptop only because a Skipper
agent was answering on `:18004`; the same tests would have failed on a machine without it, and
would have passed while proving nothing on a machine running a *different* agent.

An autouse fixture in `tests/unit/conftest.py` refuses, for the duration of every unit test, a
`connect`/`connect_ex` to **this host** on a port the platform's own services use — 14200 Prefect,
15000 MLflow, 18001 Ray Serve, 18002 control plane, 18004 agent, 18099 dashboard:

```
LiveServiceContacted: this unit test connected to the Skipper agent at 127.0.0.1:18004. Whether
that service is running is a property of this machine, not of the code under test — stub the
client (see tests/unit/test_cli_chat.py::_isolate) or point at a port nothing serves.
```

**`LiveServiceContacted` derives from `BaseException`, not `Exception`, and that is load-bearing.**
Service-probing code catches broadly — that is what a probe *is*. The control plane's own `_ping`
is `try: urlopen(...) except Exception: return False`, and the dashboard's health router swallows
everything its twelve pings can throw. While the guard raised an `AssertionError` it was caught by
the code under test and turned into "the service is down": the guard stayed silent, the test went
green, and the verdict was still whatever happened to be listening on the developer's machine. So
the guard was not enforcing over precisely the code most likely to need it. A `BaseException`
passes through those handlers the way `KeyboardInterrupt` does, and pytest reports it as an error.
The four `except BaseException` handlers in the platform (`platform_db`'s transaction context and
the three `resilience` wrappers) all re-raise, and `retry_on()` does not match this type, so
nothing retries or absorbs it. Each of the three suites has a
`test_the_guard_survives_the_except_exception_every_probe_is_written_with` test pinning it.

The rule is deliberately narrow, because unit tests open sockets for good reasons:
`test_vlm_serving_engine` starts its own `HTTPServer` on an ephemeral port, `test_datastore_reachability`
probes a port it closed itself, and `test_cli_mcp` points at `127.0.0.1:1` precisely because nothing
is there. None of those are affected — only the platform's well-known ports on the local host are.
It is scoped to `tests/unit`, so integration tests keep their real connections. When a test genuinely
needs to talk to one of those ports, stub the client, as `tests/unit/test_cli_chat.py::_isolate` does.

Its first catch was the group of tests that assert every MCP read tool *degrades* rather than
raising: they were calling the real endpoints, so a bare laptop exercised the error branch and a
laptop with the stack up exercised the success branch. The `dead_services` fixture in the same
conftest points every service URL at `127.0.0.1:1` — a refusal, instantly — so the degrade path is
the one that runs everywhere. Request it from any test whose subject is what happens when a service
is *not* there.

The dashboard's backend suite carries the same guard, in
`platform/services/dashboard/backend/tests/conftest.py`. It reads its port list from `settings`
rather than a hand-written constant, so a backing service added there is covered without touching
the guard; `database_url` is excluded, because the Postgres run connects to it for real and should.
It found the defect in both directions at once — one test asserted the SeanerBUS bridge probe
*fails* while doing nothing to make it fail (green here, red on any machine running
`make seanerbus-up`, and red on the lxp node where the bridge is a bare-metal process), and one
asserted a response header while fanning out to nine live services and leaving the result in the
health router's 30-second process-global cache.

The control-plane suite carries a third copy, in
`platform/services/control_plane/tests/conftest.py`, together with an autouse fixture that points
`MLFLOW_TRACKING_URI` / `PREFECT_API_URL` / `RAY_SERVE_URL` / `DASHBOARD_URL` at `127.0.0.1:1`
before `app` is reloaded — the module reads them into constants at import time. Four tests were
calling `GET /status`, which fans out to all four peers, for reasons that had nothing to do with
them (the pending-approval count, the reported probe address); one sibling test had shown the
isolated form for a year by patching `urllib.request.urlopen`, but nothing made it the rule.

All three copies now have a `test_live_service_guard.py` beside them — the dashboard's was added
last, after the suite had run the guard before every one of its tests without anything testing the
guard itself. It reaches its port list through a `guarded_ports` fixture in the conftest rather
than re-deriving it, so the test and the guard cannot drift apart.

That copy is deliberate, and `test_live_service_guard.py` in the same directory is what makes it
safe to keep: the control-plane CI job installs `fastapi`, `uvicorn`, `prometheus_client`, `pyyaml`
and `pytest` and no `examlops` at all, because the service does not depend on the platform package.
A shared helper would give its test suite a dependency the service itself does not have — so the
mechanism is copied, and each copy proves itself.

None of the three guards uses `monkeypatch`. An autouse conftest fixture that requests it pulls it earlier in
setup order for every test in the suite, which reverses teardown order against any fixture that
assumed monkeypatch had already restored the environment — `test_settings.py` assumed exactly that,
and errored the moment the guard existed. A guard must not reorder the suite it guards.

### A default endpoint may not name a container port on `localhost`

Every service here is published on the host under the project's +10000 offset — `14200:4200` for
Prefect, `19000:9000` for MinIO, `18099:8099` for the dashboard. A client therefore has exactly two
correct addresses: the **host** port (`localhost:14200`), or the **service name** inside the compose
network (`http://orchestrator:4200/api`, which compose sets itself). `localhost:4200` is neither —
nothing listens there on the host, and inside the network `localhost` is the caller's own container.

The failure is silent. The control plane defaulted to `localhost:4200`, so a control plane started
outside compose reported a perfectly healthy Prefect as **down** on `/status` and posted its retrain
flow runs into a closed port; `exa backup create` reached `localhost:9000` for MinIO the same way and
recorded an empty object tier. Neither raised.

`tests/unit/test_localhost_defaults_match_published_ports.py` derives the rule instead of listing it:
it parses every `docker-compose*.yml` for `HOST:CONTAINER` mappings and fails on any source default
naming a container-side port on `localhost`, so publishing a new service brings its port under the
guard with no edit. Two exemptions are recorded with reasons and re-checked each run — the inference
pipeline runs *inside* the Ray container, and the SeanerBUS bridge is also run bare-metal in dev,
where it serves on the host's `8003` with no mapping at all.

---

## GitHub Actions — the pull-request gate

`.github/workflows/ci.yml` runs on every pull request, every push to `main`, merge-queue groups
and manual dispatch. It deploys nothing.

| Job | What it proves |
|---|---|
| `lint + unit tests` | `uv lock --check`, then an install **from `uv.lock`**, ruff lint + format, mypy, the ratcheted CLI mypy, the unit suite (`-n auto`), the dashboard backend suite |
| `package` | the `examlops` wheel builds, passes `twine check --strict`, and runs from a directory with no checkout |
| `skipper agent tests` | the agent suite with the agent's own `requirements.txt` |
| `dashboard frontend tests` | `npm ci`, lint, vitest, `tsc -b` + production build |
| `helm chart` | `make helm-validate`, the chart guards, `make helm-package` |
| `control plane tests` | the control-plane suite |
| `docs site` | `mkdocs build --strict` with the pinned toolchain in `platform/ci/requirements-docs.txt` |
| `workflow lint` | actionlint (with shellcheck) + zizmor over every workflow file |
| `dependency review` | pull requests only: no new dependency with a high or critical advisory |
| **`ci-ok`** | runs last with `if: always()` and fails unless every job above passed |

**Require exactly one check in branch protection: `ci-ok`.** Listing jobs individually means a
job added later is silently not required. `ci-ok` always runs, and fails if any job it needs failed
or was cancelled (`dependency review` alone may be skipped, because it only runs on pull
requests). `tests/unit/test_github_workflows_hardened.py` fails when a job in `ci.yml` is missing
from its `needs:`.

### Reproducible installs

The Python job installs with `uv sync --frozen --extra dev`, which gives exactly the versions
`uv.lock` pins. Until 2026-09-10 it ran `uv pip install -e ".[dev]"`, which ignores the lock
and takes the newest of everything. That day `main` went red with no change in the repository: a
new typer release was published and the CLI type check failed against it. With the lock-exact
install, a dependency only changes through a pull request that edits `uv.lock`, so its breakage
shows up on that PR. `uv lock --check` fails a PR that edits a `pyproject.toml` without relocking.
The same step pattern runs every check even after an earlier one fails (`if: !cancelled()`), so
one run reports every problem.

### Supply-chain rules

Every workflow file follows these rules. `make lint-workflows` (actionlint + zizmor, the same pinned
versions CI uses) and `tests/unit/test_github_workflows_hardened.py` enforce them:

- **Actions are pinned to a full commit SHA**, with the version in a comment:
  `actions/checkout@<40-hex> # v7.0.1`. Tags can be moved: on 2026-03-19, 76 of 77
  `aquasecurity/trivy-action` tags were force-pushed to credential-stealing code
  ([GHSA-69fq-xp46-6x23](https://github.com/advisories/GHSA-69fq-xp46-6x23)).
- **`persist-credentials: false` on every checkout**, so no later step can reuse the token.
- **A read-only token by default.** Write scopes go only to the job that needs them, such as
  `pages: write` on the Pages deploy. `pull_request_target` is not allowed.
- **A `timeout-minutes` on every job.**
- Tools come from pinned PyPI releases run through `uvx`, or from checksummed downloads (helm is
  checked against the SHA-256 in its release notes). No extra third-party action is needed.

### Dependabot

`.github/dependabot.yml` covers the uv workspace, every pinned `requirements*.txt` (the services,
`serving/ray_serving` and the docs toolchain), the dashboard frontend, the workflow actions and
every Dockerfile's base image. Where a Dockerfile pins `image:tag@sha256:<digest>`, Dependabot
re-pins the digest when the tag is republished with patched layers.
Minor and patch updates arrive grouped, one PR per ecosystem. A major update arrives on its own
PR, so one breaking major cannot block the safe updates next to it. A release must be at least 7
days old before Dependabot proposes it (14 for a major); most malicious releases are found and
pulled within that time. Each `ignore` rule records why it exists and what would let it be
removed, and the guard test fails an ignore without that comment. The four ecosystems run on
different weekdays, so their pull requests don't all compete for runners at once.

mlflow, xgboost and ray are excluded from Dependabot on purpose. A model must be served with the
versions it was trained with. Training uses `uv.lock`, serving uses
`serving/ray_serving/requirements.txt`, and mlflow and ray are also pinned in the tracking-server
and notebook images. Ray Client also refuses a cluster of another Ray version. Dependabot updates
each manifest separately, which would split them. Upgrade them by hand, in every place in one
change. `tests/unit/test_training_serving_versions_agree.py` fails on any mismatch.

### Documentation site

`.github/workflows/pages.yml` rebuilds the site with the same strict build and deploys it to
GitHub Pages. It only runs in the public repository. Pages must be enabled once under
**Settings → Pages → Source: GitHub Actions**. Until then the deploy job fails with
`Failed to create deployment (status: 404)`; the build job still runs.

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
