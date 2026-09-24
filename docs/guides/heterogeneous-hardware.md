# Heterogeneous Hardware & Hybrid HPC↔Cloud (E8)

> Next-Gen 40 · feature **E8** · ADR 0041 · spec `design/vision/specs/E8-heterogeneous-hardware-hybrid.md`

ExaMLOps implicitly assumed NVIDIA/CUDA GPUs on **one** HPC cluster. Real European HPC is
increasingly heterogeneous — AMD MI300, Intel Gaudi, TPU, plain CPU — and when on-prem is
saturated, bursting to cloud is common. A CUDA-and-single-cluster lock can't place work on the
best-available or cheapest hardware, and can't spill to cloud.

E8 adds a **device-abstraction + hybrid-placement** layer: a workload declares what it needs
**neutrally** (an `accelerator` + capability tags + an optional `target`), and the platform
places it on the best-available **compatible** device across HPC and cloud pools. The default
single-cluster NVIDIA path is unchanged — everything here is additive.

## Neutral device requirements

```python
from examlops.hardware import Workload, place

w = Workload(
    "train-llm",
    accelerator="amd",          # nvidia | amd | intel-gaudi | tpu | cpu
    capabilities=["fp8"],       # capability tags the pool must advertise
    target="hpc",               # hpc | cloud | None (=any)
    engine="vllm",              # engine (E2) — declares which backends it supports
    fraction=1.0,               # <1.0 requests a GPU fraction (E3)
)
placement = place(w)            # Placement(...)  or  Rejection(...)
```

## Portability — never schedule what can't run

Each engine declares its supported backends; each accelerator needs one:

| Accelerator | Backend | Example engines |
|---|---|---|
| nvidia | cuda | vllm, sglang, generic |
| amd | rocm | vllm, generic |
| intel-gaudi | ipex | ipex, generic |
| tpu | xla | generic |
| cpu | cpu | ipex, cpu, generic |

Before scheduling, `place()` runs a **portability check**. A CUDA-only engine targeting AMD is
**rejected with a clear error** — never silently placed on the wrong device:

```
$ exa hardware place train --accelerator amd --engine sglang
✗ Rejected: engine 'sglang' cannot run on amd (needs backend rocm; supports ('cuda',))
```

## Honest fallback

When the requested accelerator has no available pool, `place()` **falls back** to another
engine-compatible accelerator and says so (`fallback=True`, with a note) rather than failing or
pretending. A sub-1.0 **fraction** request on a vendor without GPU fractioning (only NVIDIA MIG
today) is honored as a **whole device**, flagged `fraction_honored=False`:

```
$ exa hardware place train --accelerator tpu --engine generic --target hpc
✓ train → pool hpc-mi300 · amd on hpc (fallback)
  requested tpu unavailable — fell back to amd
```

Among eligible pools the **cheapest** (lowest `$/hr`) wins.

## Governed cloud bursting

Bursting HPC→cloud is **opt-in** and **residency-governed** — data movement is explicit and
audited, never silent egress:

| Residency | Cloud burst |
|---|---|
| `open` | permitted |
| `eu-only` | permitted only to an EU region |
| `no-egress` / `restricted` | **blocked** + audited |

```
$ exa hardware burst train --accelerator nvidia --residency eu-only --allow-burst
⚠ Burst blocked: data-residency eu-only forbids egress to region 'us'
```

Every burst attempt — allowed or blocked — is written to `burst_events` and the tamper-evident
audit log (`source = exa-hardware`), so cross-boundary data movement is always accountable
(D4/D6).

## Per-device accounting

Device type + region flow into cost and carbon so heterogeneous fleets are comparable:

```python
from examlops.hardware import device_accounting
device_accounting(placement, hours=10)
# {"device": "amd", "region": "eu", "hours": 10, "cost": 18.0, "carbon_g": 2500.0}
```

The same device type + region are recorded on the placement decision for `exa models cost` and
`exa finops carbon`.

## CLI

```bash
# Register device pools (HPC + cloud, any accelerator):
exa hardware add-pool hpc-nvidia --target hpc --accelerator nvidia --count 8 \
    --region eu --cost-per-hour 2.5 --carbon-factor 300 --supports-fractions
exa hardware add-pool hpc-mi300  --target hpc --accelerator amd    --count 4 --region eu
exa hardware add-pool cloud-a100 --target cloud --accelerator nvidia --count 100 --region us

exa hardware pools                          # list registered pools (cheapest first)
exa hardware place train --accelerator amd --engine vllm --target hpc
exa hardware portable --engine sglang --accelerator amd
exa hardware burst train --accelerator nvidia --residency eu-only --allow-burst
exa hardware decisions                      # recent placement decisions
```

## Hardware Profiles — named, versioned resource+runtime bundles (ADR 0157)

> Phase 1 (registry + CLI) shipped 2026-09-24. Phases 2–4 (workbench/training/serving/dashboard
> consumer wiring) are not built yet — see `design/adr/0157-hardware-profiles.md`.

Every surface that requests compute (`exa workbench create`, `exa pipeline run`, `exa pipeline
distributed launch`) historically invented its own flags for the same underlying shape —
`--gpus N`, `--cpu`/`--memory-gb`, `--nodes` — with no way to name "the shape I use for JPCP
training" once and reuse it. **Hardware Profiles** fix that: a reusable, named, **versioned**
bundle — accelerator family, GPU/CPU/memory/node shape, MIG/fraction, driver/runtime tags, and
which surfaces it applies to (`workbench`/`training`/`serving`/`any`) — folded into the existing
`exa hardware` group as a `profile` subcommand, since a profile is simply a named, saved preset
of the same neutral device vocabulary `exa hardware place` already uses.

A profile is **sugar over the existing seams, not a fourth resource vocabulary**: it resolves
into `examlops.admission_seam.request.Resources` (with thin adapters to `hpc_placement.ResourceAsk`
and `examlops.hardware.Workload`) — the exact shapes `--gpus 2`/`exa hardware place` already
reach.

```bash
# Create version 1 and point the 'active' label at it (versions are immutable — never edited):
exa hardware profile set gpu-small --accelerator-family nvidia --gpu 1 --cpu 4 \
    --memory-gb 16 --applicability training,workbench

# A second `set` on the same name creates version 2; version 1 stays retrievable:
exa hardware profile set gpu-small --accelerator-family nvidia --gpu 2 --cpu 8 --memory-gb 32
exa hardware profile show gpu-small --version 1     # still there

exa hardware profile list --applicability training  # filter by applicability
```

### Resolution never fabricates a capability (ADR 0157 decision 5)

`exa hardware profile resolve <name> --cluster <cluster>` checks a profile against a target's
*live* capacity (the same `hpc_placement.can_satisfy` + node snapshot every placement decision
already uses) and reports one of four honest outcomes:

| Status | Meaning |
|---|---|
| `unchecked` | No `--cluster` given — the raw ask is returned with no capability claim. |
| `verified` | A live node snapshot confirms the coarse ask *and* every named field (GPU model hint, MIG profile). |
| `degraded` | The coarse ask (GPU count/CPU/nodes) is satisfiable, but a finer claim (model hint, MIG profile) isn't confirmed by what discovery reported — resolution proceeds, the unconfirmed field is named, never silently assumed true. |
| `unresolvable` | The ask exceeds the target's *total* capacity — refused, never scheduled on a smaller ask nobody asked for. |

```bash
$ exa hardware profile resolve gpu-small --cluster lxp --for training
✓ gpu-small@2: verified — 'lxp' snapshot satisfies the ask and confirms every named field
```

### Deletion (ADR 0157 GWT-4)

`exa hardware profile delete <name> [--version N] [--yes]` prompts for confirmation like
`workbench delete`. Deleting a single version that the `active` label currently points at
leaves the label **dangling with a warning** — it is never silently re-pointed to some other
version, so a stale name can't quietly start meaning something different:

```bash
$ exa hardware profile delete gpu-small --version 1 --yes
✓ Deleted hardware profile 'gpu-small' version 1 (1 row(s))
⚠ label 'active' pointed at version 1, which no longer exists — it is now dangling ...
```

Omitting `--version` deletes the whole name — every version and every label.

### What's not built yet

Nothing yet *consumes* a profile — no `exa workbench create --hardware-profile`, no
`exa pipeline run --hardware-profile`, no `resources.hardware_profile` in a model YAML, and no
dashboard surface. This is a registry + CLI slice (spec Phase 1); the consumer wiring is
Phase 2 (workbench), Phase 3 (training/serving), and Phase 4 (dashboard).

## Graceful degradation

The whole layer runs on the standard library — device pools, placement, portability, fraction
fallback, residency checks, and accounting need no vendor SDK. CI exercises it with CPU +
mocked device pools; real ROCm/Gaudi/TPU paths are capability-gated where the hardware exists.

## Related

- **Phase 23** scheduler abstraction (Flux/Slurm) — the HPC pools this places across.
- **E2** engines — declare the backends portability checks against.
- **E3** fractional GPUs — per-vendor fractioning honored (MIG) or falling back honestly.
- **E1** K8s serving — the cloud burst target.
- **D6** residency / **D4** audit — govern and record cloud bursting.
- **FinOps / Green-AI** — per-device cost + carbon accounting.
- **ADR 0157** Hardware Profiles — named, versioned presets of this same neutral vocabulary.
