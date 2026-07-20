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

## Fractional accounting

```bash
exa hpc gpu-share plan JPCP --fraction 0.25 --timeslice --record
exa hpc gpu-share accounting
```

`fractional_gpu_hours(fraction, seconds)` = `fraction × wall-hours`, so a 0.25-GPU job for
2 hours books 0.5 GPU-hours — feeding the FinOps / carbon accounting (`exa finops`).

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

All planning and accounting is pure-Python — no GPU or driver required. Capability flags
come from HPC discovery (`exa hpc detect/gpus`); absent them, the honest-fallback path
keeps allocations correct (whole GPUs, waste surfaced).

## See also

- [Optimized inference engines (E2)](llm-serving-engines.md) — what runs on the allocated slice.
- [Kubernetes serving (E1)](kubernetes-serving.md) — the serving substrate.
- [HPC fleet](hpc-fleet.md) — capability discovery and capacity accounting.
