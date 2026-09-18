# Testing strategy

The unit suite is **2781 tests**. Run in one process that is about **nine minutes** — long enough
that the gate gets skipped, and a gate that gets skipped is not a gate. Run across this machine's
cores it is **66 seconds**, so the whole suite is affordable on every change.

That single fact shapes everything below: ExaMLOps does **not** use test-impact selection. Picking
"just the affected tests" would save perhaps another minute and would silently miss the large
family of guard tests in this repo that *read files* rather than importing them — a change to
`Makefile`, `.gitlab-ci.yml` or an ADR has no import edge for a coverage-based selector to follow.
Running everything, quickly, is both simpler and safer.

## The tiers

| Tier | When | Command | Time | What it covers |
|---|---|---|---|---|
| 0 | on every file edit | *(automatic)* | <1s | `ruff check --fix` + `ruff format` on the edited file |
| 0.5 | while working an area | `make watch W=tests/unit/test_x.py` | seconds/save | the scoped tests, re-run failed-first on every save |
| 1 | inner loop | `make test-fast` | ~70s | whole unit suite, parallel |
| 2 | before pushing | `make gate` | ~2min | lint · format · typecheck · unit · docs |
| 3 | before a release | `make preflight` | ~10min | full CI mirror: integration, dashboard, Postgres, compose, Helm |
| 4 | CI | *(automatic)* | — | every tier-3 job, plus live-service jobs |

Tier 0 is a Claude Code `PostToolUse` hook in `.claude/settings.json`; it runs ruff on any `.py`
file written inside the repo. Formatting breaks are the single most common cause of a red
pipeline here, and this closes the usual route to one.

**It does not catch everything.** The hook fires on the editor's write, so a file created another
way — a shell heredoc, `sed -i`, a generator — is not formatted by it. That is why tier 2 still
runs `ruff format --check` rather than trusting tier 0: the fast hook is a convenience, and the
gate is the guarantee.

Tier 2 is also installed as a git `pre-push` gate — see *Installing the hooks* below.

## Running tests

```bash
make test-fast                 # the one to use — whole unit suite, parallel, quiet
make test-fast JOBS=4          # cap the workers on a loaded laptop
make watch W=tests/unit/test_x.py  # tier 0.5 — keep it running; every save re-runs the scope
make test-failed               # re-run last run's failures first, then the rest
make test-slowest              # the 30 slowest tests
make test-serial               # single-process; use to confirm a parallel-only failure
pytest tests/unit/test_x.py    # one file — no `-n`, so there is no worker start-up cost
```

`make watch` is deliberately serial and scoped: the whole tree on every save is what
`test-fast` is for, and xdist worker start-up would cost more than a scoped serial run.
`make typecheck-fast` is the same idea for types — a `dmypy` daemon over the same four
roots as `make typecheck`, so after a slow first run every re-check is seconds. Both are
conveniences; `make gate` before pushing is still the guarantee.

`-n auto` is deliberately **not** in `pyproject.toml`'s `addopts`. Putting it there would make a
single-file run pay worker start-up for nothing; it belongs on the targets that run the whole
suite.

## Why parallel is safe here

It was not, until the isolation leak was closed. About forty test modules set
`os.environ["PLATFORM_DB"] = str(tmp_path / …)` **directly** instead of through `monkeypatch`, so
the value outlived the test that set it. Serially this is invisible — the directory still exists
and the stale database is simply unused. Under `-n auto` it produced four failures that every one
of those tests passes on its own: a `--json` command whose output would not parse, because
`warning: platform datastore unavailable` had been printed ahead of the JSON.

The warning itself was correct and went to stderr; `CliRunner` merges the streams, which is why it
reached the parsed output. An autouse fixture in `tests/conftest.py` now gives every test a private
`PLATFORM_DB` before its body runs. Because it uses `monkeypatch.setenv`, it also undoes any direct
`os.environ` write made during the test — the leak is closed for the sloppy modules without editing
forty files.

**The rule this establishes: a test must not depend on state left by another test.** If a test only
passes serially, that is a bug in the test, not a reason to stop running in parallel.

**No test touches the checkout's own stores either.** The platform's other SQLite stores default to
paths in the working directory: `AGENT_MEMORY_DB`, `AGENT_DB`, `AGENT_MEMORY_REVIEW_DB` and
`MLFLOW_SQLITE_DB`, and MLflow 3 itself, which falls back to `sqlite:///mlflow.db` when
`MLFLOW_TRACKING_URI` is unset. The working directory is the repository root when the suite runs,
and on a dev host that's where the live stack keeps them. A backup test was found copying the
developer's real agent-memory and MLflow databases mid-write. That's a flake when they change under
it, and a unit test reading private state regardless.

Two things in `tests/conftest.py` close it:
- `_isolate_sqlite_stores` points those variables at paths under each test's `tmp_path`.
- An **audit hook on `sqlite3.connect`** refuses any connection to the checkout's `platform.db`,
  `mlflow.db` or agent stores, whoever makes it (product code, MLflow through SQLAlchemy, a
  library), and records it, so the test fails even if the code under test swallows the error.

If a test trips it, state what the test needs at the seam (as `test_rollback_registry` does with
`_alias_version`) or point the store at `tmp_path`.

**And no test leaves anything behind in it.** `_no_trace_in_the_checkout` compares the repository
root before and after every test and fails the one that added an entry. Four did when the guard was
written: `mlruns/` (MLflow artifacts), `slurm_jobs/` and `flux_jobs/` (scheduler working
directories, created by merely *constructing* an adapter) and `.pytest_cache` (from the `exa`
command that runs pytest). All four were invisible because a **machine-local** `.git/info/exclude`
hid them — no repository carries that file, so on a fresh clone a whole-tree `add` would publish
local run data and generated job scripts, which carry absolute local paths.

The suite therefore points `EXAMLOPS_HPC_WORKDIR` at a temporary directory, and the guard keeps it
that way. SQLite sidecars (`-wal`, `-shm`, `-journal`) of a file that was *already* there are
ignored: on a dev host the live stack writes to the checkout's own `platform.db` while the suite
runs, and blaming whichever test happened to be running would make the guard fail at random.

### What is deliberately *not* parallel

`test:postgres` in CI runs single-process on purpose. Its workers would share one schema that the
isolation fixture truncates between tests, so parallelising it would be genuinely, badly flaky.
`EXAMLOPS_POSTGRES_SCHEMA` isolates one *instance*, not one worker.

## Flaky tests

A flaky test is worse than a missing one: it teaches everyone to re-run the suite until it is
green, which is the same as having no suite. Fix them; do not add automatic reruns.

The one flake found when parallelism was introduced is a useful pattern. `coord_rate_allow` is a
fixed-window limiter comparing `window_start <= CURRENT_TIMESTAMP - <window> seconds`, and SQLite's
`CURRENT_TIMESTAMP` has whole-second resolution. With a **1-second** window, two calls milliseconds
apart that straddle a second tick are both read as starting a new window — so the test failed about
one run in five, in isolation. The fix asserts the denial over a 60-second window, where a
one-second tick cannot reach the boundary, and keeps the 1-second window only for the *reset*
assertion, which is the safe direction (a coarse clock can make a window look more elapsed, never
less).

## Markers

There is exactly one, and it is applied:

```python
pytestmark = pytest.mark.live   # tests/integration/test_postgres_backend_live.py, …_redis_…
```

```bash
pytest tests/integration -m live        # 19 tests
pytest tests/integration -m "not live"  # 5 tests
```

`live` tests also skip *themselves* when their opt-in variable (`EXAMLOPS_POSTGRES_TEST_DSN`,
`EXAMLOPS_REDIS_TEST_URL`) is unset, so the suite stays runnable on a laptop with nothing running.
The marker is what makes them **selectable**; the env check is what makes them **safe**.

Two rules keep the marker list honest, because a marker is a claim about the suite:

- `--strict-markers` is in `addopts`. An unregistered marker is an error, not a silent no-op —
  without it, `@pytest.mark.slwo` does nothing and the test everyone believes is tagged is not.
- A guard (`tests/unit/test_every_test_can_fail.py`) fails if a marker is registered in
  `pyproject.toml` but applied to no test. A `slow` marker was briefly registered here, described
  as being excluded from `make test-fast`, applied to nothing, and excluded by nothing. It was
  **removed rather than retro-fitted**: at 66s for the whole suite there is no reason to skip
  anything from the inner loop, and `make test-slowest` measures real durations, which cannot rot
  the way a hand-applied label does. An empty category is worse than no category — `-m slow` would
  have selected nothing and reported success.

## Installing the hooks

```bash
make install-hooks
```

This installs the tier-2 gate and **never overwrites an existing `pre-push`**. On a machine where
that filename already belongs to another guard — the AI-attribution guard is one — the CI gate is
installed beside it as `pre-push-ci` and the command prints the one line that chains them:

```bash
exec "$(git rev-parse --git-dir)/hooks/pre-push-ci" "$@"
```

Both git directories are handled (`.git` and `.git-private`). To push without the test half when
you already know the suite is green:

```bash
PREPUSH_SKIP_TESTS=1 git push
```

The gate never silently passes: if `.venv/bin/pytest` is missing it says the tests did **not** run
rather than printing nothing, because absence of output must not read as success.

## The other suites

The root unit suite is the big one, but it is not the whole estate. All four Python suites now run
parallel, each verified green before the flag was applied:

| Suite | Target | Serial | Parallel |
|---|---|---|---|
| Root unit (2781) | `make test-fast` | 520s | **66s** |
| Dashboard backend (486) | `make dashboard-check-backend` | 35s | **12s** |
| Skipper agent (337) | `make skipper-test` | 36s | **17s** |
| Control plane (130) | `make ci-control-plane` | 21s | **13s** |

Roughly **10 minutes of testing becomes under 2**. The dashboard, agent and control-plane suites are
I/O-bound rather than CPU-bound, so they gain 2–3× where the root suite gains 7.9× — worth having,
and none of them needed an isolation fix to get there.

`make ci-examlops` runs the root suite parallel too. That is not a performance choice: the target's
whole job is to *mirror* the GitHub `examlops` job, and a mirror that runs the suite differently
from CI is the failure it exists to prevent.

## Documentation diagrams are checked by the renderer, not by a pattern

`mkdocs build --strict` never reads a ` ```mermaid ` fence — the reader's browser renders it — so
the build passes a diagram mermaid cannot parse and the reader gets a red error box. Eleven
diagrams sat unchecked for months and two of them were broken.

```bash
make docs-mermaid        # every diagram in docs/, through mermaid's own grammar
```

`platform/ci/check_mermaid.py` extracts the fences and hands them to `mermaid.parse` in
`platform/ci/mermaid/` — pinned to the major the site loads, which
`tests/unit/test_docs_mermaid.py` asserts is still the one Material fetches, so a Material upgrade
says to move the pin instead of quietly checking a grammar no reader runs. `make docs-build` calls
it, and CI's docs job runs it directly.

**Exit 2 — "the checker could not run" — is not success.** Locally that prints a red notice and
lets the build finish (a contributor without node still gets docs); in CI it fails the step,
because a check that passes because nothing ran is worse than no check.

The reason this uses the library rather than a pattern of its own is worth keeping: mermaid's
grammar is large, and an approximation fails in both directions. The defect that motivated it is a
good example — a `;` inside an unquoted label is a *statement separator*, so the label ends there
and the rest is a syntax error, while the same `;` inside a **quoted** label is ordinary text and
renders fine. Both shapes exist in this repository's diagrams, and only the real parser tells them
apart. The guard tests both directions on purpose.

## CI

The GitHub `examlops` job and the GitLab `test:examlops` job both run the unit suite with
`-n auto`. The remaining jobs (agent, control-plane, dashboard, Helm, docs, Postgres) are unchanged.
See `docs/reference/cli-generated.md` for the command surface and `.gitlab-ci.yml` for the gate
wiring — the `needs:` list on `deploy:lxp` **is** the gate, so a new blocking job must be added
there too.
