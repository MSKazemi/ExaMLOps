# Policy-as-Code Governance (D5)

> Next-Gen 40 · feature **D5** · ADR 0029 · spec `design/vision/specs/D5-policy-as-code.md`

D5 puts a **`PolicyEngine` seam** in front of every governed decision — promotion,
approvals, GPU budget, model-card completeness, tenancy, supply-chain — so those decisions
consult one **versioned, signed, per-tenant policy bundle** instead of bespoke `if`
statements scattered across the code. Decisions take **structured input** and return
`{allow, reasons}`, are **explainable** via a dry-run, and are **audited**.

This *layers on top of* the existing declarative policy engine (`examlops.policy`, ADR
0079) — it does not replace it. With no OPA installed and no policy file, everything still
works: the default engine is the YAML core and the default effect is allow.

## The engine seam

```python
from examlops.policy_engine import PolicyInput, evaluate

decision = evaluate("promotion", PolicyInput(
    action="promote", subject="alice", resource="JPCP/17", tenant="acme",
    context={"rmse_new": 4.1, "rmse_prod": 5.0},
))
decision.allow      # True/False
decision.reasons    # human-readable
decision.effect     # allow | deny | require_approval
```

| Engine | When | Notes |
|---|---|---|
| `YamlPolicyEngine` | default | delegates to `examlops.policy.decide` (policy.yaml) |
| `RegoPolicyEngine` | `EXAMLOPS_POLICY_ENGINE=opa` **and** `opa` on PATH | shells to `opa eval`; **degrades** to YAML if OPA absent |

## Fail-open vs fail-closed (R4)

Security-critical decisions **fail closed** (deny) if the engine errors; the rest run in a
monitor spirit (fail-open) so a policy bug can't take down bookkeeping:

```
fail-closed: supply_chain · deploy · budget · tenancy
fail-open  : promotion · approval · model_card · …
```

## Human-driven mutations: `manual_promote` and `cluster_approve` (ADR 0079 decision 2)

`exa pipeline promote` and `exa hpc approve` consult `examlops.policy.decide_safe` before they
change anything, so an operator can govern a person at the keyboard the same way the autopilot
and the agent write gate are governed. The action kinds and the facts a `when:` can use:

| Action | Facts in the decision context |
|---|---|
| `manual_promote` | `model`, `version`, `from_alias`, `to_alias`, `metric`, `metric_value`, `<metric>_new` (e.g. `rmse_new`), `operator`, `threshold`, `force`, `actor` |
| `cluster_approve` | `cluster`, `target`, `scheduler`, `host`, `transport`, `actor` |

```yaml
policies:
  - name: freeze-prod
    action: manual_promote
    when: "to_alias == 'Production'"
    effect: deny
  - name: second-look
    action: cluster_approve
    effect: require_approval
```

* **No file, or no rule matches** - behaviour is unchanged and no `policy:*` audit row is written.
* **`deny`** - the command stops with exit code 1 and names the rule; the alias is not moved and
  the cluster stays `PENDING`. `--force` on promote does **not** override a policy deny (it only
  bypasses the built-in eval/SLO/compliance metric gates).
* **`require_approval`** - the command's existing confirmation prompt appears with
  "[policy requires approval]" and its default flips to *no*, exactly as `exa retrain` does. The
  human answering `y` is the approval; `--yes`/`--json` still auto-confirm for a human's script, and
  an agent principal is refused by the confirmation layer. (The policy layer has no separate
  approver identity; that would be a new mechanism, not built.)
* Any rule that decides (allow, deny or require_approval) is audited as `policy:<action>` with the
  effect and rule name; an engine failure denies and audits `policy_unavailable:<action>`.
* `--dry-run` on promote consults nothing (it changes nothing).

### `exa policy simulate`

```bash
exa policy simulate manual_promote --set to_alias=Production --set rmse_new=4.1
exa policy simulate cluster_approve --context-json '{"scheduler": "flux"}'
```

Evaluates the decision with **no side effects** (never audited). Exit codes: `0` allow, `1` deny,
`4` require_approval (`2` is the CLI's usage-error code), so it works as a CI check. `-o json`
prints `effect`, `rule`, `reason` and `exit_code`. `exa policy test` is the always-exit-0
sibling.

### A `policy.yaml` that does not parse is not "no policy"

`~/.config/examlops/policy.yaml` (the declarative layer read by `examlops.policy.decide`) defaults
to **allow** when it yields no rules. That is right for a file that is absent — nobody has asked
for a gate — but it means an unparsable file silently removes *every* rule you wrote, including a
human-approval gate on an autopilot promote. Fail-open here is deliberate (a broken file must not
wedge a mutation path) but it is no longer silent:

- `exa policy list` **exits 1** and names the file and the parse error. It used to report a file
  that was right there as *absent*, which sends you looking in the wrong place entirely.
- `exa policy test <action>` prints the same warning above the decision — otherwise it answers
  "allow — no matching policy", which is true of the rules that loaded and deeply misleading about
  the rules you actually wrote.
- Every other caller goes through `_load_policies`, which logs a `WARNING` on the
  `examlops.policy` logger and carries on.

An **empty** file is not an error (it is a legitimate "no policies"), but a file whose top-level
key is wrong — `rules:` instead of `policies:` — is: it parses cleanly and gates nothing.

Check it after every edit:

```bash
exa policy list && echo "policy file is live"
```

## Domain gates — built-in default-deny a bundle can only tighten

```python
from examlops.policy_engine import supply_chain_gate, budget_gate, card_gate

supply_chain_gate("JPCP", "17", signed=False)   # DENY — unsigned artifact (GWT-4)
budget_gate(gpu_hours_requested=100, budget_gpu_hours=50)   # DENY — over budget (GWT-3)
card_gate("JPCP", completeness=0.5, floor=0.8)  # DENY — model card too incomplete
```

These encode safety that a policy bundle can make *stricter* but never loosen — an unsigned
artifact is always denied deployment regardless of what the YAML/Rego says.

## Rollout: `mode: monitor` per rule, `gates:` per built-in gate (ADR 0029 decision 4)

Introduce a policy without risking an outage. A rule with `mode: monitor` is evaluated and its
would-be effect is audited (`policy_monitor:<action>`, with `would_effect` and `enforced: false`),
but it never blocks; evaluation carries on to the next rule. Flip the line to `mode: enforce` (the
default) when the audit trail shows it would not have blocked anything legitimate. An unrecognized
`mode` fails closed to `enforce`.

```yaml
policies:
  - name: no-prod-friday
    action: manual_promote
    when: "to_alias == 'Production'"
    effect: deny
    mode: monitor          # observe first
```

The engine's built-in domain gates are **off by default** and armed per gate, also with a mode:

```yaml
gates:
  supply_chain: enforce            # unsigned artifact -> deny (verify-before-load, promote)
  budget: monitor                  # over-budget GPU-hours in `exa pipeline run --project`
  model_card: {mode: enforce, floor: 0.9}   # completeness floor at `exa pipeline promote`
  datasheet: {mode: enforce, floor: 1.0}    # training data documented (ADR 0079 d6)
  residency: enforce               # train only where the data may be processed (D6)
```

or `EXAMLOPS_POLICY_GATES=supply_chain=enforce,budget=monitor` (wins over the file). Where they run:

| Gate | Decision point | Denied outcome |
|---|---|---|
| `supply_chain` | `supplychain.verify_before_load` (Ray Serve load path, `exa models verify`) and `exa pipeline promote` (a version with no signature on record) | load refused / exit 1; `--mode warn` cannot loosen an enforced gate |
| `budget` | `exa pipeline run` for `--project` (or the model's project): consumption in the budget's period + the request against the project's GPU-hour budget | exit 1 before anything launches |
| `model_card` | `exa pipeline promote` | exit 1 below the floor (default 0.8); an unmeasurable card counts as 0 |
| `datasheet` | `exa pipeline promote` — every dataset the model's YAML (active pack) declares | exit 1 when a dataset has no datasheet or one below the floor (default 1.0: every required question answered); a model that declares no dataset is refused |
| `residency` | `exa pipeline run --cluster <name\|auto>` | exit 1 before the run is targeted when a dataset's datasheet restricts `distribution.residency` and the cluster's `region:` (in `clusters.yaml`) is not in the list or not declared; with `auto` under `enforce`, non-compliant clusters are removed from placement's candidates (under `monitor` placement is untouched and the would-deny is audited). The datasets checked are `--dataset`, else the model YAML's, else — a run with no `--model` trains every model — every dataset the pack declares; a set that cannot be determined (missing/unreadable model YAML, or a `--registry` without `--dataset`) is refused |

Only the GPU-hour dimension of a budget is gated; the USD budget is still only reported by
`exa finops budget status`.

## Every mutating `exa` command decides (ADR 0079 decision 2)

A policy rule can govern **any** mutating `exa` command, not only the ones that grew a gate of
their own. The root CLI group wraps every leaf command (`examlops.cli._policy_hook`): a command
whose surface tier is `admin`, `destructive` or `cli_only` — and any third-party plugin command —
consults `policy.decide` *before its body runs*. **Gated is the default**, so a command added
tomorrow is governed the day it ships; nobody has to remember to call a gate.

The action name is the command path with spaces and hyphens as underscores: `exa project grant`
decides as `project_grant`, `exa secrets set` as `secrets_set`, `exa serve traffic` as
`serve_traffic`, `exa models rollback run` as `models_rollback_run`. Where another door already
has a name, the CLI reuses it: `exa approvals approve` / `reject` decide as `approval_approve` /
`approval_reject`, the names the dashboard and the control plane use.

The decision input carries the command's scalar parameters by name (`relation`, `obj`, `name`, …),
the shared vocabulary (`model` from `model`/`model_id`/`model_name`; `project`, `cluster`,
`dataset`, `version`, `tenant`), `target` (the first string argument), and — last, so a parameter
cannot impersonate them — `command`, `tier`, `actor`, `principal_kind` and `via: cli`. Parameters
whose names look like credentials or payloads (`value`, `secret`, `token`, `password`, `api_key`,
`content`, `body`, …) never enter the context, so they never reach the audit log; neither does a
parameter whose own help opens by naming a credential (`exa gateway chat --key` — "Virtual key to
authenticate with") or that hides its input. The context is bounded (32 keys, 256 characters a
value).

```yaml
policies:
  - name: owners-need-four-eyes
    action: project_grant
    when: "relation == 'owner'"
    effect: require_approval
  - name: no-agent-secret-writes
    action: secrets_set
    when: "principal_kind == 'agent'"
    effect: deny
  - name: traffic-freeze
    action: serve_traffic
    effect: deny
    mode: monitor            # observe first
```

Contract — the same as every other gate: no rule → the command is byte-identical and writes no
audit row; `deny` → exit 1 naming the rule, the body never runs; `require_approval` → a default-no
confirmation (a human may answer it or pass the global `--yes` — `-o json` alone is an output
format, not consent, and is refused; an agent principal is refused with `plan_required`; a
declined approval exits 1 and changes nothing) and the approved command runs with the approval
acknowledged, so a control plane that decides the same action again sees it — the approval is
audited as `policy_approval:<action>` (`approved_by`, and `consent: prompt|--yes`), as the HTTP
doors record theirs;
`mode: monitor` → audited, never blocks; an engine failure → deny, audited as
`policy_unavailable:<action>`. `--dry-run` is never gated: a preview changes nothing.

Commands whose body already decides under an established name are left to do so, so one rule is
not evaluated twice:

| Action | Command |
|---|---|
| `retrain` | `exa retrain` |
| `manual_promote` | `exa pipeline promote` |
| `connect_cluster` / `cluster_approve` / `cluster_reject` | `exa hpc connect` / `approve` / `reject` |
| `model_sign` | `exa models sign` |
| `secret_rotate` | `exa secrets rotate` |
| `project_delete` / `project_archive` / `project_remove_member` | `exa project delete` / `archive` / `remove-member` |
| `agent_promote` | `exa agent alias set` / `rollback` |
| `tool_grant_change` | `exa broker grant set` / `remove` |
| `genai_app_promote` | `exa genai-app promote` |
| `pipeline_compile` | `exa pipeline compile` |
| `autopilot_trigger` / `autopilot_promote` | `exa autopilot run` / `follow` (per model, inside the cycle) |

Exempt, each with its reason in `_policy_hook.EXEMPT`: sensitive *reads* at admin tier (`exa audit
export`, `exa secrets list`, `exa backup verify`, …), changes to the operator's own client
configuration (`exa config set/unset/use`, `exa project use`), the operator's own session
(`exa auth login/logout/token`) and hosts of an agent surface (`exa chat`, `exa mcp serve`), whose
every write is its own `agent_write` decision. `read`-tier commands are not mutations.

`tests/unit/test_policy_cli_hook.py` is the forcing guard: it fails when a table names a command
that no longer exists, when a self-gated command's source never calls the policy layer, when two
commands would derive one action name, or when any leaf of the live tree resolved through the root
group is missing the hook.

## Pluggable engines: `exa.providers.policy`

`policy_engine.evaluate` (the built-in gates and `exa policy eval`) asks the engine named by
`EXAMLOPS_POLICY_ENGINE`: `yaml` (default), `opa`, or the name of a plugin registered under the
`exa.providers.policy` entry-point group. A plugin is a `examlops.providers.Provider` whose
`compute({"decision", "action", "subject", "resource", "tenant", "context"})` returns
`{"effect": "allow|deny|require_approval", "reasons": [...]}` (an unknown effect is a deny). A plugin
that cannot be loaded degrades to the YAML engine with a logged warning and appears with its error in
`exa providers list --domain policy`. The per-command gates above (`manual_promote`, ...) use the
YAML rules directly via `policy.decide`; a third-party engine governs the engine's own gates.

## Rego bundle and its tests

`platform/infra/policy-bundle/` ships a small example bundle (`supply_chain`, `budget`,
`model_card`) with Rego unit tests. Copy `examlops/` to `~/.config/examlops/bundle/` and set
`EXAMLOPS_POLICY_ENGINE=opa`. `opa test platform/infra/policy-bundle/examlops -v` runs the tests; the
unit suite runs them when an `opa` binary is on `PATH` and otherwise only validates the bundle's
structure (no new dependency).

## Datasheets (ADR 0079 decision 6)

`exa cards lint <dataset> --revision <rev>` exits 1 when the dataset card (Croissant record) fails the
spec check or carries undocumented fields, an unpinned version or no provenance revision.

The card carries what the platform can derive from data. What only a person can answer — the
Gebru et al. *Datasheets for Datasets* questionnaire (arXiv:1803.09010) — is a file the operator
authors and reviews like code, `<dataset>.yaml` in `EXAMLOPS_DATASHEETS_DIR`, else in the active
use-case pack's `datasheets/`, else in `<config dir>/datasheets/`. The seven sections are modelled
(motivation, composition, collection process, preprocessing, uses, distribution, maintenance) with
the principal questions of each as required keys (`examlops.cards.datasheet.SECTIONS`); an answer
that is missing, empty or a placeholder (`TODO`, `TBD`, `not provided`, …) is unanswered.

```bash
exa cards lint MyDataset --template > usecases/<pack>/datasheets/MyDataset.yaml   # skeleton
exa cards lint MyDataset --revision <rev> --datasheet    # card + questionnaire; exit 1 on a gap
```

A policy can then **require** documented data before promotion: arm the `datasheet` gate (table
above). The same file may restrict where the data may be processed —
`distribution.residency: [eu]` — which the `residency` gate enforces against the `region:` a
cluster declares in `clusters.yaml`:

```yaml
# clusters.yaml
clusters:
  hpc-eu:
    scheduler: slurm
    host: login.example.org
    region: eu
```

## Dry-run explainability

```bash
exa policy eval promotion --action promote --resource JPCP/17 \
    --set rmse_new=4.1 --set rmse_prod=5.0
# promotion: deny [yaml] — matched policy rule 'no-prod-regression' → deny
```

`--dry-run` (the default) explains without auditing; `--enforce` audits the decision.

## Signed, versioned, per-tenant bundles

The effective policy for a tenant is the base `~/.config/examlops/policy.yaml` plus an
optional `policy.<tenant>.yaml` overlay. Sign it (D3 HMAC) so its content is tamper-evident
and versioned:

```bash
exa policy bundle sign --tenant acme
# Signed policy bundle acme v1 (hash 3f9c…)

exa policy bundle verify --tenant acme
# Policy bundle acme is valid.

exa policy bundle list
```

`verify` recomputes the content hash and checks the signature, flagging a tampered or
unsigned bundle (and exits 1 for CI).

## Dashboard and control-plane routes (ADR 0079 decision 2)

The CLI gates above used to be the only door a `policy.yaml` governed: the same mutation made from
the dashboard, or by a direct control-plane call, never consulted it. Both services now do, through
one shared verdict (`examlops.policy.http_gate.evaluate`) that follows the CLI's contract:

| `policy.yaml` says | HTTP answer | Audit |
|---|---|---|
| no file / no matching rule | the route behaves exactly as before | none (byte-identical) |
| `deny` | **403**, detail names the rule (`X-Policy-Rule` header on the dashboard) | `policy:<action>` under the caller's identity |
| `require_approval` | **409** until re-sent with `X-Policy-Approved: true` | `policy:<action>`, then `policy_approval:<action>` when acknowledged |
| a rule with `mode: monitor` | never blocks | `policy_monitor:<action>` (would-effect) |
| the engine raises | **403** (fail closed) | `policy_unavailable:<action>` |

`X-Policy-Approved` is the HTTP form of the CLI's default-no confirmation prompt: the platform does
not invent an approver. On the dashboard only an **admin** may give it. The acknowledgement is an
assertion by an authenticated caller and is itself audited. `exa retrain` sends it after the
operator answered its own prompt, and the dashboard's pipeline trigger forwards it, so a
`require_approval` rule on `retrain` is confirmed once, not twice.

**Order (dashboard).** The gate is one app-level dependency (public FastAPI API, identical on
0.136 and 0.141), so it runs *before* a route's own capability check. With **no matching rule** it
allows silently and the route's own 403 fires exactly as before (a viewer on an admin route still
gets the capability answer, and no policy row is written). A matching `deny` rule now answers
first, even for a caller who lacks the capability: 403 with `X-Policy-Rule`. A `require_approval`
rule answers an admin with 409 (re-send with the acknowledgement) and anyone else with 403 - a
non-admin is never invited to approve and never slips past the rule. An unauthenticated request
still gets the normal 401. (The control plane's gate runs inside the handler, after the scope
check, so there the scope answer always comes first.)

**Action names.** A mutation with a CLI equivalent reuses its name so one rule governs both doors:
`manual_promote` (alias moves to a model alias, with `model`, `version`, `to_alias`),
`cluster_approve` / `cluster_reject` (`cluster`), `project_delete`, `project_remove_member`, and
`retrain` at the control plane (`model`, `dataset`, `dummy`). The rest are
`dashboard_<router>_<verb>` (for example `dashboard_projects_create`, `dashboard_secrets_set`,
`dashboard_containers_restart`) plus `approval_approve` / `approval_reject`. A rule can read the
path parameters, the caller (`actor`, `role`, `tenant`) and the request body's scalar fields as
`body_<field>` (fields whose names look like credentials are never put in the context):

```yaml
policies:
  - name: freeze-prod-projects
    action: dashboard_projects_create
    when: "body_name == 'prod'"
    effect: deny
  - name: four-eyes-on-retrain
    action: retrain
    effect: require_approval
```

**The route table.** Every mutating route is classified in exactly one place -
`platform/services/dashboard/backend/policy_gate.py` (`ROUTE_POLICY`) and
`platform/services/control_plane/cplane/policy_gate.py` - as *gated* (with its action) or *exempt*
(with the reason: authentication/session, read-only-by-POST, documentation content, a proxy whose
target gates, the CLI console whose subprocess already enforces policy). A test walks the live
app and fails on a mutating route that is in neither column, so a new mutation cannot ship without
someone deciding. The control plane's `/approve` / `/reject` (and their `/v1/approvals/...`
twins) decide as `approval_approve` / `approval_reject`, `/modelzoo/sync` as `modelzoo_sync`,
`PUT /modelzoo/config` as `modelzoo_config_set` and `/admin/reload` as `production_reload` — the
names `exa approvals approve|reject`, `exa modelzoo sync|config-set` and `exa production reload`
decide under — so one rule governs the terminal, the dashboard and a direct API call. The
dashboard's approve/reject routes forward an admin's `X-Policy-Approved` to the control plane.

## Auditing (R7)

Every `evaluate` (unless `--dry-run`) and every bundle sign writes an `audit_events` row —
scoped to the tenant — so the D4 tamper-evident trail records who decided what, and why.

## Related

- **examlops.policy** (ADR 0079) — the YAML evaluation core this seam wraps.
- **D3** supply-chain — the HMAC key that signs bundles + the unsigned-deny gate.
- **D4** audit — every decision is a hash-chained audit event.
- **D6** tenancy — per-tenant overlays.
- **C3/C6/C8** — the promotion/SLO/fairness signals policies gate on.
