---
title: Follow a cluster job
description: How ExaMLOps admits an HPC cluster, places a training job on it, runs it through Slurm or Flux, and accounts for its GPU-hours, cost and carbon.
hide:
  - navigation
  - toc
---

# Follow a cluster job

This is the **compute line**. ExaMLOps runs on the schedulers HPC centres already operate —
Slurm, Flux, or none at all in mock mode — through one scheduler-neutral adapter. A fleet
layer on top decides which clusters the platform may use at all, which one a job should go to,
and what each job cost.

<div class="xm-player" data-scene="hpc" markdown>
<ol class="xm-steps">
<li data-focus="op,detect,cluster" data-run="op-detect;detect-cluster" data-actor="Operator" data-line="human"><strong>Discover what a cluster offers.</strong> <code>exa hpc detect login.example.org</code> probes the scheduler and GPUs over SSH, read-only, and prints a suggested configuration. <code>exa hpc nodes --host login.example.org --save --cluster cluster-a</code> stores that cluster's node inventory for placement.</li>
<li data-focus="op,connect,registry" data-run="op-connect;connect-registry" data-actor="Operator" data-line="human"><strong>Register it — as PENDING.</strong> <code>exa hpc connect</code> records the connection and a fingerprint of the SSH client key given with <code>--key</code>. A new cluster starts PENDING: nothing can be scheduled on it yet.</li>
<li data-focus="sysadmin,approve,registry" data-run="sysadmin-approve;approve-registry" data-actor="Sysadmin" data-line="human"><strong>A sysadmin approves it.</strong> <code>exa hpc approve cluster-a</code> (or the dashboard's Facility console) makes it ACTIVE and records who approved it. Re-probing later never revokes that approval.</li>
<li data-focus="registry,place" data-run="registry-place" data-actor="Placement" data-line="hpc"><strong>Placement picks the best approved cluster.</strong> Among ACTIVE clusters that can fit the request, the default score prefers the most idle GPUs, then idle nodes. The scoring function is a swappable provider.</li>
<li data-focus="place,preflight" data-run="place-preflight" data-actor="Operator" data-line="hpc"><strong>Preflight checks it.</strong> <code>exa hpc preflight cluster-a --gpus 2</code> runs discovery checks over SSH and exits 1 on any failure — useful as a CI gate before a long run.</li>
<li data-focus="place,run" data-run="place-run" data-actor="Operator" data-line="control"><strong>The pipeline runs on that cluster.</strong> <code>exa pipeline run --model JPCP --dataset PM100Dataset --cluster auto</code> asks placement for a cluster, refuses anything that is not ACTIVE, and hands the flow the scheduler and SSH settings.</li>
<li data-focus="run,adapter,cluster" data-run="run-adapter;adapter-cluster" data-actor="Scheduler adapter" data-line="hpc"><strong>The adapter submits and waits.</strong> <code>sbatch</code> on Slurm or <code>flux batch</code> on Flux, then polling every 10 seconds. Every scheduler command is capped at 30 seconds; the wait gives up after 24 hours or five unknown states in a row.</li>
<li data-focus="adapter,jobs" data-run="adapter-jobs" data-actor="Training flow" data-line="observe"><strong>The job is recorded.</strong> Job id, scheduler, flow run, model, dataset, nodes, GPUs and CPUs, then state, start, end and exit code. The MLflow run is tagged with the job id, linking the model version to its compute.</li>
<li data-focus="jobs,capacity" data-run="jobs-capacity" data-actor="FinOps" data-line="observe"><strong>GPU-hours become cost.</strong> <code>exa hpc capacity</code> reports utilisation and GPU-hours; <code>exa models cost jpcp --record</code> reads the scheduler's accounting for each version and prices it.</li>
<li data-focus="capacity,carbon" data-run="capacity-carbon" data-actor="FinOps" data-line="observe"><strong>…and carbon.</strong> <code>exa finops carbon record</code> converts GPU- and CPU-hours to energy and CO₂e with a swappable provider — grid intensity, data-centre PUE and GPU power — and records which provider produced the estimate.</li>
<li data-focus="registry,prom" data-run="registry-prom" data-actor="Operator" data-line="observe"><strong>The fleet is monitored.</strong> <code>exa hpc prometheus-sd --out platform/infra/docker-compose/targets/fleet.json</code> writes Prometheus targets — node and GPU exporters from the saved node inventories, vLLM from the LLM endpoint registry — into the directory Prometheus reads, and Prometheus re-reads it every 30 seconds.</li>
</ol>
</div>

!!! note "Mock mode"
    With `EXAMLOPS_HPC_SCHEDULER=mock` (the default) training runs inline on the machine that
    runs the flow, so no job record is written and `exa models cost` reports illustrative
    placeholder figures. Set `slurm` or `flux` to schedule real jobs.

## Scheduler adapters at a glance

| | Mock | Slurm | Flux |
|---|---|---|---|
| Submit | Trains inline | `sbatch` | `flux batch` |
| Poll | Completes immediately | `squeue`, then `sacct` | `flux jobs` / event log |
| Transport | Local | Local or SSH (host-key checked) | Local or SSH (host-key checked) |
| Job record | None | `hpc_jobs` | `hpc_jobs` |
| Select with | `EXAMLOPS_HPC_SCHEDULER=mock` | `…=slurm` | `…=flux` |

## Try it

```bash
exa hpc detect login.example.org
exa hpc connect login.example.org --name cluster-a --user me --key ~/.ssh/id_ed25519
exa hpc clusters
exa hpc approve cluster-a
exa hpc place --gpus 2
exa hpc preflight cluster-a --gpus 2
exa pipeline run --model JPCP --dataset PM100Dataset --cluster auto
exa hpc jobs
exa hpc capacity
```

## Read more

- [HPC fleet](../guides/hpc-fleet.md) — discover, connect, approve, place
- [HPC training workflow](../guides/hpc-training-workflow.md) — run on a real cluster
- [Slurm adapter](../components/slurm-adapter.md)
- [FinOps providers](../guides/finops-providers.md) and [carbon signals](../guides/carbon-signals.md)
