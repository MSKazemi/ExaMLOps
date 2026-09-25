# Agent runtime

The agent runtime runs **agents as a deployable workload**. You register an agent version
([Agent versions](agent-versions.md)) and the runtime hosts it: sessions (threads), runs, human
interrupts and durable state. It serves many agent versions for many tenants over one HTTP surface.
You do not write a new service for each agent.

It lives in `examlops.agent_runtime`.

## Quick start

```bash
# 1. Control plane: compile what the runtime serves from.
exa agent runtime snapshot --out /state/agent-snapshot.json

# 2. Serving plane: run it. Callers authenticate with bearer tokens mapped to a tenant.
export EXAMLOPS_AGENT_RUNTIME_TOKENS='{"<a long random token>": {"subject": "ci", "tenant": "acme"}}'
export EXAMLOPS_AGENT_ENTRYPOINT_ALLOW=myorg.agents     # module prefixes agent code may load from
exa agent runtime serve --snapshot /state/agent-snapshot.json      # 127.0.0.1:18005

# 3. Talk to an agent.
curl -s -H "Authorization: Bearer $TOKEN" -d '{"agent": "jobdoc"}' localhost:18005/threads
curl -s -H "Authorization: Bearer $TOKEN" -d '{"input": "why did job 42 fail?"}' \
     localhost:18005/threads/<thread_id>/runs/wait
```

`exa agent runtime serve` is a long-running process, so the dashboard's CLI console does not offer
it. Run it on a host.

## What `serve` does besides answering requests

A background loop runs one bounded pass every `--interval` seconds (default 15, env
`EXAMLOPS_AGENT_RUNTIME_INTERVAL`). Each pass does three things:

1. **Follows the snapshot.** When the snapshot file changes, a newer generation is adopted. A file
   that is unreadable, invalid, tampered with (its digest does not match) or older than the one in
   force is **refused, and the last-known-good snapshot keeps serving**. A broken control plane can
   stop new configuration from arriving. It cannot take running agents down.
2. **Sweeps sessions**: `active → idle → suspended`. A suspended session releases its sandbox and
   its session-quota slot. Its next input resumes it from the state store.
3. **Recovers orphaned runs.** A run left `running` by a worker that died, whose lease has expired,
   resumes from its last checkpoint on this worker. At most five per pass; the rest wait for the
   next pass.

If one of these fails, the other two still run. `GET /healthz` reports each one's counters: snapshot
generation, adopted, rejected and stale files, sessions moved, and runs recovered. The endpoint is
unauthenticated, so it never shows file paths or error text.

| Flag | Default | Meaning |
|---|---|---|
| `--snapshot` | `$EXAMLOPS_AGENT_SNAPSHOT` | Snapshot file to serve from and follow. Required. |
| `--host` / `--port` | `127.0.0.1` / `18005` | A bind beyond loopback is refused unless `--allow-remote` is given. Put TLS in front. |
| `--state-db` | `$EXAMLOPS_AGENT_STATE_DB` | Agent state store. It is refused if it is `platform.db`. |
| `--worker` / `--peer` | `worker-1` | This worker's id, and every worker id in the pool (for session affinity). |

## HTTP surface

It mirrors the LangGraph Agent Protocol shape, so LangGraph-native clients map onto it. Assistants
are the snapshot's agents and aliases.

```
GET    /healthz
GET    /assistants
POST   /threads                         {"agent", "alias"?, "metadata"?}
GET    /threads?status=&after=&limit=
GET    /threads/{thread_id}             DELETE closes it
GET    /threads/{thread_id}/state
POST   /threads/{thread_id}/runs        {"input"}                 -> 202, runs in the background
POST   /threads/{thread_id}/runs/wait   {"input"} | {"command": {"resume": ...}}
GET    /threads/{thread_id}/runs/{run_id}
POST   /threads/{thread_id}/runs/{run_id}/cancel   {"action": interrupt|rollback|cancel}
```

- **Authentication fails closed.** `EXAMLOPS_AGENT_RUNTIME_TOKENS` is a JSON map
  `{token: {"subject", "tenant"}}`. If it is unset, empty, or holds a placeholder or a token shorter
  than 16 characters, every request is refused with 503.
- **The tenant comes from the credential**, never from the request body. Another tenant's thread
  answers 404, the same as a missing one.
- **Bodies are bounded**: 512 KiB per request and 256 KiB per run input.
- **Affinity.** With `--peer` set, a request for a thread this worker does not own answers
  `421 Misdirected Request` and names the owner.

## Adapters: framework neutrality

An agent version's `code.framework` picks an adapter. Every adapter states what it can actually do,
and the runtime refuses to host a version whose manifest asks for more. For example, a version that
declares the `rollback` strategy on an adapter that cannot discard checkpoints is refused at deploy
time, with the reason.

| Adapter | durable | interrupts | idempotent tool keys | rollback | state schema |
|---|---|---|---|---|---|
| `python`: the built-in journaling step engine | yes | yes | yes | yes | yes |
| `langgraph`: the reference adapter over LangGraph's own `SqliteSaver` | yes | yes | no | no | yes |

A `python` agent is a list of named nodes, each `fn(state, ctx) -> dict`. The engine writes a
checkpoint after every node. A node that is not idempotent routes its side effects through
`ctx.call_tool`.

A `langgraph` agent's entrypoint returns an **uncompiled** `StateGraph`. The adapter compiles it
against the agent state store, so LangGraph's checkpoints never reach `platform.db`. This adapter
needs the optional `langgraph-checkpoint-sqlite` package.

Code is loaded only from module prefixes listed in `EXAMLOPS_AGENT_ENTRYPOINT_ALLOW`. If that is
unset, no agent code is loaded.

## Durability and idempotency

- Checkpoints are written at step boundaries to the **agent state store**: SQLite on a single node,
  and never `platform.db`. Every checkpoint carries `agent_version_id` and `state_schema_version`.
- A worker holds a per-thread lease while it steps a thread, and a heartbeat renews it every third
  of its TTL, so a node that spends longer than the TTL in one model or tool call does not look
  dead. When the worker dies, the renewals stop, the lease expires and another worker recovers the
  run. A worker checks that it still holds the lease before every tool call and every checkpoint;
  one that has lost it (a stall longer than the TTL) stops without acting and leaves the run to its
  new owner.
- Retention (`AgentStateStore.prune`) never drops a journaled tool result of a run that can still
  execute (pending, running or parked on a human), however old it is.
- Every tool call gets the key `hash(thread_id, checkpoint_id, node, call_seq)`. A re-executed node
  presents the same key. The runtime's journal returns the stored result, so the side effect happens
  once. A tool that accepts an `idempotency_key` parameter also receives the key.
- A tool call that needs approval (a tier-B write, or a `require_approval` policy outcome) **parks
  the run** as `interrupted`. The approval is stored, so it survives a restart, and the write then
  executes exactly once.

## Sessions and concurrent input

A session (thread) moves through `active → idle → suspended → active | closed`. `quarantined` is a
rollback outcome. Opening a session, and resuming a suspended one (by new input or by answering
the interrupt it is parked on), is checked against the tenant's `max_sessions` quota from the
snapshot. The check runs in the same transaction as the write, so concurrent requests cannot
overshoot the quota. A quota breach answers 429. Every status change is a compare-and-set, so the
background sweep never moves a session that was closed or quarantined in the meantime back to
`idle` or `suspended`. Under `enqueue`, at most 32 runs may wait on one thread. Past that, new
input answers 429 (`thread_queue_full`).

When a thread is already busy, a second input meets the agent's `policy.multitask_strategy`:

| Strategy | Outcome |
|---|---|
| `reject` (default) | The second input is refused. This is Skipper's behaviour today. |
| `enqueue` | It runs after the first one finishes. |
| `interrupt` | The first run stops at its next step boundary, keeping its progress (`superseded`). |
| `rollback` | The first run stops and its checkpoints are discarded (`rolled_back`). |

A manifest's `budgets.max_steps` stops a runaway run (`budget_exceeded`).

## Rollout by session

- A new session starts on `Production`, or on `Canary` for the configured share of **new** sessions
  (`exa agent alias canary jobdoc 10`). It stays pinned to the version it started on.
- On a Production move, the recorded state-compatibility verdict decides what in-flight sessions do.
  `compatible` sessions move to the new version at their next input. `pin` keeps them on the old
  version. `drain` finishes their current work there and moves them afterwards.
- A rollback's in-flight policy reaches the runtime through the snapshot:
  - `continue`: sessions on the bad version keep running.
  - `interrupt`: their runs are stopped.
  - `quarantine`: their runs are stopped and the sessions refuse further input.
- **Shadow is replay.** `AgentRuntime.replay(thread, candidate)` runs a candidate over a recorded
  session through a gateway that has no route to a live tool.

## Static stability

On the request path, the runtime reads only its snapshot and its own state store. It does not read
the control plane, MLflow, Prefect or `platform.db`. The snapshot carries:

- agents and their aliases, the canary share, migrations and retirements;
- the manifests of every version the runtime may meet;
- tool grants, so the broker reads grants from the snapshot, not the live table;
- follow-binding model resolutions, from the serving snapshot;
- re-evaluation pins;
- per-tenant quotas (`EXAMLOPS_AGENT_QUOTAS`, `EXAMLOPS_AGENT_MAX_SESSIONS`).

The snapshot is digest-sealed, and the digest covers its `generation`, so an edited generation cannot
cause genuine later snapshots to look older. The digest is a content hash, not a signature: it catches a
corrupted or hand-edited file, not someone who can rewrite the file and recompute it. The state
store keeps the last adopted copy, so a restarted runtime serves even when no snapshot file is
reachable.

**One exception: tool calls.** The default tool gateway is the in-process tool broker. It reads its
grants from the snapshot, but it still writes its evidence record, counts rate limits and injects
credentials through `platform.db`. With `platform.db` down, sessions open and runs that call no tool
complete. A tool write is refused with `evidence_unavailable`. A tool read with no rate limit and no
injected credential still runs, and its lost evidence record is counted.

## Sandboxes

A node that runs code or shell calls `ctx.sandbox_exec(...)`. The call goes through one
`SandboxProvider` seam:

| Substrate | Provider | Isolation advertised |
|---|---|---|
| `compose` | Docker, with `--runtime=runsc` when installed | `gvisor`, or `container-weak` without runsc |
| `hpc` | Apptainer `--containall --net --network none` | `container` |
| `k8s-agents` | `agent-sandbox` `SandboxClaim` (`agents.x-k8s.io/v1beta1`) | `gvisor` / `vm` |

- **Isolation is measured, not assumed.** `exa agent runtime sandboxes` shows what this host offers.
  `serve` offers the providers whose binary is installed. An agent's `sandbox.isolation`, or a
  tenant quota's `sandbox_isolation`, that is stronger than the best provider available is refused,
  and the refusal names both levels. With no provider installed, every sandbox call is refused. Code
  never runs unsandboxed.
- **Egress is default-deny.** Every sandbox starts with no network. A per-version `sandbox.egress`
  allow-list needs an operator-configured egress proxy (`EXAMLOPS_SANDBOX_EGRESS_PROXY`). Without a
  proxy, a version that asks for egress is refused. With one, the proxy's own policy is what is
  enforced: the runtime does not send the version's host list to the proxy. Configure the proxy to
  be at least as strict as every version's list. On Docker, `EXAMLOPS_SANDBOX_PROXY_NETWORK` must be
  an `internal` network whose only way out is the proxy.
- **Sandboxes hold no credentials.** Their environment is cleared.
- **Limits:** `EXAMLOPS_SANDBOX_CPUS` and `EXAMLOPS_SANDBOX_MEMORY` bound each Docker sandbox. The
  Apptainer provider sets no CPU or memory limit of its own; on HPC that is the batch job's
  allocation. Every exec has a timeout and a 64 KiB output cap. Templates come from
  `EXAMLOPS_SANDBOX_TEMPLATES`.

## What is not built

- **A Postgres agent state store.** The store is SQLite. Production HA and the lease coordinator on
  Postgres are not built, and the store has no encryption at rest.
- **The ADR 0116 admission seam, per-run budgets in currency, and per-task cost attribution.**
  Admission is the snapshot's `max_sessions` quota, and budgets are step counts.
- **Kubernetes and HPC deployment packaging for the runtime itself.** The substrate sandbox
  providers exist, and the Kubernetes provider is exercised against a fake API only.
- **Skipper as the runtime's first tenant.** Skipper still runs as its own service.
- **A Pydantic AI or DBOS adapter.** The built-in journaling engine is the framework-neutral path.
- **Streaming runs over HTTP**, stateless runs, and crons.
- **Scheduled retention.** `AgentStateStore.prune` exists, but neither the maintainer nor a CLI
  command calls it yet, so the state store grows until an operator prunes it.
- **The open MCP gateway product** (agentgateway / Envoy AI Gateway). Tools are reached through the
  in-process [tool broker](tool-broker.md) behind the runtime's `ToolGateway` seam.
