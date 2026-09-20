# Platform Ops workbench — govern the platform from a notebook

ExaMLOps has a **Platform Ops** environment for managing the platform *itself* — the compute-node
cost, connections, the ExaMLOps↔bridge wiring, service config, and `platform.db` knobs — and for
**deploying calculation code**. It comes in two halves that share one governed code path:

- a **Jupyter workbench** (the reserved `platform-ops` project), and
- a **dashboard console** (Platform → Platform Ops).

Both route every change through **one façade, `examlops.platform_admin`**, so a change is
attributable, policy-gated, and on the tamper-evident audit chain — exactly like an `exa` CLI change.

## Why a façade (the "take care of the code" part)

Writing to the config files / provider store / `platform.db` *directly* from a notebook would bypass
the RBAC + policy + audit the CLI and dashboard emit. `platform_admin` closes that gap: every method
wraps the same `examlops.*` writer the CLI uses inside `authz.check → policy.decide → write_audit_event`.
Always go through `pa.*` — never edit the stores by hand.

```python
from examlops import platform_admin as pa
```

## Two tiers

| Tier | What you change | How it ships |
|---|---|---|
| **A** (default) | config: compute cost, connections, bridge UUID, `config.toml`, `platform.db` knobs — **plus** sandboxed calc code (providers) | hot, no restart, reversible, audited |
| **B** (admin) | real integration **source** (the bridge, `platform/clients`, pipeline engine) | staged → `dualgit ship` → service redeploy |

## Tier A — change the compute-node cost

```python
pa.compute_cost_card()                       # current rate card + provider methodology
pa.set_compute_cost(gpu_per_hour=3.10, cpu_per_hour=0.07)   # governed + audited
```

`exa models cost` and the dashboard FinOps view pick the new rate up immediately (it writes the
`[finops.cost]` block the cost estimator reads).

## Tier A — write Python and deploy it

A **provider** is a swappable calculation (cost / carbon / drift / promotion …). `deploy_provider`
runs your Python through the **AST security gate** *before* it reaches disk (imports, dunders and
dangerous calls are rejected), persists it, and activates it — no restart.

```python
code = '''
class WeekendDiscountCost(Provider):
    name = "weekend-discount"
    def compute(self, inputs):
        return {"cost_usd": round(inputs.get("gpu_hours", 0) * inputs.get("gpu_rate", 2.5) * 0.8, 4)}
'''
pa.deploy_provider("cost", "weekend-discount", code)   # AST-gated, activated, audited
pa.list_authored_providers()
```

Provenance then shows in `exa providers list`, the dashboard **Providers** console, and FinOps.

## Tier A — other config surfaces

```python
pa.set_connection("dataplane-prod", "dataplane",
                  config={"endpoint": "http://dataplane:8000"}, secret_value="…")  # secret → secrets store, never audited
pa.set_bridge_uuid("JPCP")                 # assign the Dataplane bus UUID if missing (idempotent)
pa.set_bridge_uuid("JPCP", regenerate=True)  # rotate it (HPC teams must update their config)
pa.set_knob("traffic", "JPCP", {"Production": 90, "Canary": 10})
pa.set_config(control_plane_url="http://control-plane:8002")   # token values are redacted in audit
```

## Tier B — edit the ExaMLOps↔bridge / data-plane source (admin)

Editing real integration code is deliberately **not** hot-applied. In an admin `platform-ops`
workbench, `platform/clients/` is mounted read-write; edit the file, then record the intent:

```python
out = pa.propose_source_change(["platform/clients/dataplane_bus_bridge.py"], "tune bridge retry/backoff")
print(out["result"]["next_steps"])   # exact ship + redeploy steps (commit via `dualgit ship` → redeploy the service)
```

`propose_source_change` records a governed, audited intent and returns the steps — it does **not**
commit, push, or restart anything. Ship it with `dualgit doctor` → `dualgit ship`, then redeploy the affected service.

## See the results — the change feed

Every change above is on the audit chain and appears in the dashboard **Platform Ops** console. From
Python:

```python
for row in pa.recent_changes(limit=15):
    print(row["ts"], row["actor"], row["action"], row["target"])
```

## Governance summary

| | Tier A | Tier B |
|---|---|---|
| Who | `platform.<domain>` capability (authz) | admin (`owner` on `platform:core`) |
| Code safety | AST trust-tier gate before activation | git review + `dualgit doctor` + preflight |
| Policy | `policy.decide` can `require_approval` (re-call with `approve=True`) | staged, not auto-shipped |
| Audit | hash-chained `audit_events`, attributed to you | commit + `platform_source_change` row |
| Reversible | provider deactivate / config revert (hot) | git revert + redeploy |

## Project per model

Related: `exa modelzoo adopt` provisions one project per Zoo model (storage · budget · workbench ·
pipelines), idempotently, as the default — while a project can still hold several models via
`exa project assign`.

```bash
exa modelzoo adopt JPCP            # one project for the JPCP model
exa modelzoo adopt --all --dry-run # preview backfill of every model
```

See ADR 0098 for the design rationale.
