# CLI Console — every `exa` command in the dashboard

The **CLI Console** (**Platform → CLI Console**, `/platform/cli`) makes every `exa` command
runnable from the dashboard. It lists the live command tree, generates a form for each
command's parameters, and runs the real CLI on the dashboard host, the same code the terminal
runs. The purpose-built consoles (Drift, Projects, FinOps …) stay the fastest way through their
workflows. This console guarantees that nothing the CLI can do is missing from the dashboard.
The design is recorded in ADR 0119.

To *manage* the things these commands act on (tables with New, View, Edit and Delete per item),
use [Resources](dashboard-resources.md), which is built on this console.

## What you see

- **Coverage line**: how many commands exist, how many are runnable here, and how many fall in
  each tier (below).
- **Command list** (left) grouped by the same 12 lifecycle panels as `exa --help`, with a search
  box. Opening the console from another console's `exa …` pill (bottom-right of the page)
  pre-filters the list to that console's commands, and **⌘K** finds any command by name.
- **Command view** (right): the command's help, its tier, a link to the console that also covers
  it, a form with one field per parameter (text, number, choice, checkbox, or one-per-line for
  repeatable options), the **equivalent terminal command** with a Copy button, and the
  command's own examples.
- **Output / History / Workspace** tabs under the form. JSON output renders as a table (a list of
  records) or a key/value view (one record), and you can toggle the raw output. Text output shows
  exactly what the terminal would print. Runs that write files list them for download.
- **While a command runs** the Output tab shows what it has printed so far, and follows new lines
  unless you scroll up to read. A JSON run's result is one document the CLI writes when it
  finishes, so until then you see only the messages it prints (its stderr). Output stays visible
  even when your next poll is answered by a different dashboard replica; it lags there by about
  two seconds.
- **Run again**: a finished run's output has a **Run again** button, and every entry in
  **History** opens that output. It loads the run's arguments and output format back into the
  command's form for you to review. It never runs anything by itself, so a destructive command
  still needs its confirmation typed. Secret values are never stored, only a mask, so the form asks
  for them again.

## Who can run what — tiers

Every command has exactly one tier, decided in the platform package
(`examlops.cli.surface`) rather than in the dashboard:

| Tier | Who | What it means |
|---|---|---|
| **Read** | anyone signed in | Reads platform state (`exa drift status`, `exa project list`, `exa hpc place`). |
| **Admin** | admin | Changes state or reads sensitive data (`exa project create`, `exa audit verify`, `exa rag ingest`). |
| **Destructive** | admin, plus typing the command back | Erases or overwrites (`exa project delete`, `exa backup restore`, `exa serve traffic`). |
| **CLI only** | nobody, from a browser | See [below](#commands-that-stay-in-the-terminal). |

**Arguments can raise the tier.** A read becomes an admin run when you
- turn on a flag that persists something (`exa models cost --record`, `exa hpc gpu-share plan
  --record`, or `exa cards model`, which saves the card unless you untick `--save`);
- give it a file path (`exa docs --out cli.md`);
- or point it at another host (`exa ask --approve …`, `exa hpc detect --host …`).

The badge next to the command title shows the tier your current arguments put the run in, and the
Run button explains why when it is disabled. The server re-checks all of this, so the badge is
only a preview.

## Files: the CLI workspace

Commands that read or write files see exactly one directory, the **CLI workspace**. Every path
you type is relative to it. Absolute paths and `..` are refused.

- **Inputs**: open the **Workspace** tab (admin), upload the file (e.g. `inputs/docs.jsonl`),
  then run `exa rag ingest mykb --docs inputs/docs.jsonl`. Path fields suggest workspace files as
  you type.
- **Outputs**: run `exa audit export --out exports/audit.json` (or `exa docs --out cli.md`,
  `exa cards model JPCP --out card.md`, …). The file appears under the run's output, and in the
  Workspace tab, to download.

The **Workspace listing is capped at 2000 files**, and says when it hit that cap: the response
carries `truncated: true` alongside `limit`. Until 2026-09-14 it simply stopped at 2000 and returned
a full-looking list, so an operator hunting for the file a command had just written could conclude
it was never produced. A path you know is still downloadable whether or not the listing showed it.

Symlinks are excluded from the listing **and** refused as paths: containment resolves the path
first, so a link inside the workspace pointing anywhere else is rejected rather than followed.

Commands themselves run from the repository root, like an operator's terminal
(`EXAMLOPS_DASHBOARD_CLI_CWD`, default `REPO_ROOT`). Commands that read the use-case pack or the
compose files by relative path therefore work. Your path arguments are still handed over as
absolute paths inside the workspace, so a command's own defaults (e.g. `exa backup create`'s
`./backups`) land where they would in a terminal. Pass `--out` to get a downloadable file.

In the compose deployment the workspace and the CLI's own `config.toml` live on the
`dashboard_cli_data` named volume (`/var/lib/examlops-dashboard/`). They survive rebuilds and
never land in the repo. Run bare (`uvicorn`), the workspace defaults to
`~/.local/state/examlops/cli-workspace`. Set `EXAMLOPS_DASHBOARD_CLI_WORKSPACE` to move it, and
`EXAMLOPS_CONFIG` to move the config.

On Kubernetes the chart mounts the same directory from `dashboard.cliState.existingClaim`. With
more than one replica, that claim must be `ReadWriteMany`, so that a file uploaded through one
replica can be read by a run on another. Left empty, the chart falls back to a per-pod
`emptyDir`, and `helm install` prints a warning when `replicas` is above 1.

## Commands that stay in the terminal

Thirteen commands are listed but not runnable here, and each one says why:

| Command | Why | Use instead |
|---|---|---|
| `exa chat` | an interactive REPL reading from a terminal | `exa ask` (one question, `--session` to continue) or the Copilot panel |
| `exa mcp serve` | starts a long-running server | run it on a host; `exa mcp tools` lists what it would expose |
| `exa config init` | an interactive setup wizard | `exa config set` one key at a time |
| `exa stack up` / `down` / `restart` | controls the host stack that serves this dashboard | the **Services** console, per service |
| `exa stack monitoring-up` / `monitoring-down` | brings the host monitoring stack up/down | `make monitoring-up` / `make monitoring-down` on the host |
| `exa instance init` | creates a host data root that every process must then be started with | run it on the host; `exa instance info` / `check` show the install here |
| `exa upgrade apply` | migrates the live datastore this dashboard runs on | run it on the host in a maintenance window; `exa upgrade plan` / `history` run here |
| `exa auth login` | an interactive device sign-in that waits for a browser approval | the dashboard has its own organisation sign-in; run it in a terminal |
| `exa auth token` | prints the caller's bearer credential, which from the console would be the dashboard server's own | run it in a terminal |
| `exa autopilot follow` | a long-running event consumer that starts an autopilot cycle whenever a training run completes | run it as a service on a host; `exa autopilot run` is the one-shot equivalent |

A few flags are refused because they would never finish or would print a secret:
`exa status --watch`, `exa drift status --watch`, `exa serve traffic-list --watch`,
`exa stack logs --follow` (always runs with `--no-follow`), `exa backup schedule` (always runs
one cycle, `--once`), and `exa secrets get --reveal` (the dashboard never returns a secret value).

A run never stops at a prompt, because nobody could answer it. A command's own `--yes` flag is
always passed for you (your confirmation in the console replaces it), so it has no checkbox. Any
value a command would otherwise ask for interactively is a required field instead: the secret
value for `exa config set`, and the version for `exa models rollback run`.

## When a run fails

A run needs what the same command needs in a terminal: a reachable service, credentials in the
CLI config, required inputs, and its Python dependencies inside the dashboard image. If one is
missing, the run fails with the CLI's own message and hint (exit 1), not a console error. For
example, `exa hpc queue` without `--cluster`, or `exa modelzoo status` when the control-plane
token is not configured. The dashboard image carries the CLI's core dependencies, not every
optional extra. Commands that need a heavy extra (the pipeline generator's Prefect stack, GPU
serving engines) report the missing module.

## Safety and audit

- Each run is its own process on the dashboard host, started without a shell. Arguments are
  checked against the command's declared parameters, and values can never be read as extra flags.
- The run's environment drops the dashboard's own credentials (JWT key, login passwords,
  database URL). It sets `EXAMLOPS_ACTOR` to your dashboard session, so records the CLI writes
  itself (e.g. a project's `created_by`) name you.
- Runs stop after `EXAMLOPS_DASHBOARD_CLI_TIMEOUT` seconds (default 300). Output is capped
  (`EXAMLOPS_DASHBOARD_CLI_MAX_OUTPUT`). At most `EXAMLOPS_DASHBOARD_CLI_MAX_CONCURRENT` runs
  are in flight overall (default 4) and `EXAMLOPS_DASHBOARD_CLI_PER_USER` per session (default 2).
  A busy runner answers *429* instead of queueing.
- You can cancel a run from its output view. A run cancelled before its process started never
  starts, so *cancelled* always means the command did not run to completion.
- Every accepted run writes two events to the tamper-evident audit chain: `cli_run` (who ran what,
  secret values shown as `***`) and `cli_run_finished` (the outcome). See them on the **Audit**
  console, or with `exa audit`.
- Run history lives in the platform datastore (`dashboard_cli_runs`), not in one process, so
  every dashboard replica serves every run and a restart keeps the history. Viewers see their own
  runs, admins see everyone's. The newest `EXAMLOPS_DASHBOARD_CLI_HISTORY` runs are kept (default
  500), each with up to `EXAMLOPS_DASHBOARD_CLI_STORE_OUTPUT` characters of output (default
  256000). A cancel raised on one replica stops the process on the replica that runs it, within
  about a second. A run whose replica died is reported as *lost* rather than left *running*. The
  audit chain remains the durable record.

## Switching the console off

Some deployments do not want a browser to run CLI commands at all. The console sits behind the
`cliConsole` feature flag (on by default). An admin turns it off under **Platform → Flags**, and
the change is live without a restart:

- the **backend** refuses it: every `/api/v1/cli/*` endpoint answers *403*, except cancelling
  a run, so turning the switch off never stops an operator from stopping a run already going;
- the **CLI Console** and **Resources** pages leave the navigation and say they are switched off,
  and the ⌘K palette stops offering commands and resource tables. The **Config** page drops its
  CLI configuration section, and pages lose their "open in CLI Console" link.

The switch is audited like every flag change. The terminal `exa` is unaffected.

## API

| Endpoint | Role | Purpose |
|---|---|---|
| `GET /api/v1/cli/catalog` | viewer | Every command: params, examples, panel, tier (gzip-compressed when the browser accepts it) |
| `POST /api/v1/cli/runs` | viewer (read) / admin | Start a run: `{command, args, format: json\|text, context?, confirm?}` → `202` with the run |
| `GET /api/v1/cli/runs[/{id}]` | viewer (own) / admin (all) | Run history, or one run with its output |
| `POST /api/v1/cli/runs/{id}/cancel` | the run's owner / admin | Stop a run |
| `GET/POST /api/v1/cli/workspace`, `GET/DELETE /api/v1/cli/workspace/file?path=` | admin | List, upload, download, delete workspace files |

Every endpoint except cancel also requires the `cliConsole` flag to be on for the caller.
Otherwise it returns *403*.

## Keeping it complete

When a command is added to `exa`, it shows up in the console automatically. It must also be
given a tier in `examlops/cli/surface.py`, and `tests/unit/test_cli_surface.py` fails until it
is. The same test fails when a new parameter looks like a file path but has no containment
decision. So the dashboard cannot quietly fall behind the CLI, and a new command cannot quietly
become runnable by viewers.
