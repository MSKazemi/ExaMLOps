# `exa` CLI — Verified Examples

One working, verified example per command. Built + kept by the dashboard/CLI QA loop (2026-07-30).
Each example was run on the LXP node against the live stack; read-only examples are shown for
mutating/outward commands (a real `retrain`/`pipeline run`/`serve reload` would fire training/deploys).

> Run any command with `-h`/`--help` for its full options, or `exa explain <cmd>` for plain-language help.

## Getting Started

```bash
exa --version
#   exa version 0.46.0

exa status            # platform snapshot: service health + pending approvals + prod models
#   ExaMLOps Service Health: Control Plane ✓ · MLflow ✓ · Prefect ✓ · Ray Serve ✓ (3 models) · Dashboard ✓
#   ✓ No pending approvals

exa doctor            # diagnose setup: config, connectivity, DB health

exa env               # effective config + the source of every value (secrets redacted)
#   config file: ~/.config/examlops/config.toml   (table of Key · Value · Source)

exa explain status    # plain-language help + examples for a command

exa docs --out docs/reference/commands.md   # generate the full command reference from the live tree

exa plugins           # list installed exa CLI plugins + load status
#   No plugins installed (entry-point group: examlops.cli_plugins).
#     → Add one via [project.entry-points."examlops.cli_plugins"] in a package.

exa config show       # print resolved config (env + ~/.config/examlops/config.toml)
exa config contexts   # list configured contexts + the active one
```

<!-- The loop appends verified examples for each remaining group below (Training/Pipelines,
     Data/Features, Models, Serving, GenAI, Monitoring, HPC/FinOps, Governance, Projects, Platform). -->
