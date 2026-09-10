---
description: "Discover, connect and schedule ML training across Slurm and Flux clusters with ExaMLOps: read-only discovery, sysadmin-approved admission, placement, queueing, preflight checks and GPU capacity."
---

# HPC Fleet — discover, connect, and schedule across SLURM & FLUX

exaMLOps runs training jobs on HPC clusters through a scheduler abstraction that supports
**Flux**, **Slurm**, and a **mock** (inline) backend, over either a local subprocess or SSH
transport. The *fleet* layer adds the operator workflow around it: **discover** what a
cluster offers, **connect** it under sysadmin approval, and **schedule** jobs onto it.

> **Design principle:** discovery only *proposes* a configuration. No workload ever runs
> against a cluster until a sysadmin approves it. All discovery is read-only.

## 1. Discover a cluster (`exa hpc`, read-only)

Auto-detect the scheduler on a candidate login node and see what it offers. Nothing is
connected or submitted — these commands only run side-effect-free query commands
(`flux resource list`, `sinfo`, `scontrol`, `nvidia-smi`).

```bash
# Probe a host over SSH and get a suggested configuration
exa hpc detect lxp-login
exa hpc detect lxp-login --user hpcuser --key ~/.ssh/id_ed25519

# Probe the local machine (or the env-configured transport)
exa hpc detect

# List compute nodes with CPUs / memory / GPUs and normalized state
exa hpc nodes --host lxp-login

# Persist the current inventory as a snapshot in platform.db
exa hpc nodes --host lxp-login --save --cluster lxp

# List GPU devices (model, memory, utilization, online status)
exa hpc gpus --host lxp-gpu01

# JSON for scripting / agents
exa --json hpc detect lxp-login
```

`detect` prints a **suggested config** — the scheduler it found, the transport, and the
`EXAMLOPS_HPC_*` environment variables (or, later, the `clusters.yaml` entry) you would use
to schedule on it. It does **not** apply that config.

### What discovery detects

| Scheduler | How | Reports |
|---|---|---|
| **Flux** | `flux resource list` (state-grouped, hostlist-expanded), `flux version`, `flux account` | nodes, cores, GPU counts, up/allocated/down state, flux-accounting present? |
| **Slurm** | `sinfo -N -o '%N|%c|%m|%G|%t|%P'`, `sinfo --version`, `sacctmgr` | nodes, CPUs, memory, GPU type+count (GRES), state, partition |
| **Unmanaged GPU host** | `nvidia-smi` | per-GPU model, total/used memory, utilization, online |

A real scheduler that only exposes GPU *counts* (Flux) is automatically enriched with GPU
*device* detail from `nvidia-smi` when that host is reachable.

### Node states (normalized)

`idle` · `allocated` · `mixed` · `down` · `drain` · `unknown` — mapped from each scheduler's
native state vocabulary so the fleet view is uniform.

### Extending to a new scheduler

Discovery is pluggable. Implement the `SchedulerProbe` protocol
(`available` / `capabilities` / `list_nodes` / `list_gpus`) in
`platform/infra/slurm-adapter/discovery.py` and `register_probe(MyProbe())`. The CLI,
registry, and placement layers speak only the normalized vocabulary
(`NodeInfo` / `GpuInfo` / `ClusterCaps`), so they need no changes.

## 2. Connect under sysadmin approval

A discovered cluster is *registered*, never auto-connected. It lands in the registry in a
`PENDING` state and **cannot receive jobs** until a sysadmin approves it.

```bash
# Probe a host and register it as PENDING
exa hpc connect lxp-login --name lxp --user hpcuser --key ~/.ssh/id_ed25519

# See all registered clusters and their state
exa hpc clusters

# Sysadmin: authorize (or block) scheduling on the cluster — audited
exa hpc approve lxp
exa hpc reject lxp --reason "wrong account"
```

Two sources of truth, by design:

| Source | Holds | Location |
|---|---|---|
| **`clusters.yaml`** | connection *definition* (scheduler, transport, host, ssh user/key/port) — human-editable | `~/.config/examlops/clusters.yaml` (override `EXAMLOPS_HPC_REGISTRY`) |
| **`hpc_clusters` table** | governance *state* (`PENDING`/`ACTIVE`/`REJECTED`), who requested/approved, last capabilities | `platform.db` |

Merely listing a cluster in `clusters.yaml` grants nothing — it still starts `PENDING`.
Re-probing an already-approved cluster refreshes its capabilities but never silently
de-authorizes it. SSH keys are referenced by path (and fingerprinted for the audit trail),
never inlined.

**Dashboard.** The Facility console shows a **Fleet** panel with every cluster, its state,
and live capacity (idle/total GPUs, utilization %, GPU-hours used); admins get inline
**Approve** / **Reject** actions (`POST /api/v1/facility/fleet/{name}/approve|reject`,
admin-gated, audited). Viewers see the list read-only.

Once a cluster is `ACTIVE`, exaMLOps resolves it into the `EXAMLOPS_HPC_*` environment the
scheduler adapter already reads — so `exa pipeline run --cluster lxp` (Phase 35c) targets it
without any manual env plumbing.

## 3. Schedule with placement, queue & preflight

Once clusters are `ACTIVE`, exaMLOps can pick the best one for a job, show the live queue,
and fail fast before submitting.

```bash
# Which ACTIVE cluster should run a 4-GPU job? (least-loaded that fits)
exa hpc place --gpus 4

# Live scheduler queue for a cluster (squeue / flux jobs, normalized)
exa hpc queue --cluster lxp

# Tracked submissions from platform.db (hpc_jobs)
exa hpc jobs --model JPCP

# Fail-fast pre-submit checks (exit 1 on any failure) — safe as a CI gate
exa hpc preflight lxp --gpus 4

# Run training on a specific cluster, or let placement choose
exa pipeline run --model JPCP --dataset PM100Dataset --cluster lxp
exa pipeline run --model JPCP --dataset PM100Dataset --cluster auto --gpus 4
```

**Placement** (`exa hpc place`, and `--cluster auto`) scores every `ACTIVE` cluster by
matching headroom — it filters to clusters that *can* satisfy the ask (by total GPUs/nodes),
then prefers the one with the most idle capacity, and explains its choice (e.g. *"chose
flux@lxp — 6/8 idle GPUs, 2/2 idle nodes"*). It reads node snapshots from `hpc_nodes`
(refresh them with `exa hpc nodes --save --cluster <n>`), falling back to each cluster's
declared capabilities when no snapshot exists.

`--cluster <name>` refuses any cluster that is not `ACTIVE`, so the approval gate is enforced
on the scheduling path too, not just at connect time.

## 4. Capacity, cost & agent access

```bash
# Per-cluster GPU capacity, utilization, GPU-hours used and cost (ACTIVE clusters)
exa hpc capacity
```

`exa hpc capacity` joins each cluster's node inventory (`hpc_nodes`) with its consumed
GPU-hours (`hpc_jobs`) to show total/idle GPUs, utilization %, GPU-hours used, and cost
(at `GPU_COST_PER_HOUR`). Energy/carbon accounting stays in `exa finops carbon` (the Green-AI
provider substrate) rather than being duplicated here.

**Agent / MCP access.** The fleet is agent-callable via MCP (`exa mcp serve`): read-only tools
`hpc_clusters`, `hpc_nodes`, `hpc_place`, `hpc_jobs` are always exposed; the mutating
`hpc_approve_cluster` is registered only when writes are enabled
(`EXAMLOPS_MCP_ALLOW_WRITES=1` / `--allow-writes`) and still writes an audit event. So an
agent can answer "which clusters are online, how many free GPUs, where should this job run?"
while approval stays human-gated by default.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_HPC_REGISTRY` | `~/.config/examlops/clusters.yaml` | Cluster-definition registry file |
| `EXAMLOPS_HPC_CLUSTER` | unset | Default cluster for commands that take `--cluster` |
| `EXAMLOPS_HPC_SSH_{HOST,USER,KEY,PORT}` | — | SSH transport for discovery/scheduling (Phase 23) |
| `EXAMLOPS_HPC_SCHEDULER` | `mock` | `mock`\|`slurm`\|`flux` (Phase 23; set for you by `--cluster`) |
| `GPU_COST_PER_HOUR` | `2.50` | Rate used by `exa hpc capacity` |

## Related

- End-to-end training on a cluster: `docs/guides/hpc-training-workflow.md`
- Scheduler abstraction (Phase 23): `docs/components/slurm-adapter.md`
- Environment variables: `docs/reference/env-vars.md`
- Command reference: `docs/reference/cli.md`
- FinOps + Green-AI carbon accounting: `docs/guides/finops-providers.md`
