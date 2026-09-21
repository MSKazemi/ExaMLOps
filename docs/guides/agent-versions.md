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

## What is not built

This slice is the registry and the evidence gate. Not built: MLflow `LoggedModel` registration and
`agent-<name>` in `models:/` URIs (versions live in `platform.db`); the non-inferiority test
(`analysis/ab_stats.py` still has only Welch/z tests, so `eval.non_inferiority_margin` is recorded,
not enforced); the state-compatibility gate; canary by session and replay shadow; re-evaluating
dependents when a followed model is promoted; per-run resolution of `follow` bindings; the evidence
pack export; an `agent_version_id` column on `eval_suite_results` (evidence is keyed by
`model_version`); and a tenant column on the new tables.
