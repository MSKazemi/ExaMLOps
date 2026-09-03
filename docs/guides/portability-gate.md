# Portability gate — parity across an execution-target change

Every gate ExaMLOps had before this one was **single-target**. `exa pipeline validate-model`
smoke-tests a model on the backend it is already running on; `exa pipeline promote --if-<metric>`
reads a metric from the run that produced it. A portability gate is inherently **two-target** — it
compares the *same* model across *two* execution targets — and nothing did that.

Meanwhile `exa models quantize` registers, signs, BOMs and audits a requantised version **with no
numeric comparison at all**. Quantisation is a *deliberate* numeric change. So a promotion that
changed the numerics shipped on a green latency check.

## The three verdicts

| Verdict | Means | Autonomous promotion? |
|---|---|---|
| `passed` | a transformation happened, fixtures ran on both targets, divergence within the model's declared tolerance | **yes** |
| `blocked` | divergence exceeded the tolerance — the *measured* value is recorded | no |
| `inert` | nothing was compared: no transformation, no fixtures, or no reachable second target | no |

**`inert` is not a pass.** This is the trap the gate exists to close: on a host where no quantizer
runs, the artefact is unchanged, so a comparator that simply diffed outputs would report **perfect
parity** and green-light the promotion having measured nothing at all. An identical result is only
evidence of parity when a transformation actually occurred.

## The gate is conditional, not a tax

A promotion that does **not** change the execution target skips the gate entirely. The target
change is read from the version string: `exa models quantize` names its output
`<version>-<method>`, so `17-awq` records that the numerics changed and `17` records that they did
not.

## Tolerance is per-model and declared

A ranking model tolerates far more numeric drift than a regression model whose output is a
physical quantity, so there is no global tolerance. Declare it in the model's YAML:

```yaml
name: JPCP
parity_tolerance: 1.0e-4     # max relative divergence permitted across a target change
```

Undeclared falls back to a deliberately tight `1e-3`: a model whose numerics move more than that
should have to say so, rather than inherit a permissive default. The tolerance is reviewed like any
other gate threshold — and per the same principle as [judge calibration](judge-calibration.md), a
tolerance that never fires is evidence it is too loose.

## Commands

```bash
# Run the gate for a quantized version against its base
exa models parity JPCP 17-awq
exa models parity JPCP 17-awq --tolerance 1e-4
exa --json models parity JPCP 17-awq
```

`exa pipeline promote` runs the gate automatically when the version being promoted changes the
execution target, and refuses on anything but `passed`. `--force` overrides and is audited, like
the eval and SLO gates beside it.

Every run records the **measured divergence**, not just the verdict, in `parity_checks` and in the
audit trail — so drift toward the tolerance boundary is visible before it crosses.

## Current state on this platform

`exa models parity` will report **`inert`** for anything quantised here today, and that is the
correct answer: `quantize_model()` does not invoke a quantizer on any path — the GPU branch differs
from the CPU branch only in that it does not warn — so the weights are unchanged and there is
nothing to compare. The gate reports what is true rather than what the function name suggests.

When a real quantizer is wired in, it sets `weights_transformed` at the point it actually
transforms the weights, and the gate starts measuring.

## Scope

This is the **quantisation arm** of ADR 0117's portability gate, which the roadmap moved into W1
because it needs no second accelerator family — quantisation changes numerics on the same host. Two
arms are deliberately out of scope and the gate reports `inert` rather than pretending:

- **the engine arm** (comparing vLLM against SGLang) needs a GPU host;
- **the accelerator-family arm** needs a second silicon family on site.

Also out of scope from ADR 0117: prefill/decode topology as runtime policy (decision 1), three-signal
routing (1b), and paired `(TTFT, TPOT)` SLOs (decision 2). The ADR is recorded as *Partially
implemented* for that reason.

## Design

- ADR 0117 — *Serving topology is runtime policy; promotion is a two-target gate* (requirement G5.8)
- `examlops.parity` — the comparator, the verdicts and the tolerance lookup
- Related: [corruption detection](corruption-detection.md), which applies the same
  *absent ≠ pass* principle to a drift signal
