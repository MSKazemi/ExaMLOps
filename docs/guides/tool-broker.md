# Agent tool broker

An agent with tools turns text into actions, so *which* tool an agent may call, with *which*
arguments and *how often* is a policy question, not a prompt question. The tool broker (ADR 0145,
tool half) answers it in one place: `examlops.tool_broker` sits between an agent and the platform's
tool registry (`examlops.mcp.tools`), decides every call from the agent's **grants**, audits every
decision, and only then runs the real tool.

This is the smallest software-only slice of the ADR. It is **not** a gateway product and it does not
sandbox code; see [What is not built](#what-is-not-built).

## Grants and the default

A grant belongs to a **subject**: an agent-version id (`av-sha256:...`, ADR 0146), an agent name, or a
workload-identity subject. It names one tool, or `*` for all of them.

| Field | Meaning |
|---|---|
| `effect` | `allow` (default) or `deny`. |
| `tier_ceiling` | `read`, `A`, `B` or `C`: a tool above it is denied (write tiers, ADR 0102). |
| `needs_approval` | The call needs a human (see [Approval](#approval)). |
| `max_calls_per_minute`, `max_calls_per_session` | Bounded counters in `platform.db`. A per-session limit needs a session id. |
| `arg_schema` | A JSON-Schema **subset**: `type`, `properties`, `required`, `additionalProperties`, `enum`, `const`, `pattern`, `minLength`, `maxLength`, `minimum`, `maximum`, `items`, `minItems`, `maxItems`. Anything else is refused when the grant is set. A `pattern` is at most 256 characters and is matched (fully) only against strings up to 4096 characters. |
| `credentials` | `{parameter: secret name}`, injected by the broker (see below). |
| `egress` | `{url_args: [...], allowed_hosts: ["api.example.com", "*.example.org"]}` for tools that take a URL. |

**The default, exactly:**

- a caller with **no grant set** is not brokered: the decision is `allow` (reason `no_grant_set`), which
  is what happens today;
- a caller **with** a grant set is **default-deny**: a tool no grant covers is denied;
- an exact-tool grant beats a `*` grant;
- the most specific set wins **whole**, never merged: version id, then agent name, then workload
  subject;
- removing a subject's *last* grant makes it un-brokered again. To keep a subject locked out, store
  `exa broker grant set <subject> '*' --effect deny`.

```bash
exa broker grant set jobdoc list_models
exa broker grant set jobdoc set_traffic_split --tier-ceiling A --needs-approval --max-per-minute 5
exa broker grant set jobdoc recent_audit_events \
  --arg-schema-json '{"type":"object","additionalProperties":false,"properties":{"model":{"type":"string","pattern":"jpcp.*"}}}'
exa broker grant list
exa broker simulate --agent jobdoc --tool set_traffic_split --args-json '{"model":"jpcp","production":100}'
```

Setting or removing a grant passes through the `tool_grant_change` policy hook (a `policy.yaml` rule
can deny it or require approval) and is audited (`tool_grant_set`, `tool_grant_removed`).
`exa broker simulate` runs nothing, counts no quota and reads no secret; it exits 1 on a deny.

## What one call goes through

`invoke(caller, tool, args, ctx)`:

1. the tool must exist in the registry;
2. the grant decides. For `plan_change` and `apply_plan` the **target** tool's grant is checked as
   well, so a grant for `apply_plan` cannot launder a mutation the agent holds no grant for;
3. approval, if the grant or an operator `tool_call` rule asks for it;
4. egress: a URL argument's host must be in the grant's allow-list **and** pass the dataplane's SSRF
   check (`examlops.dataplane.safety.check_address`: private, loopback and metadata addresses refused,
   DNS resolved once);
5. rate limits;
6. credentials are read and injected;
7. the real tool runs.

Every decision writes one audit row, `tool_broker:allow|deny|require_approval`, with the agent, the
agent version, `on_behalf_of`, session, correlation id, tool, tier, reason code and the **redacted**
arguments (secret-named keys masked, credential-shaped text masked with the dataplane redactor).
Audit is best-effort by design (`audit_best_effort`): a lost row is counted in
`audit_events_dropped`, never silently.

The tool itself is untouched: it keeps its own `plan_required` gate for an agent principal
(`EXAMLOPS_PRINCIPAL_KIND=agent`) and its own `agent_write` policy gate. The broker calls the tool
and so reuses both; it does not re-implement them.

### Approval

`needs_approval` is satisfied by exactly three things, none of which an agent can put in its
arguments: an `apply_plan` whose plan a human approved (`approve_plan`) and whose token is presented;
an `approve` decision supplied by the runtime in code (`BrokerContext(approved=True)`, the ADR 0144
interrupt result); or an already-verified approval context. `plan_change` never needs approval
(planning changes nothing). A read tool with `needs_approval` is therefore not callable by an
autonomous agent, by design.

### Credentials

A grant's `credentials` map a tool parameter to a secret **name**. At call time the broker reads the
value from `examlops.secrets` and passes it to the tool; the agent never holds it. If the agent
supplies that parameter itself the call is denied (`credential_in_args`). The value is masked in the
audit row and scrubbed from the tool's result (a tool may echo it). No tool in today's registry needs
a secret of its own, so the seam is exercised with a test tool.

## Turning it on

```bash
EXAMLOPS_TOOL_BROKER=off       # default: the MCP server is byte-identical to before
EXAMLOPS_TOOL_BROKER=monitor   # decide + audit every call, never block, count no quota
EXAMLOPS_TOOL_BROKER=enforce   # block, and list only the tools the caller may call
EXAMLOPS_AGENT_NAME=jobdoc EXAMLOPS_AGENT_VERSION_ID=av-sha256:... exa mcp serve
```

Stdio MCP is one client per server process, so the process's own identity
(`EXAMLOPS_AGENT_NAME`, `..._VERSION_ID`, `..._SUBJECT`, `..._ON_BEHALF_OF`, `..._SESSION`) is the
caller. **This is not authentication**: whoever can start the server chooses the identity, and the
HTTP transport is still loopback-only and unauthenticated. The broker constrains a *cooperating
runtime*; binding an identity to a verified token is the gateway half, not built. An unrecognised
mode value means `enforce` (a typo must not disable a control).

Skipper is not routed through the broker (its code was out of scope for this change).

## What is not built

- The **MCP/A2A gateway product** (agentgateway / Envoy AI Gateway), OAuth 2.1 resource-server
  behaviour, RFC 9728 metadata, RFC 8707 audience binding and RFC 8693 token exchange (ADR 0120 lists
  the last as a follow-up).
- **Blast-radius contracts and autonomy levels** (ADR 0113) as a per-call input; only the write tier
  ceiling is enforced.
- **Synchronous evidence-chain writes** for tool writes and denials: decisions use best-effort audit.
- **Serving-snapshot delivery** of grants (ADR 0127): grants are read live from `platform.db`.
- **The sandbox seam** (`SandboxProvider`) and per-agent-version network egress for the sandbox.
- **The whole model half** of ADR 0145 (the inference gateway path, Skipper's migration onto it) - a
  separate work stream.
- Tenant scoping of grants: `tool_grants` has no tenant column (listed as a known gap in the scope
  audit).
