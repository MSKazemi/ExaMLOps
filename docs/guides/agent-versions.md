# Agent versions

An agent's behaviour is a tuple: its code, its prompts, the models it calls, the tools it may use and
its policy. `exa agent version` registers that tuple as **one immutable, content-addressed version**
(ADR 0146), and `exa agent alias` moves `Staging` / `Canary` / `Production` between versions, with
Production gated on recorded evaluation evidence. This is the registry. It does not run an agent.

## A manifest

Every member is pinned by something that cannot move: an image digest, a prompt *version number*, a
model version, a tool-schema hash. JSON or YAML.

```yaml
schema_version: 1
agent: jobdoc
code: {image: "ghcr.io/example/jobdoc@sha256:<64 hex>", entrypoint: "app.graph:build", framework: langgraph}
prompts: [{name: jobdoc-system, version: 7}]
models:
  - {role: planner,  servable: "gen://qwen3-32b", binding: follow, alias: Production}
  - {role: embedder, servable: "gen://embed-e5",  binding: pin,    version: 4}
tools:
  tools: [{name: docs.search, schema_hash: "sha256:<64 hex>"}]
  grants: [docs.search, hpc.jobs.read]
policy: {contract: jobdoc-v2, autonomy: L2}
eval: {suites: ["jobdoc-trajectory@2"], non_inferiority_margin: 0.03}
```

Required: `schema_version`, `agent`, `code`, `prompts`, `models`, `tools`, `policy`. Optional: `memory`,
`state`, `eval`, `sandbox`, `budgets`, `guardrails`. Validation lists **every** problem and refuses:
unknown fields; a prompt pinned by `label`; an image without a digest; a `pin` model without a
version; a `follow` model with one; an unpinned suite; a stale `version_id` or `tools.manifest_hash`.
`follow` bindings are the one deliberate floating reference: they name an alias, and the version they
resolve to is not part of the identity. MCP tool schema hashes come from
`examlops.agent_versions.mcp_tool_manifest([...names])`.

`version_id` (`av-sha256:...`) is the hash of the canonical JSON of the whole manifest: change a
prompt version, a tool schema or a grant and it is a new version; register identical content and you
get the existing one back.

```bash
exa agent version register jobdoc.agent.yaml
exa agent version list --agent jobdoc
exa agent version show jobdoc@Production
exa agent version diff jobdoc@Staging jobdoc@Production   # which components changed, by name
```

## Aliases and the Production gate

```bash
exa agent alias set jobdoc Staging av-sha256:...           # free
exa agent alias set jobdoc Production jobdoc@Staging       # gated
exa agent alias show jobdoc Production                     # target + move history
exa agent alias rollback jobdoc Production --reason "regression"
```

Every move is written to the audit log and to the alias history. A `policy.yaml` rule on the action
`agent_promote` (context keys `agent`, `to_alias`, `alias`, `ref`; `rollback: true` for a rollback)
can deny or require approval; with no rule the command behaves as if the hook did not exist.

Production is refused unless **all** of this holds, and each unmet condition is named:

- an eval gate is configured for the registered-model name `agent-<name>` (`exa eval gate set`), in
  `block` mode. **No gate is not a pass.**
- the version has recorded results (`model_version` = its `version_id`) for the gate suite **and** for
  every suite its `eval.suites` declares, and a score for every gate metric;
- every judge that scored those suites is calibrated (ADR 0111): an unmeasured judge blocks with
  `no_calibration`, a biased one names the failed check;
- the gate's thresholds pass;
- where signing is configured (`EXAMLOPS_SIGNING_KEY` or an Ed25519 key), the manifest carries a valid
  signature. A site with no signing configured is not blocked.

`alias rollback` restores the version the alias held before its latest move. It is not re-gated (that
version passed the gate when it was promoted). Rolling back twice returns to where you started.

## Using a version from code

```python
from examlops.agent_versions import resolve
v = resolve("jobdoc", "Production")   # AgentVersion(version_id, agent, manifest, signed)
v.prompt("jobdoc-system")             # the pinned prompt version number, or None
```

The Skipper agent can opt in to a pinned system prompt: set `EXAMLOPS_AGENT_VERSION_PIN=<agent>[@<alias>]`
(alias default `Production`) and `skipper-system` is read at the prompt version number that agent
version pins, instead of the moving label. Unset, nothing changes. A pin that cannot be honoured is
logged and the label is used. Only the system prompt is consumed; Skipper's tool set is not read from
a manifest.

## Agent Card

`exa agent version card jobdoc@Staging [--out card.json]` prints an A2A-shaped card generated from a
registered version (also readable as the MCP resource `examlops://agent/jobdoc@Staging/card`; a bare
`jobdoc` means its Production alias). `version` is the content-addressed version id, so the card
changes exactly when the pinned tuple does; `skills` are the pinned tools described from the MCP
registry. The card declares `protocolVersion: "1.0"` and advertises only what exists: `streaming`,
`pushNotifications` and `stateTransitionHistory` are false and `extensions` empty, because the
platform has no task store, stream or push sender (the flags come from `IMPLEMENTED_CAPABILITIES`,
so they cannot be set by hand). It declares no `url`, interfaces or security scheme because nothing
serves the agent at `/.well-known/agent-card.json` yet. The A2A 1.0 JSON schema is not verified
offline: the card is checked for required fields and honesty, not schema-validated.

## Replacing Production: non-inferiority and state compatibility

When a version already holds Production, a candidate must also clear two more checks (ADR 0146 d3,
d5). Both verdicts are stored on the alias move and shown in the [evidence pack](#evidence-pack).

**Non-inferiority.** With `eval.non_inferiority_margin` declared, every gate metric that is a
proportion over a known sample (it carries a Wilson interval) is tested one-sided against the
running version (Newcombe hybrid-score interval, `analysis/ab_stats.non_inferiority_proportions`).
A candidate is refused when the bound on the difference crosses `-margin`. The measured
difference is recorded even when it passes. Too few samples cannot prove non-inferiority, so they
are refused too. A metric that is not a proportion is listed as `untested`, because there is no
per-sample data to test it with. Its regression is still caught by the gate's `max_drop`.

**State compatibility.** A manifest may export its checkpoint schema:

```yaml
state:
  schema_version: 3
  schema_hash: sha256:...          # must equal the hash of `schema`, or registration is refused
  schema:
    fields: {messages: {type: list}, step: {type: int, default: 0}}
    nodes: [plan, act, review]
    interrupt_nodes: [review]
```

`exa agent version compat A B` prints the verdict:

- `compatible`: only additive fields that carry a default.
- `incompatible`: a field was renamed, removed or retyped, a field was added without a default,
  or a node was removed.
- `inert`: a schema is missing on either side. This is never reported as compatible.

An incompatible or inert change promotes only with `--state-strategy pin` (threads still running
on the old version stay on it until they close) or `drain` (their in-flight work finishes on the
old version, and their next input starts on the new one). Checkpoints are never migrated
automatically.

```bash
exa agent version compat jobdoc@Production jobdoc@Staging
exa agent alias set jobdoc Production jobdoc@Staging --state-strategy pin
```

## Rolling out by session

```bash
exa agent alias set jobdoc Canary av-sha256:...
exa agent alias canary jobdoc 10                                # 10% of NEW sessions
exa agent alias rollback jobdoc Production --in-flight quarantine
```

The [agent runtime](agent-runtime.md) starts that share of new sessions on Canary and pins every
session to the version it started on. Changing the share never moves an existing session. A
rollback records what happens to sessions still on the bad version:

- `continue`: they keep running.
- `interrupt`: their runs are stopped.
- `quarantine`: their runs are stopped and the sessions refuse further input.

**Shadow is replay.** `AgentRuntime.replay(thread, candidate)` runs a candidate over a recorded
session. It uses tool results stubbed from the recording, through a gateway that has no route to a
live tool. It reports matched calls, calls the recording does not have, and whether each answer is
the same.

## Follow bindings: resolved per run, re-evaluated on model moves

A `follow` binding is resolved once, when a run starts, and recorded on the run
(`resolved_models`). A model canary therefore cannot switch versions in the middle of a run. Every
model-alias move goes through `examlops.events.alias_changed`. That call enqueues a re-evaluation
for every agent version that follows the alias, and writes `agent_reeval_enqueued` to the audit
chain. With `policy.reeval_on_follow: blocking`, the agent snapshot holds the binding on the model
version it was evaluated with until the entry is resolved `passed`.

```bash
exa agent version reeval --status pending
exa agent version reeval-resolve 12 --outcome passed
```

## Evidence pack

`exa agent version evidence <ref> --out pack.json` exports one sealed JSON document with:

- the tuple and whether its signature verifies;
- every evaluation result, with its Wilson interval and the `calibration_id` of the judge that
  produced it, plus that calibration record;
- the gate reports;
- the tool grants and the contract;
- every promotion and rollback, with the evidence stored on each move;
- the audit events that name the version, plus the chain head.

`digest` is the sha256 of everything else in the document, so an altered copy is detectable. The
pack serves EU AI Act Art. 50 transparency.

Evaluation results are keyed to the version they measure: `eval_suite_results.agent_version_id` is
filled automatically for any result recorded under `agent-<name>` for an `av-sha256:` version (or
passed explicitly to `record_eval_result`). The pack reads them with
`get_eval_results_for_agent_version`, which filters by version in SQL before its row cap, so a busy
sibling version cannot push this version's results out of the pack. Rows recorded before the column
existed still match by `model_version`.

## What is not built

- **MLflow `LoggedModel` registration** and `agent-<name>` in `models:/` URIs. Versions live in
  `platform.db`.
- **A tenant column on the agent tables.** They are listed as a known gap in the scope audit.
- **Judge scoring of replay trajectories.** Replay returns the trajectories and outputs; feeding
  them to calibrated judges is left to the eval pipeline.
- **Non-inferiority for continuous metrics at promotion time.** `non_inferiority_welch` exists,
  but eval results store one score per run, not per-sample values.
- **A worker that consumes the re-evaluation queue.** Entries are resolved by `reeval-resolve`
  (or by whatever runs the suites).
