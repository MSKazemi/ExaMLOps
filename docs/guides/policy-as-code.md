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
