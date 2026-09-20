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

## Auditing (R7)

Every `evaluate` (unless `--dry-run`) and every bundle sign writes an `audit_events` row —
scoped to the tenant — so the D4 tamper-evident trail records who decided what, and why.

## Related

- **examlops.policy** (ADR 0079) — the YAML evaluation core this seam wraps.
- **D3** supply-chain — the HMAC key that signs bundles + the unsigned-deny gate.
- **D4** audit — every decision is a hash-chained audit event.
- **D6** tenancy — per-tenant overlays.
- **C3/C6/C8** — the promotion/SLO/fairness signals policies gate on.
