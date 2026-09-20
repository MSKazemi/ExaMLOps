# ExaMLOps example Rego bundle (ADR 0029)

A small, tested starting point for sites that run OPA. `RegoPolicyEngine` evaluates
`data.examlops.<decision>.allow` with the decision's flattened context as `input`, one package per
governed decision:

| Package | Input | Allows when |
|---|---|---|
| `examlops.supply_chain` | `signed` | the artifact's signature verified |
| `examlops.budget` | `gpu_hours`, `budget` | requested GPU-hours are within the budget |
| `examlops.model_card` | `completeness`, optional `floor` | completeness meets the floor (0.8 default) |

Use it:

    mkdir -p ~/.config/examlops/bundle && cp -r examlops ~/.config/examlops/bundle/
    export EXAMLOPS_POLICY_ENGINE=opa          # needs the `opa` binary; falls back to YAML without it
    exa policy eval supply_chain --action deploy --set signed=true --dry-run

Test it (needs `opa`; the unit suite skips this when the binary is absent):

    opa test examlops -v
