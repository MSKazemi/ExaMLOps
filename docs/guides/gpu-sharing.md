# GPU Sharing & Fractional Allocation (E3)

> Next-Gen 40 · feature **E3** · ADR 0030 · spec `design/vision/specs/E3-gpu-sharing.md`

E3 makes **fractional GPU allocation** first-class across the scheduler abstraction and
the K8s serving path. A workload asks for a *fraction* of a GPU (or a named MIG profile);
the platform picks the best available mechanism for the cluster's capabilities, bin-packs
fractional requests onto whole GPUs, surfaces the isolation level **honestly**, and
accounts fractional GPU-hours.

## Mechanisms (strongest → weakest isolation)

| Mechanism | Isolation | Notes |
|---|---|---|
| **mig** | hardware | NVIDIA Multi-Instance GPU — memory/SM partitioned |
| **timeslice** | soft | time-sliced sharing — no memory/SM partition |
| **whole** | exclusive | a full GPU (fraction ≥ 1 or fallback) |

## Honest fallback

If a sub-1.0 fraction is requested but the cluster supports **no** fractional mechanism,
the request is rounded up to a **whole GPU** and the wasted capacity is surfaced — the
platform never pretends a share happened when it didn't:

```bash
exa hpc gpu-share plan JPCP --fraction 0.25
# JPCP: whole (isolation: exclusive) — 1.00 GPU
#   no fractional support — rounded 0.25 up to a whole GPU (75% wasted)
#   ⚠ 75% GPU capacity wasted by this fallback.
```

With MIG or time-slicing available:

```bash
exa hpc gpu-share plan JPCP --fraction 0.25 --mig-capable      # → MIG (hardware isolation)
exa hpc gpu-share plan JPCP --fraction 0.5 --timeslice         # → timeslice (SOFT isolation)
exa hpc gpu-share plan JPCP --mig 2g.10gb --mig-capable        # → exact MIG profile
```

## Bin-packing

```bash
exa hpc gpu-share pack --ask a:0.5 --ask b:0.3 --ask c:0.4 --gpus 2 --timeslice
# packs a+? onto GPU 0 and the rest onto GPU 1, reporting GPUs used and any unplaced asks
```

First-fit-decreasing packing keeps GPU count low; asks that don't fit are reported as
`unplaced` rather than silently dropped. Mechanisms are never mixed on one physical GPU.

## Declaring what a cluster can share

Mechanisms are never assumed. A registered cluster (`exa hpc connect`) declares them in its
`capabilities` in `clusters.yaml`; a cluster that declares nothing gets the whole-GPU fallback:

```yaml
clusters:
  gpu-cluster:
    scheduler: slurm
    capabilities:
      total_gpus: 16
      mig_profiles: [1g.5gb, 2g.10gb, 3g.20gb, 7g.40gb]
      mig_gres_types: {1g.5gb: a100_1g.5gb}   # Slurm gres.conf type names, per site
      shards_per_gpu: 8                        # Slurm gres/shard per GPU (time-slicing)
      flux_mig_properties: {1g.5gb: mig-1g}   # Flux: node property exposing that slice as a GPU
```

`exa hpc gpu-share plan --cluster <name>` plans against those declared capabilities and scheduler
instead of the `--mig-capable` / `--timeslice` flags, and `--scheduler slurm|flux` shows the
resources the ask maps to:

```bash
exa hpc gpu-share plan JPCP --fraction 0.1 --cluster gpu-cluster
# JPCP: mig (isolation: hardware) — 0.14 GPU
#   MIG 1g.5gb snapped up from 0.10
#   slurm resources: gres=gpu:a100_1g.5gb:1
#   ⚠ 4% GPU capacity wasted by this allocation (rounded up).
```

## Placement (scheduler-neutral ask)

`examlops.hpc_placement.ResourceAsk` carries `gpu_fraction` and `mig_profile` next to `gpus`. A
hardware profile with `--gpu-fraction` / `--mig-profile` (ADR 0157) resolves into that ask, so
`exa pipeline run --hardware-profile <p> --cluster auto` scores each cluster by what it would
really allocate: a cluster that has to round the fraction up to a whole GPU spends more of its
headroom than one that can slice it. Each candidate carries a `gpu_sharing` entry (mechanism,
isolation, allocated and wasted fraction), and the placement reason names the chosen mechanism
and its isolation level. A whole-GPU ask (the default) places exactly as before.

## Slurm and Flux (HPC)

When `exa pipeline run` has a fractional ask, it passes `EXAMLOPS_HPC_GPU_FRACTION` /
`EXAMLOPS_HPC_MIG_PROFILE` to the run, and `--cluster` passes the cluster's declared sharing as
`EXAMLOPS_HPC_GPU_SHARING` (always set, `{}` when none). The training flow maps the ask onto the
scheduler before `sbatch` / `flux batch`:

| Scheduler | MIG (hardware isolation) | Time-slice (soft isolation) |
|---|---|---|
| Slurm | `--gres=gpu:<type>:<n>` (`mig_gres_types`, else the profile name) | `--gres=shard:<k>`, `k = ceil(n × fraction × shards_per_gpu)` |
| Flux | `-g<n> --requires=<property>` (`flux_mig_properties`) | not expressible: whole GPU |

Slurm's `--gres` is a **per-node** count, so `<n>` above is GPUs per node: a 4-GPU job over
`nodes=2` asks each node for `gpu:<type>:2`. A GPU total that does not divide evenly over the
nodes cannot be expressed as a per-node GRES and falls back to whole GPUs with a warning; a node
*range* (`nodes=2-4`) stops the submission.

If a mechanism is not declared, or cannot be expressed (Flux time-slicing, a MIG property that
would overwrite an existing `--requires`), the job gets a **whole GPU** and a `[gpu-sharing]
WARNING` line with the wasted share. A fraction the scheduler cannot enforce is never passed off
as one it did. A malformed ask (unknown MIG profile, fraction outside (0, 1], non-numeric env)
stops the submission.

The submitted job is linked to its allocation: a `gpu_allocations` row with the job id and
scheduler, plus a `gpu_allocation_recorded` audit event in the same transaction.

## Kubernetes serving (KServe)

A model YAML can declare a top-level `gpu_sharing:` block. The KServe renderer then puts the
matching extended-resource limit on the model-server container (the predictor for an
`InferenceService`, and each canary predictor too, or the `main` container of an
`LLMInferenceService`), plus `examlops.io/gpu-*` annotations stating the mechanism, isolation and
fraction:

```yaml
gpu_sharing:
  fraction: 0.25           # (0, 1]; defaults to autoscale.gpu_fraction when omitted
  mig_profile: 1g.5gb      # optional; implies mechanism: mig
  mechanism: mig           # mig | timeslice | hami (default: mig with a profile, else timeslice)
  timeslice_resource: nvidia.com/gpu   # set nvidia.com/gpu.shared if the plugin renames
```

| Mechanism | Container limits | Isolation |
|---|---|---|
| `mig` | `nvidia.com/mig-<profile>: 1` (GPU Operator *mixed* strategy); with no profile the fraction snaps up to the smallest one that fits | hardware |
| `timeslice` | `nvidia.com/gpu: 1` on a time-sliced device plugin; the fraction is **not enforced**, only recorded | soft |
| `hami` | `nvidia.com/gpu: 1`, `nvidia.com/gpumem-percentage` and `nvidia.com/gpucores` at the fraction | soft, enforced by HAMi |

The block is validated before rendering: unknown keys, a MIG profile smaller than the declared
fraction, or a fraction that disagrees with `autoscale.gpu_fraction` fail the render rather than
producing a different GPU shape. So does a generative model whose engine sets
`tensor_parallel_size` or `pipeline_parallel_size` above 1: a GPU share is one slice or device per
container, and vLLM sharded over several GPUs could never start in it (`data_parallel_size`
replicas may each take a share). A model with no block and no sub-1.0 `autoscale.gpu_fraction`
renders exactly as before, with no GPU request. Every rendered request is checked against the
pinned KServe CRD schema.

## Fractional accounting

`exa models cost <MODEL> --record` bills each version's scheduler job at the fraction it was
**allocated**. Schedulers count device-hours: `sacct` counts a MIG slice as one `gres/gpu`. The
cost step looks up the `gpu_allocations` row linked to that job id and records
`gpu_hours = device-hours × allocated fraction`, with `model_costs.gpu_fraction` and
`gpu_mechanism` as provenance (shown in the **GPU Share** column). Because budgets
(`exa finops`), carbon (`exa finops carbon`) and project cost roll-ups all read
`model_costs.gpu_hours`, the fraction reaches all of them. Rules:

- a job with no linked allocation is billed its device-hours unchanged. Accounting never guesses a fraction from another allocation of the same model;
- the lookup is scoped to the job's scheduler, because job ids are only unique within one: Slurm job `4711` is never billed at the fraction of a Flux job `4711`;
- a whole-GPU fallback is billed in full, because the waste was paid for;
- a planning record (`exa hpc gpu-share plan --record`) has no job id and is never used for billing.

```bash
exa hpc gpu-share accounting              # recorded allocations (tenant-filtered in SQL)
exa models cost JPCP --record             # GPU-hours scaled by each job's allocation
```

`fractional_gpu_hours(fraction, seconds)` = `fraction × wall-hours` stays available for callers
that hold a duration instead of a scheduler record.

## Programmatic use

```python
from examlops.gpu_sharing import FractionalAsk, ClusterGpuCaps, select_mechanism, bin_pack

caps = ClusterGpuCaps(supports_mig=True, mig_profiles=["1g.5gb", "2g.10gb"])
choice = select_mechanism(FractionalAsk("JPCP", fraction=0.2), caps)
print(choice.mechanism, choice.isolation, choice.wasted_fraction)

result = bin_pack([FractionalAsk("a", 0.5), FractionalAsk("b", 0.4)], gpu_count=1, caps=caps)
print(result.gpus_used, result.unplaced)
```

## Graceful degradation

All planning, mapping, rendering and accounting is pure Python. No GPU, driver, scheduler or
cluster is needed to plan or render. A cluster with no declared mechanism, or a scheduler that
cannot express one, gets whole GPUs with the waste surfaced. That result stays correct, just
less efficient.

## Not built yet

- Kubernetes **DRA** resource claims and **MPS** are not rendered. The open mechanisms rendered
  today are MIG, device-plugin time-slicing and HAMi.
- Slurm **shard** jobs: `sacct` reports `gres/shard`, not `gres/gpu`, and `exa models cost`
  reads only `gres/gpu`. A shard job's GPU-hours therefore stay unrecorded (shown as `—`) and
  are not estimated.
- The scheduler-neutral ask has a GPU fraction and a MIG profile but no GPU-memory or
  compute-share field.

## See also

- [Optimized inference engines (E2)](llm-serving-engines.md) — what runs on the allocated slice.
- [Kubernetes serving (E1)](kubernetes-serving.md) — the serving substrate.
- [HPC fleet](hpc-fleet.md) — capability discovery and capacity accounting.
