# Extension Security & Trust Tiers (ADR 0081)

ExaMLOps can be extended at every layer: calculation providers, CLI plugins, policy rules and
agent-callable tools. This page states the one security model that covers all of them: whose
code runs, with what privilege, and how it is evaluated. It then covers the extension points
for providers and, in detail, the gated-mutation tier that governs agent writes over MCP
(ADR 0082).

## The three trust tiers

| Tier | Who/what | Privilege | Boundary | Used by |
|---|---|---|---|---|
| **T1: trusted code** | Built-in Python and entry-point plugins | Arbitrary code, like any installed dependency | Who can `pip install` or drop a file on the host. This is an OS/deployment concern and is **not** enforced in-process | Provider plugins, CLI plugins (`examlops.cli_plugins`), pipeline steps |
| **T2: sandboxed config** | Declarative YAML formulas and policy conditions written by sysadmins | Arithmetic and boolean logic over the inputs it is given, nothing more | `simpleeval`: no imports, attribute access, comprehensions, lambdas or I/O; a curated function allow-list; `ProviderError` on violation | Expression providers, policy `when:` rules |
| **T3: gated mutation** | Any actor, human or agent, that changes state | Only after passing the gate | Read-only default + explicit enable + dry-run + confirmation + policy check + audit | MCP write tools, `retrain`, `promote`, `approve` |

The five rules that follow from the contract:

1. **User config is never passed to `eval`/`exec`.** All declarative logic goes through the T2
   sandbox. If a use case needs something the sandbox can't safely express (loops, imports,
   attribute access), the answer is a T1 plugin. The sandbox is never widened to fit.
2. **Widening the sandbox is a gated decision.** Any addition to `SAFE_FUNCTIONS` needs a
   security note and fails CI until it is reviewed (see below).
3. **Mutation is deny-by-default on every surface.** Agents get read tools unless writes are
   explicitly enabled. Every enabled write is dry-run-able, confirm-gated, policy-checked and
   written to `audit_events`. See [Gated mutation](#t3-gated-mutation-agent-writes-over-mcp).
4. **Discovery fails safe, never open.** A plugin that fails to load is recorded
   (`ok=False, error=…`) and skipped. It never crashes the core and never silently swaps in
   different behaviour, and the built-in default stays in place.
5. **T1 plugins run with full host privilege, and there is no in-process sandbox for them.**
   See [Least privilege for plugins](#least-privilege-for-t1-plugins).

## Least privilege for T1 plugins

A provider plugin, a CLI plugin under `examlops.cli_plugins` or a pipeline step is ordinary
Python. It runs with the same privilege as the `exa` process that loaded it. It can read the
platform database, every environment variable (including tokens and keys), the files of the user
running the process, and the network. ExaMLOps does **not** sandbox arbitrary Python in-process,
and does not claim to. RestrictedPython, subinterpreters and similar tools were considered and
rejected as brittle (ADR 0081, *Alternatives*).

The trust boundary is therefore **who can install code on the host**. What to do about it:

- **Single-tenant sites.** Install plugins the way you install any other dependency: from a
  reviewed source, pinned, through the same change process as the platform itself.
  `exa plugins` and `exa providers list` show what is installed and whether it loaded.
- **Multi-tenant or untrusted plugins.** Use **OS-level isolation**: a separate container or
  pod per tenant, a dedicated unprivileged Unix user with no access to other tenants' data, a
  read-only root filesystem, and only the credentials that tenant needs. Do not load a plugin
  you do not trust into a process that holds platform-wide secrets.
- **If a formula is enough, use T2.** A YAML formula can't read files, environment variables
  or the network. Keep T1 for code that really needs Python (NumPy, HTTP, a vendor SDK).

## Calculation providers: the three ways to plug one in

Providers (ADR 0074/0077) span both code tiers:

| Provider kind | Trust tier | Author |
|---|---|---|
| Built-in Python provider, shipped inside `examlops` | T1 | Platform maintainers |
| Entry-point plugin under `exa.providers.<domain>` | T1 | Site operators publishing rate cards or scoring libraries |
| Declarative YAML formula in `providers.yaml` | T2 | Sysadmins adjusting coefficients or formulas |

```toml
# pyproject.toml of a plugin package (T1)
[project.entry-points."exa.providers.carbon"]
my-formula = "my_package.providers:MyCarbonProvider"
```

```yaml
# ~/.config/examlops/providers.yaml (T2, sandboxed)
[carbon]
provider = "expression"
[carbon.formulas]
kwh     = "gpu_hours * 0.35 * pue"
co2e_g  = "kwh * 500"
```

A T2 formula **cannot** read files, access environment variables, exfiltrate prompts or call
external APIs.

### The `SAFE_FUNCTIONS` allow-list

The sandboxed evaluator exposes only these math helpers (from `examlops/providers/expression.py`):

| Function | Provided by |
|---|---|
| `min`, `max`, `abs` | built-ins |
| `round`, `pow`, `sum` | built-ins |
| `log`, `log10`, `log2` | `math` |
| `exp`, `sqrt`, `floor`, `ceil` | `math` |

**Adding a function to `SAFE_FUNCTIONS` changes a security boundary.** It widens what a T2
formula author can call. Any such change must:

1. Include a security-review note in the commit message.
2. Update `_REVIEWED_SAFE_FUNCTIONS` in `tests/unit/test_provider_security.py`. CI fails
   until this is done.

### Sandbox guard tests

| Test | What it checks |
|---|---|
| `test_no_raw_eval_exec_in_sandboxed_modules` | `expression.py`, `yaml_provider.py` and `loader.py` contain no bare `eval`/`exec`/`compile` calls |
| `test_safe_functions_match_reviewed_set` | `SAFE_FUNCTIONS` equals the last-reviewed allow-list exactly |
| `test_sandbox_rejects_dangerous_expressions` | `__import__`, `__class__.__bases__`, `open`, comprehensions and lambdas all raise `ProviderError` |

A failed `test_safe_functions_match_reviewed_set` means the allow-list needs a security review.
Do not quietly update the test to make it pass.

## T3: gated mutation (agent writes over MCP)

The MCP server (`exa mcp serve`) exposes tools, resources and prompts to agents. Mutating tools
pass through several layers. Each one is enforced in code and covered by a test.

| Layer | What it does | Where |
|---|---|---|
| Exposure | Mutating tools are registered only with `--allow-writes` / `EXAMLOPS_MCP_ALLOW_WRITES` | `mcp/tools.py` |
| Dry run | Every mutating tool accepts `dry_run=true` | `mcp/write_safety.py`, `plans.preview_change` |
| Confirmation | Tier-B/C calls from a human need `confirm=true` | `mcp/write_safety.py` |
| Plan/apply + HITL | An agent must plan every write. A tier-B/C plan needs a human approval token | `plans.py` (ADR 0147) |
| Policy | Every call is checked with `policy.decide("agent_write", …)` and fails closed if policy is unavailable | `_agent_write_gate` (ADR 0079) |
| Audit | Every completed write, plan, approval and refusal goes to `audit_events` | `_audit_write`, `plans._audit` |
| Transport auth | OAuth 2.1 resource server on HTTP: audience binding, per-tool scopes, Origin validation | `mcp/http_auth.py` |

### Write tiers

| Tier | Meaning | Examples |
|---|---|---|
| `A` | Low-risk. Autopilot may do it | `set_traffic_split`, `disable_challenger`, `project_assign_model` |
| `B` | High-impact. Needs a human decision | `trigger_retrain`, `set_promotion_rule`, `hpc_approve_cluster`, `project_add_member` |
| `C` | Human-only. Never bound to an autonomous agent | `grant_access`, `approve_plan` |

`exa mcp capabilities --all` lists each tool's tier.

### Dry run

Every mutating tool takes `dry_run` (default `false`). `dry_run=true` changes nothing and returns
a preview:

```json
{"ok": true, "dry_run": true, "changed": false, "preview": {
  "tool": "set_promotion_rule",
  "intended_change": "set promotion rule for JPCP: rmse < 5.0",
  "current_state": {"state": null},
  "blast_radius": {"scope": "one model's promotion gate", "tier": "B", "reversible": true},
  "policy": {"action_kind": "set_promotion_rule", "effect": "allow"},
  "needs_confirmation": true,
  "required_approvals": [],
  "would_succeed": true}}
```

Any principal may dry-run, including an agent. A dry run is a read, so Skipper's bridge does not
interrupt the conversation for one. A policy that would refuse the call shows up as
`"effect": "deny"` and `"would_succeed": false` in the preview. It is not an error.

### Confirmation (human callers)

When a human's MCP client calls a tier-B or tier-C tool directly, the call is refused unless it
carries `confirm=true`:

```json
{"ok": false, "code": "confirmation_required", "tier": "B", "preview": {"...": "..."}}
```

The client shows its user the preview, then sends the call again with `confirm=true`. This is
the MCP version of the CLI's `[y/N]` prompt. If policy would refuse the call anyway, the caller
gets the policy error straight away and is not asked to confirm something that is bound to fail.
If the preview itself can't be computed, the call is still refused: the gate fails closed.

Tier-A writes are not confirm-gated by default. They are the autopilot-safe set and carry MCP
`destructiveHint` annotations the client can act on, so asking for confirmation on every one of
them would cause consent fatigue (ADR 0082, layer 4).

| Variable | Default | Effect |
|---|---|---|
| `EXAMLOPS_MCP_CONFIRM_TIERS` | `B,C` | Tiers whose direct human calls need `confirm=true`. Takes a subset of `A,B,C`, or `none`. A malformed value keeps the default |
| `EXAMLOPS_MCP_AUTO_CONFIRM` | unset | The `--yes` equivalent for scripted use. It must be set explicitly: `CI=true` alone does **not** auto-confirm |

A host that has already asked a human (Skipper's LangGraph `interrupt()`) runs the call inside
`examlops.mcp.write_safety.confirmed()`. A `confirm` argument set by the model itself is dropped.

### Agents: plan, apply, and a human for high-impact changes

An agent principal (`EXAMLOPS_PRINCIPAL_KIND=agent`) can't confirm for itself, because a flag the
model sets is not human consent. A direct write from an agent is refused with `plan_required`.
The agent must first call `plan_change(tool, args)`, then `apply_plan(plan_hash)` (ADR 0147).

For **tier B and C**, the plan lists `human_approval` under `required_approvals`, and
`apply_plan` refuses to run it without an `approval_token`. A human mints that token with
`approve_plan`, which agents are refused. This is ADR 0082's layer 4: promotion rules, cluster
approval, retrains and membership changes always get a human decision, however permissive the
policy is. An agent can never apply a tier-C plan, even one a human wrote.

| Variable | Default | Effect |
|---|---|---|
| `EXAMLOPS_MCP_HITL_TIERS` | `B,C` | Tiers whose agent-applied plans need a human approval token. Takes a subset of `A,B,C`, or `none`. A malformed value keeps the default |

### Remote MCP over HTTP: OAuth 2.1 resource server

Without configuration, `exa mcp serve --transport http` binds only to loopback. It still
validates the `Origin` header: a request whose `Origin` is not loopback or not on the allow-list
is refused with 403, which guards against DNS rebinding.

To serve remote clients, turn on the resource server. The trust file is the same one the
control plane and dashboard use ([identity federation](identity-federation.md), ADR 0120):

```bash
export EXAMLOPS_IAM_CONFIG=/etc/examlops/identity-providers.yaml
export EXAMLOPS_MCP_AUTH=oauth
export EXAMLOPS_MCP_RESOURCE=https://mcp.example.org/mcp   # canonical URI of this endpoint
export EXAMLOPS_MCP_ALLOWED_ORIGINS=https://console.example.org
exa mcp serve --transport http --host 0.0.0.0 --port 8765 --allow-writes
# terminate TLS in front of it (the resource URI is https)
```

With `EXAMLOPS_MCP_AUTH=oauth`:

- `GET /.well-known/oauth-protected-resource`, plus the path-suffixed form such as
  `/.well-known/oauth-protected-resource/mcp`, serves the **RFC 9728** Protected Resource
  Metadata: the resource, the trusted authorization servers, the supported scopes and
  `bearer_methods_supported: ["header"]`. It needs no authentication.
- Every other request needs `Authorization: Bearer <access token>`. The token is verified by
  the platform verifier: signature, issuer, expiry and the account directory. Its `aud` must
  also **name this MCP resource**. A token issued for the control plane or any other service is
  refused, so tokens can't be passed through from one service to another.
- A failure returns 401 or 403 with an RFC 6750 challenge that tells the client where to find
  the metadata:
  `WWW-Authenticate: Bearer resource_metadata="https://mcp.example.org/.well-known/oauth-protected-resource/mcp", error="insufficient_scope", scope="mcp:tools:admin mcp:tool:set_promotion_rule"`.
- The server starts only if the configuration is safe. If the resource URI is missing, uses
  http on a non-loopback host, or has a fragment, or no identity provider is configured, it
  refuses to start (`McpAuthConfigError`).

**Scopes.** A `tools/call` needs one of the scopes for that tool:

| Scope | Grants |
|---|---|
| `mcp:tools:read` | Read tools, resources and prompts |
| `mcp:tools:write` | Read tools and tier-A writes |
| `mcp:tools:admin` | Every tool, including tier B/C |
| `mcp:tool:<name>` | Exactly that one tool |

`plan_change` and `apply_plan` need the scope of the tool they plan or apply, so a
`mcp:tools:write` token can't apply a tier-B plan. An `apply_plan` whose plan the server can't
resolve needs `mcp:tools:admin`. Reading resources or prompts needs a coarse scope; a single
`mcp:tool:<name>` grant reaches only that tool. Scopes decide which tools a caller may reach
at all. Dry-run, confirmation, plan/approval and policy still apply on top. Run `exa mcp scopes`
(or `exa --json mcp scopes`) to print the full catalogue.

**The user's role applies as well as the client's scope.** A scope says what the *client* was
granted; the platform role from the trust file (and the center's PDP, when one is configured)
says what the *user* may do. Both must allow a call: reads need `viewer`, tier-A writes
`operator`, tier-B/C writes `admin`, through the same `authorize()` the control plane and
dashboard use. A refusal is a 403 `access_denied`, audited as `authz_denied`; if the PDP can't be
reached, the call is refused.

**Sessions are bound to the principal.** The MCP session id the server issues is tied to the
principal that opened it. A request carrying that session id with another principal's token
(stream, call or `DELETE`) is refused with 403. The binding table is an LRU capped at
`EXAMLOPS_MCP_MAX_BOUND_SESSIONS` (default 10000).

**Bodies are parsed before they are forwarded.** A `POST` whose body isn't a JSON-RPC message or
batch is refused with 400 rather than passed to the MCP server. A body the guard can't read would
otherwise skip every per-tool check.

**Audit and metrics.** Every refusal (`mcp_http_denied`) and every authorized mutating call
(`mcp_http_tool_authorized`) is written to `audit_events` with the verified principal as the
actor. Decisions are counted in `examlops_mcp_http_auth_total{outcome}`. Origin refusals are
counted but not audited individually, because a hostile page can make a browser send any
number of them. Refusals of unauthenticated requests are audited up to
`EXAMLOPS_MCP_ANON_DENY_AUDIT_PER_MIN` per minute (default 60), and every one is still counted.
Anyone who can reach the port can send bad tokens, and each audit row is a locked write to the
hash-chained log. Request bodies are capped at `EXAMLOPS_MCP_MAX_BODY_BYTES` (default 1 MiB),
and larger ones get a 413.

**Opaque tokens** are introspected at their own issuer (RFC 7662) and must report an `aud` that
names the MCP resource.

## Trust-tier decision tree

```
What are you adding?
├── Python code (provider plugin, CLI plugin, pipeline step) → T1: trusted install;
│                                                               isolate by OS if untrusted
├── A formula or policy condition in YAML                    → T2: sandboxed, arithmetic only
└── A new state-changing operation                           → T3: register as a mutating tool
                                                                with a tier; it inherits dry-run,
                                                                confirmation, plan/apply, policy
                                                                and audit automatically
```

## Related

- ADR 0074: generic `examlops.providers` substrate
- ADR 0077: provider domains
- ADR 0079: policy-as-code (`agent_write` decisions)
- ADR 0081: the trust-tier security contract
- ADR 0082: hardening the agent-programmable (MCP) surface
- ADR 0147: plan/apply for agent principals
- [Identity federation](identity-federation.md): the trust file the MCP resource server uses
- [Agent tool broker](tool-broker.md): per-agent grants on top of scopes
