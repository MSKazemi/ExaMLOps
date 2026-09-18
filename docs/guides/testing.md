# Testing strategy

The unit suite is **6411 tests**. In one process that is **20 minutes**; across this machine's
cores it is **3 minutes 37 seconds** — a 5.5× difference, and the reason every gate below runs in
parallel. Twenty minutes is long enough that the gate gets skipped, and a gate that gets skipped
is not a gate.

|  | wall clock |
|---|---|
| one process (`make test-serial`, `JOBS=0`) | 1205 s — 20 min 4 s |
| across this host's cores (`make test-fast`) | 217 s — 3 min 37 s |

Both are measurements taken on 2026-09-13 on a busy dev host, not estimates, and both move with
the suite. This page previously said "2781 tests in 66 seconds" — the suite had grown 2.3×
underneath it and nobody noticed, because a stale number reads exactly like a fresh one.
`tests/unit/test_documented_counts_are_current.py` now compares the documented figure with the
real one. **Re-measure rather than scale the old number**; a serial run is twenty minutes of
patience and the number is what this page's whole argument rests on.

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
file written inside the repo. Formatting breaks are the single most common cause of a red pipeline
here, and this closes the usual route to one.

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

`make watch` is deliberately serial and scoped: the whole tree on every save is what `test-fast`
is for, and xdist worker start-up would cost more than a scoped serial run. `make typecheck-fast`
is the same idea for types — a `dmypy` daemon over the same four roots as `make typecheck`, so
after a slow first run every re-check is seconds. Both are conveniences; `make gate` before
pushing is still the guarantee.

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
reached the parsed output. An autouse fixture in `tests/conftest.py` now gives every test a
private `PLATFORM_DB` before its body runs. Because it uses `monkeypatch.setenv`, it also undoes
any direct `os.environ` write made during the test — the leak is closed for the sloppy modules
without editing forty files.

**The rule this establishes: a test must not depend on state left by another test.** If a test
only passes serially, that is a bug in the test, not a reason to stop running in parallel.

**No test touches the checkout's own stores either.** The platform's other SQLite stores default
to paths in the working directory: `AGENT_MEMORY_DB`, `AGENT_DB`, `AGENT_MEMORY_REVIEW_DB` and
`MLFLOW_SQLITE_DB`, and MLflow 3 itself, which falls back to `sqlite:///mlflow.db` when
`MLFLOW_TRACKING_URI` is unset. The working directory is the repository root when the suite runs,
and on a dev host that's where the live stack keeps them. A backup test was found copying the
developer's real agent-memory and MLflow databases mid-write. That's a flake when they change
under it, and a unit test reading private state regardless.

Two things in `tests/conftest.py` close it: - `_isolate_sqlite_stores` points those variables at
paths under each test's `tmp_path`. - An **audit hook on `sqlite3.connect`** refuses any
connection to the checkout's `platform.db`, `mlflow.db` or agent stores, whoever makes it (product
code, MLflow through SQLAlchemy, a library), and records it, so the test fails even if the code
under test swallows the error.

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

### Running the suite on Postgres

`make test-postgres` starts a throwaway Postgres 16 and runs the unit suite, the dashboard suite
and the live-backend suite against it. It is parallel like the SQLite one, and the mechanism is
the engine's answer to "a private `PLATFORM_DB` per test":

| Scope | SQLite | Postgres |
|---|---|---|
| One test | its own `PLATFORM_DB` file | the shared schema, emptied before each test (`postgres_isolation`) |
| One xdist worker | nothing needed | **its own schema** — `exa_test` → `exa_test_gw3` (`scope_schema_to_this_worker`, called from `tests/conftest.py` at import, before anything connects) |
| One instance on a shared server | a different file | `EXAMLOPS_POSTGRES_SCHEMA` |

Until the worker scope existed this suite could only run single-process: eight workers sharing one
schema truncate each other's rows mid-test, which is badly flaky rather than slow. That cost ~9
minutes per measurement and the parity number was therefore checked by hand and rarely — the same
argument this guide makes for the SQLite suite. `JOBS=0` still forces serial when a failure is
suspected of being an isolation bug.

Two helpers exist for tests whose *precondition* is not portable, and reaching for the right one
is what keeps a parity failure meaningful:

- `empty_datastore()` — a datastore that exists and holds **no tables**, for the surfaces that
  must degrade honestly when their table is absent. On Postgres, a sibling schema that is
  deliberately never bootstrapped.
- `datastore_before_a_migration(tmp_path, monkeypatch, *tables)` — a datastore where just those
  tables are absent, so a test can build one in the shape an older release left it and let
  `init_db(force=True)` migrate it. A *different* sibling schema, because bootstrapping the one
  above would quietly turn every "the table is absent" test into a vacuous pass.

When a precondition genuinely cannot exist on an engine, **skip with the reason** (as the
`PLATFORM_DB`-seam half of `test_suite_stores_are_isolated` does: Postgres opens no such file, so
the accident it describes cannot happen there). A test that passes without asking its question is
the one failure mode a green suite cannot show you.

## Flaky tests

A flaky test is worse than a missing one: it teaches everyone to re-run the suite until it is
green, which is the same as having no suite. Fix them; do not add automatic reruns.

The one flake found when parallelism was introduced is a useful pattern. `coord_rate_allow` is a
fixed-window limiter comparing `window_start <= CURRENT_TIMESTAMP - <window> seconds`, and
SQLite's `CURRENT_TIMESTAMP` has whole-second resolution. With a **1-second** window, two calls
milliseconds apart that straddle a second tick are both read as starting a new window — so the
test failed about one run in five, in isolation. The fix asserts the denial over a 60-second
window, where a one-second tick cannot reach the boundary, and keeps the 1-second window only for
the *reset* assertion, which is the safe direction (a coarse clock can make a window look more
elapsed, never less).

## Guards that read the repository

A large family of tests here answers questions about the repository rather than about a function:
what the public tree leaks, whether every environment variable is documented, whether every ADR's
named artifacts exist, whether the Makefile tells the truth. They are the reason this project does
not use test-impact selection (they have no import edge to follow), and they have their own
failure mode: **a guard that scans nothing passes.**

Two rules follow, and both are enforced rather than remembered.

**Enumerate the tree through `tests/unit/_guard_deps.tracked_and_new_files()`.** It returns
tracked files plus untracked-but-eligible ones — so a guard fails on the change that introduces a
problem, not on some later one — and it drops paths that no longer exist on disk. `git ls-files
--cached` lists the *index*, which still holds a file deleted in the working tree whose deletion
is not staged (`D` in `git status`, and an ordinary state in a tree several people work in).
Guards that went on to read each path died with `FileNotFoundError` from inside `pathlib`, which
reads as a broken test rather than as "someone is removing a file". One unstaged deletion failed
five tests across three guards, and while those failures stood they **masked two genuinely
undocumented environment variables** — the cost of a guard that fails for the wrong reason is not
the noise, it is the findings nobody can see behind it.
`tests/unit/test_guard_file_enumeration.py` holds the contract.

**Scan a directory through `scan_files(root, pattern)` from the same module.** Most of these
guards assert a *negative* — no module imports the monolith, no SQL inlines a `LIKE` pattern, no
caller bypasses `/v1`, no alert threshold is unreachable — and a negative claim over an empty scan
is the strongest possible pass: zero offenders, green, indistinguishable from total compliance.
`test_platform_db_coupling_ratchet.py` was found in exactly that state, with its root pointed at a
directory that did not exist. `scan_files` asserts it matched something, so every caller inherits
the check instead of remembering it, and the failure names the root and the pattern.

Two guards stand behind it, covering different halves. `test_guard_paths_are_not_stale.py`
evaluates every module-level repo path in this directory and fails if one no longer resolves — the
*root*. `scan_files` catches the other half, the **pattern**: a root that still exists while
`*.py`, `*.md` or `docker-compose*.yml` quietly stops matching after a rename. Neither tries to
detect *whether* a module asserts its own non-emptiness; a detector of assertion style is itself
the kind of check that stops matching without telling anyone, which is why the assertion lives in
the shared scan instead.

**Never let the corpus contain the subject.** A guard checking that every metric an alert names is
one the code emits scanned `platform/` — which contains `alert_rules.yml` itself, so every name in
the rules matched *itself*, and the check passed for any string at all. It was vacuous from the
day it was written, and only a mutant with one letter changed exposed it. Scan the **emitters**
and never the declaration under test; then prove the narrowing was the fix by widening it again
and watching the mutant survive.

**And when a guard carries exemptions**, give it a companion test that each one still describes
something real: an exemption outlives its subject, and then it is just an open door. Several
guards here fail with exactly that message.

## Degradations must be distinguishable

The platform degrades rather than failing in many places, and that is right: a broken plugin must
not block a promotion, a serving replica must keep serving when the datastore is away. Three
defects in one week showed the cost of doing it *silently* — a governance digest that verified
nothing, a signer whose failure looked like a deliberate policy, and a provider fallback that
replaced a site's own promotion gate without a word. None of them was a crash, and none was
visible from the outside.

So a blanket `except Exception` inside a function whose docstring promises a degradation must
either **say something** — log it, or return the cause to the caller — or **carry its reason** on
the `except` line as `# noqa: BLE001 - <why>`. Being quiet is allowed when it is argued: two sites
on the serving request path stay silent precisely because a log line per inference would drown the
outage that caused it, and they say so.

The principle, its instances and how to find the next one: [Honest
degradation](honest-degradation.md).

`tests/unit/test_degradations_are_visible.py` is a **ratchet**: the count of silent sites may only
go down. It started at 8 and is at 4, each remaining one low-consequence and needing a judgement
rather than a sweep. A ratchet was chosen over a hard zero deliberately — a guard that demanded
all of them at once is a guard someone switches off.

## Markers

There is exactly one, and it is applied:

```bash
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

Roughly **10 minutes of testing becomes under 2**. The dashboard, agent and control-plane suites
are I/O-bound rather than CPU-bound, so they gain 2–3× where the root suite gains 7.9× — worth
having, and none of them needed an isolation fix to get there.

`make ci-examlops` runs the root suite parallel too. That is not a performance choice: the
target's whole job is to *mirror* the GitHub `examlops` job, and a mirror that runs the suite
differently from CI is the failure it exists to prevent.

## Live verification

Some claims the unit suite cannot reach: what Prometheus does with a DNS-discovered target whose
container stops, what Ray actually exports, whether the per-service Postgres roles really refuse a
write. The `tests/integration/*_live.py` files prove those against the real thing, and each is
gated on an environment variable so it skips in the ordinary run.

**Every gated test file has a `make` target**, and `tests/unit/test_live_tests_are_runnable.py`
keeps it that way. The reason is worth stating: on 2026-09-15, **11 of 17 gates had no runner at
all** — no target, no CI job, nothing but a command in the test's own docstring. A test nobody can
run is documentation, not verification: it never executes, so the claim it makes is unchecked and
its rot is undetectable. That was found while *relying* on one of them to settle a question about
Prometheus's DNS discovery.

The guard keys on the **property** — a test file that reads an environment variable with no default
— rather than on a naming convention. Its first version looked for `tests/integration/*_live.py`
and gates spelled `*LIVE*`, passed at zero, and missed three more: the pgvector parity suite lives
in `tests/unit/`, and `EXAMLOPS_NATS_TEST_URL` and `EXAMLOPS_REDIS_TEST_URL` are not spelled
`LIVE`. Every one of those targets passed the first time it was run, so nothing had rotted — but
nothing had been checking.

| Target | Gate | What it proves against the real thing |
|---|---|---|
| `make pgvector-live` | `EXAMLOPS_PGVECTOR_TEST_DSN` | pgvector returns the **same ranking** as the SQLite fallback for every metric, filtered and unfiltered, dense/sparse/hybrid — a backend that answered differently would make the fallback a lie about production. Starts its own Postgres |
| `make nats-live` | `EXAMLOPS_NATS_TEST_URL` | The event backbone against a real JetStream. Starts its own broker |
| `make redis-live` | `EXAMLOPS_REDIS_TEST_URL` | Cross-replica coordination against a real Redis. Starts its own server |
| `make prometheus-live` | `EXAMLOPS_PROMETHEUS_LIVE` | An opt-in service is scraped once it runs, raises no alert when it was never deployed, and — the part the alerts depend on — **stays a target with `up == 0` after its container stops**, through several DNS refreshes |
| `make ray-live` | `EXAMLOPS_RAY_LIVE` | Ray's real metric export (a gauge set once disappears from the scrape, which is why the replica republishes) and router behaviour when a replica is lost |
| `make postgres-roles-live` | `EXAMLOPS_POSTGRES_ROLES_LIVE` | The per-service roles refuse the writes they are supposed to refuse |
| `make spire-live` | `EXAMLOPS_SPIRE_LIVE` | SPIFFE attestation under Compose, workload identity, and model-server mTLS. **Allow ~4 minutes**: the attestation test builds and brings up the whole identity overlay before it can assert anything |
| `make iam-live` | `EXAMLOPS_IAM_LIVE_KEYCLOAK_URL` &c. | Federated login against a real Keycloak and a real OPA (`platform/infra/iam/` brings up a reference pair) |
| `make lineage-live` | `EXAMLOPS_LINEAGE_LIVE_MARQUEZ_URL` | OpenLineage events reach a real Marquez and come back conformant |
| `make helm-kind-live` | `EXAMLOPS_KIND_GATEWAY_LIVE`, `EXAMLOPS_KIND_SPIRE_LIVE` | The chart's gateway and workload identity on a kind cluster (~3 min). **Builds the control-plane image first** — the fixture skips only when the image is *absent*, so without that step a stale one is silently tested |
| `make chaos-drills`, `make chaos-drills-kind` | `EXAMLOPS_CHAOS_LIVE`, `EXAMLOPS_KIND_*` | The drills below |

None of these run in CI — they need Docker, a cluster, or a broker. The target is what makes them
runnable and discoverable; the guard is what stops the next one from being added without one.

**A target is only proven by running it.** `iam-live` and `lineage-live` were written to print a
sentence naming the variables to set; both instead died with bash's `unbound variable`, because this
Makefile runs recipes under `-u` and a bare `$$VAR` aborts the presence test before it can decide.
Expanding the recipe with `make -n` would never have shown it —
`tests/unit/test_makefile_presence_tests_survive_nounset.py` now catches that shape statically.

## Chaos drills

Opt-in drills that break a real dependency and hold the platform to what the docs promise. Each
needs Docker (some need kind), each is skipped unless its variable is set, and none is part of
`make test-fast`.

| Drill | Turn it on | What it breaks, and what must hold |
|---|---|---|
| [Datastore outage](postgres-backend.md#when-the-datastore-goes-away) `tests/integration/test_datastore_outage_drill_live.py` | `EXAMLOPS_CHAOS_LIVE=1` | Kills Postgres under the control plane and the gateway's authorization service. Readiness turns 503 in about 2 s and never hangs, writes are refused cleanly, a verified virtual key keeps working from cache while an unseen one is refused, and everything recovers with no restart |
| Event backbone `tests/integration/test_backbone_outage_drill_live.py` | `EXAMLOPS_CHAOS_LIVE=1` | Kills NATS under the control plane, then the datastore as well, and brings them back one at a time. Retrains are still accepted while only the bus is gone, the replica stays in rotation, the failure shows in `/health` within about ten seconds without turning the backlog into poison, and the outbox drains exactly once when the broker returns |
| Control-plane failover `tests/integration/test_control_plane_failover_kind_live.py` | `EXAMLOPS_KIND_FAILOVER_LIVE=1` | Kills replicas mid-dispatch under load in kind. Nothing accepted is lost, no retrain runs twice, and a crashed replica's claim is taken over |
| Serving overload `tests/integration/test_serving_overload_drill_live.py` | `EXAMLOPS_CHAOS_LIVE=1` | Loads one replica ten times past its capacity, with the queue bounded and unbounded. Every answer is a prediction or a shed 503, latency stays bounded only with the bound, a spent budget is refused without running the model, and the server recovers |
| [Restore a Postgres backup](backup-restore.md#can-you-actually-restore-it) `tests/integration/test_postgres_dr_roundtrip_live.py` | `EXAMLOPS_CHAOS_LIVE=1` | Seeds a chained audit log and a traffic split on a real Postgres, takes a bundle, **drops the schema**, and restores. Every row must come back and the chain's head hash must be the one from before — a restore that rewrote the log would otherwise pass. Needs `pg_dump`/`pg_restore`; skips without them |
| [Restore the artifacts](backup-restore.md#can-you-actually-restore-it) `tests/integration/test_objects_dr_roundtrip_live.py` | `EXAMLOPS_CHAOS_LIVE=1` | Puts model artifacts in a real MinIO, backs them up, **destroys the bucket**, restores, and compares **digests** — counting objects would pass over a restore of the right number of wrong files. Also lists past a 1000-key page, which the in-memory double never truncates |
| [The backup sidecar](backup-restore.md#does-the-container-behind-the-rpo-run) `tests/integration/test_backup_sidecar_live.py` | `EXAMLOPS_CHAOS_LIVE=1` | Builds the shipped sidecar image, runs **one real cycle**, and reads what the container wrote: the manifest must show the platform datastore captured, the bundle must verify with the host's own checksums, and the database inside it must still carry its audit chain |
| Serving on Kubernetes `tests/integration/test_serving_kind_drill_live.py` | `EXAMLOPS_KIND_SERVING_LIVE=1` | Deletes, upgrades, kills and adds model-server pods in kind, under load, with and without the recommended pod settings. Churn costs only the connections pinned to a dying pod and one retry erases it; a pod with no readiness probe is sent inference before its model is loaded (2069 requests in a 45 s run) |
| Control-plane partition `tests/integration/test_control_plane_partition_kind_live.py` | `EXAMLOPS_KIND_PARTITION_LIVE=1` | Holds one replica's dispatch to Prefect so it is alive but cut off. The command must reach a terminal state, and however many dispatches are in flight (10 here) Prefect must start exactly one run. Found that the cut-off replica keeps the command until its attempts are spent, and that the late dispatch leaves a run the platform records as dead |
| Serving node loss `tests/integration/test_serving_node_loss_kind_live.py` | `EXAMLOPS_KIND_NODE_LOSS_LIVE=1` | Drains and then hard-stops a worker in a three-node kind cluster under load. A drain costs an eviction; a lost node black-holes its share of inference until Kubernetes evicts the pod (minutes, by default), with each request hanging for the caller's timeout. A shorter `unreachable` toleration does **not** shorten it (132–139 s with it, 133–136 s without): what ends the black hole is EndpointSlice removal, not the pod's deletion. The gateway is what turns the same loss into a blip |
| Serving replica loss `tests/integration/test_serving_replica_failover_live.py` | `EXAMLOPS_RAY_LIVE=1` | Kills a model-server replica mid-inference. The caller sees the documented failure cause, and the retry budget bounds the retries |
| Gateway ejection `tests/integration/test_serving_gateway_ejection_live.py` | `EXAMLOPS_GATEWAY_LIVE=1` | Wedges one of two model-server endpoints so it accepts connections and never answers. The gateway must stop choosing it within about ten seconds (10–13 of 20 requests timed out without its health checks, none with them; ejected after 8.4 s, back in use 11.5 s after it recovered), put it back when it recovers, and never eject a lone endpoint |
| Authorization outage `tests/integration/test_serving_gateway_live.py` (last test) | `EXAMLOPS_GATEWAY_LIVE=1` | Stops the gateway's authorization service. Requests fail closed with 503 and the alert's counter moves |

Run one with `-s` to see its measured timings, which is what the guides quote, or all of them with
`make chaos-drills`. [Game days](game-days.md) covers what each drill proves, what its numbers
should look like, and how to run the same failures against your own installation.

The Docker set also runs **weekly** in `.github/workflows/chaos-drills.yml`, keeping each run's
log as a 90-day artifact; the cluster set is opt-in from the Actions tab. It is a report rather
than a required check, for the reason given in [game
days](game-days.md#they-also-run-every-week-on-their-own): these measure timing on a shared
runner, and a gate that reddens for a slow neighbour trains people to re-run it.

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

### The same copy the readers run

That pinned package is also what the site *serves*. Material's bundle loads the renderer from
`https://unpkg.com/mermaid@11/dist/mermaid.min.js` unless `mermaid` is already defined — a floating
major, from a third party, executing in every reader's browser, with Subresource Integrity
impossible against a moving tag. `docs/overrides/docs_assets_hook.py` adds the pinned file to the
build and `mkdocs.yml` loads it first, so Material never reaches for the network, and the version a
reader runs is by construction the version CI parsed.

A missing `node_modules` **fails the build**, for the same reason exit 2 is not success: building
anyway would 404 the asset, leave `mermaid` undefined and quietly restore the CDN fetch on the
published site.

Measured rather than assumed, in a headless browser against real builds — which is what
`tests/integration/test_docs_site_is_self_contained.py` keeps (opt-in; it needs a browser):

| build | requests to unpkg.com | diagrams |
|---|---|---|
| before | 2 — the floating tag, then its redirect | render |
| with the hook | 0 | render, identically |

The same probe rejected the obvious alternative. Material's `privacy` plugin does download the file
at build time, but rewrites this particular reference to an **absolute** `site_url` address, because
the URL lives inside a JavaScript bundle rather than in HTML or CSS. Diagrams then render on the
production origin and degrade to raw text on a local build, a preview deploy or the github.io
domain — two of them did exactly that in the probe.

### Nobody else watches the reader

That plugin *is* the right tool for the assets whose references live in HTML and CSS, and it is now
enabled for them. Every page used to fetch the site's typeface from `fonts.googleapis.com` and
`fonts.gstatic.com` — six requests carrying the reader's IP address and the page they were reading
to a third party, which for the public documentation of a European research project is a
data-protection question before it is a supply-chain one. The fonts are now served from this site;
the typography is unchanged.

Its one exclusion is deliberate and says why beside itself in `mkdocs.yml`, because an exclusion
that outlives its reason is just a hole: **mermaid** is served by the hook above, so localising it
again would ship a second, unused 3.5 MB copy behind an absolute URL.

**KaTeX** used to be a second exclusion, and the story is worth keeping. Letting the plugin localise
it produced a build that looked finished from every angle a cheap check can see — stylesheet and
script served locally, zero external requests, formulae still on the page — while all ~60
`fonts/KaTeX_*.woff2` 404'd, because that stylesheet names them with *relative* urls the plugin does
not follow, and the maths rendered in a fallback face. It is now served whole by the same hook, with
its directory layout intact so those urls resolve, and the exclusion is gone rather than left behind
as dead configuration.

That failure also decided how the browser test asks the question.
`getComputedStyle(...).fontFamily` reports the family the CSS *declares* — `KaTeX_Math` in the
working build and in the broken one alike, an assertion that cannot fail. `document.fonts.check()`
separates them: true with five KaTeX faces loaded, false with none.

`tests/unit/test_docs_assets_are_local.py` holds the configuration (the hook registered, nothing
loaded from an `http(s)://` URL, mermaid first, a missing package failing the build) and
`tests/unit/test_docs_privacy_plugin.py` holds the plugin on with every third-party URL explained.
The browser test asserts what a reader's browser actually does: the only host any page still
reaches is `api.github.com`, Material's own repository widget.

## Is the published site whole?

`mkdocs build --strict` checks the links *between* pages. Nothing checked what a page then asks the
browser for — an image that moved, a stylesheet a theme upgrade renamed, a script that throws, a
diagram the renderer refused, a formula left as TeX source. All of those are invisible to the build
and plainly visible to a reader, and this repository has been bitten once already: two invalid
diagrams were published for months before anyone opened the page.

`tests/integration/test_docs_site_pages_are_whole.py` loads **every** page (145 of them) in a
browser and reports, per page, any response of 400 or worse, any console error, any unrendered
diagram, any visible TeX source, and any empty `href`. It is opt-in and takes about three and a
half minutes, so it is not part of any gate — it is what you run to answer "is the site whole".

The first run reported nothing, and **a sweep that finds nothing is a claim about the detector**,
so each of the five checks was demonstrated against a deliberately broken build first: a KaTeX
stylesheet served without its fonts, an unparseable diagram, a missing image, KaTeX's scripts
removed, and an injected `<a href="">`. All five fired. Only then was "145 pages, no findings"
worth reporting.

## CI

The GitHub `examlops` job and the GitLab `test:examlops` job both run the unit suite with `-n
auto`. The remaining jobs (agent, control-plane, dashboard, Helm, docs, Postgres) are unchanged.
See `docs/reference/cli-generated.md` for the command surface and `.gitlab-ci.yml` for the gate
wiring — the `needs:` list on `deploy:lxp` **is** the gate, so a new blocking job must be added
there too.
