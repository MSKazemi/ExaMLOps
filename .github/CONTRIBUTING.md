# Contributing to ExaMLOps

Thank you for taking the time to look. ExaMLOps is an open-source MLOps platform for HPC:
it trains models as cluster jobs (Slurm or Flux), versions them in MLflow, gates production
behind a human approval, serves them with Ray Serve and watches drift, cost and carbon
afterwards. Contributions of every size are welcome, and **you do not need a supercomputer
to contribute** — the whole unit suite runs on a laptop with the scheduler in `mock` mode.

- **Docs site:** <https://mskazemi.github.io/ExaMLOps/>
- **Starter tasks:** [`good first issue`](https://github.com/MSKazemi/ExaMLOps/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
  · [`help wanted`](https://github.com/MSKazemi/ExaMLOps/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22)
- **Code of conduct:** [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · **Security reports:** [SECURITY.md](SECURITY.md) (never in a public issue)
- **Where to ask:** [SUPPORT.md](SUPPORT.md)

## Ways to contribute

Code is one way among several, and all of them count.

| You have… | A good contribution |
|---|---|
| 10 minutes | Fix a typo or a stale command in a guide — every page has an ✏️ *edit* button |
| an hour | Pick a [`good first issue`](https://github.com/MSKazemi/ExaMLOps/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22), add a missing test, improve an error message |
| used it for real | Open an issue describing your cluster, what worked and what didn't — field reports shape the roadmap |
| a model or dataset | Package it as a **use-case pack** (see below) — no platform change needed |
| a formula you trust | Ship a **provider plugin** for carbon, cost, drift or promotion — no fork needed |
| a missing command | Ship an **`exa` CLI plugin** as its own package |
| reviewer instincts | Review open pull requests; a careful review is as valuable as a patch |

### Extend without forking

ExaMLOps is built so most extensions live **outside** this repository:

- **Use-case packs** — a directory with `pack.toml`, `models/*.yaml`, `model_configs/*.py`
  and `datasets/schemas.json`, selected with `EXAMLOPS_USECASE_DIR`. The platform core never
  names a concrete model; see [Add a new model](https://mskazemi.github.io/ExaMLOps/guides/add-a-new-model/).
- **Calculation providers** — Python packages registered under the
  `exa.providers.<domain>` entry-point group (for example `exa.providers.carbon`).
  A complete example lives in [`examples/exa-carbon-plugin/`](../examples/exa-carbon-plugin/);
  the design is in [Programmable MLOps](https://mskazemi.github.io/ExaMLOps/guides/programmable-mlops/)
  and [FinOps providers](https://mskazemi.github.io/ExaMLOps/guides/finops-providers/).
- **CLI plugins** — third-party `exa` sub-commands registered under the
  `examlops.cli_plugins` entry-point group; `exa plugins` lists what loaded.

If you build one of these, open an issue or a pull request adding it to the docs — we will
link to it.

## Development setup

Requirements: **Python ≥ 3.12**, [`uv`](https://docs.astral.sh/uv/), `make`, `git`.
Docker is only needed for the full local stack (MLflow, Prefect, Ray Serve, dashboard), not
for the unit tests.

```bash
git clone https://github.com/MSKazemi/ExaMLOps.git
cd ExaMLOps
make install-dev          # creates .venv with uv and installs the exa CLI + dev tools
source .venv/bin/activate
exa --help                # the platform CLI, grouped into 12 lifecycle panels
make test-fast            # the whole unit suite in parallel (~70 s)
```

Optional:

```bash
make stack-up             # full local stack in Docker Compose (see the Quick Start guide)
make docs-serve           # the docs site at http://localhost:8080 with hot reload
cd platform/services/dashboard/frontend && npm install && npm test   # dashboard frontend
```

The upstream model library `modelzoo/` is **not** part of this repository. Tests that need
it skip cleanly when it is absent, so a fresh clone is fully testable.

Deeper orientation: [Developer onboarding](https://mskazemi.github.io/ExaMLOps/guides/developer-onboarding/)
(what every directory is for) and [Testing strategy](https://mskazemi.github.io/ExaMLOps/guides/testing/).

## Making a change

1. **Open or claim an issue first** for anything bigger than a small fix, so nobody
   duplicates work and we can agree on the approach before you write it. Comment
   "I'd like to take this" on an issue and it's yours.
2. **Fork and branch** from `main`: `git checkout -b fix/serve-empty-registry`.
3. **Write the test with the change.** Behaviour changes need unit coverage in `tests/unit/`;
   integration tests are for live-service boundaries only.
4. **Run the gates locally:**

   | Command | What it runs | When |
   |---|---|---|
   | `make test-fast` | whole unit suite, parallel (~70 s) | while you work |
   | `make lint` / `make lint-fix` | Ruff (`E,F,W,I,UP`, line length 100) | before committing |
   | `make gate` | lint · format · typecheck · unit · strict docs build (~2 min) | before opening the PR |
   | `.venv/bin/pytest tests/unit/test_x.py -v` | one file | debugging |

   A test that only passes when run serially is a bug in the test: each test gets its own
   private `PLATFORM_DB`.
5. **Update the docs you touched.** A new or changed `exa` command needs its `--help`
   text, an example, and the command guide updated; `make docs-explore` regenerates the
   generated capability pages. The docs build is `--strict`, so a broken link fails CI.
6. **Open the pull request** and fill in the template. CI runs lint, unit tests, the agent
   and dashboard suites, the Helm chart and the docs build.

### Rules that CI enforces (so you don't trip on them)

- **Platform ⟂ use-case boundary.** `platform/` and `pipelines/` must never name a concrete
  model or dataset, and never `import seanergys_modelzoo`. Content is reached through the
  loader seam (`pipelines.usecase` / `examlops.usecase`). Guard: `tests/unit/test_usecase_boundary.py`.
- **`platform` shadows the standard-library module.** Never `import platform.clients`;
  put `<repo>/platform` on `sys.path` and `import clients.X`.
- **Registry integrity.** New models are created with `exa scaffold`; a half-applied
  scaffold fails `tests/unit/test_registry_integrity.py`.
- **No bare `sqlite3.connect`.** Platform state goes through `examlops.resilience.db`
  (WAL + busy timeout); a guard test bans the bare call.
- **Every `exa` command has a dashboard tier.** New commands must be classified in
  `examlops.cli.surface`; `tests/unit/test_cli_surface.py` fails on an unclassified one.
- **Additive database changes only.** New tables and columns, no destructive migrations.

## Commit messages

Scoped [Conventional Commits](https://www.conventionalcommits.org/):

```
fix(serving): handle an empty registry
feat(cli): add --since to exa audit
docs(guides): correct the drift baseline example
test(guards): cover stale paths
```

Keep commits focused — one logical change each. Squash fix-ups before review if you can;
if you can't, we will squash on merge.

## Using AI assistants

You may use AI coding assistants. You remain the author: you must understand every line you
submit, have run the gates yourself, and be able to answer review questions about it.
If a substantial part of a pull request was generated, say so in the PR description
(one line is enough) — please do **not** add AI tools as commit co-authors. Pull requests
that are clearly unreviewed generated output, or that do not run, will be closed with a
short note.

## Review and response times

We aim to give every new issue and pull request a **first human response within 3 working
days** (maintainers are in the Central European time zone). A first response may be a
question rather than a verdict. If a week passes without one, please ping the thread —
that is a bug in our process, not an imposition.

A pull request is merged when CI is green, the change has tests and docs, and a maintainer
has approved it. Changes touching authentication, cryptography, secrets, the audit hash
chain, database migrations or public API compatibility get an extra-careful review.

## Licensing of contributions

ExaMLOps is licensed under the [Apache License 2.0](../LICENSE). By submitting a
contribution you agree that it is licensed under the same terms (Apache-2.0 §5); you keep
the copyright to your work. Do not submit code you do not have the right to license this way.

## Recognition

Every merged contribution is credited in the [CHANGELOG](../CHANGELOG.md) and the release
notes, by name or handle as you prefer. Sustained contributors are invited to become
reviewers and maintainers — see [GOVERNANCE.md](GOVERNANCE.md).
