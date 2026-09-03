# Carbon signals — accounting vs decision

Electricity cannot be traced from source to consumer, so several carbon-intensity metrics coexist.
They are **not interchangeable**, and using the wrong one is not a rounding error. Gorka, Rhodes &
Roald (UW–Madison, arXiv:2411.06560) measured what happens:

> *"Disconcertingly, we observe that shifting according to common metrics such as **average carbon
> emissions** can **reduce the amount of emissions allocated to the consumer doing the shifting,
> while increasing the total emissions of the power system**."*

A greener report and a worse world. ExaMLOps therefore attaches a **type** to every carbon signal
and enforces it, rather than documenting the distinction and hoping.

| Type | Also called | Safe for | Not safe for |
|---|---|---|---|
| `accounting` | attributional, average | reporting (what the EU Energy Efficiency Directive expects) | deciding *where* or *when* to run |
| `decision` | marginal, consequential | scheduling and placement | reporting — it overstates allocated emissions ≈2× |

## The type comes from the method, not from a label

`signal_type` is **derived** from `method` and cannot be set independently:

| Method | Type |
|---|---|
| `average_grid_mix`, `residual_mix`, `static_default`, `operator_supplied` | `accounting` |
| `locational_marginal`, `marginal_emissions`, `short_run_marginal` | `decision` |
| anything else | `unknown` |

An operator who could label an average feed `decision` would reintroduce exactly the error this
exists to prevent — and the endpoint's number looks identical either way. A method nobody has
classified yields `unknown`, which satisfies **neither** guard: absent beats inferred, applied
where substituting a plausible default is actively harmful.

## What is enforced

- `carbon.estimate_emissions()` **raises** on a decision signal.
- Placement **refuses** an accounting signal: the carbon objective is dropped, its weight recorded
  as zero, and `objectives_unavailable: ["carbon"]` appears on the placement record. **No default
  value is substituted** — substituting one here is the harm itself, not a convenience.
- `choose_cluster(..., strict_carbon=True)` **raises** instead of silently degrading, for a caller
  that asked for carbon-aware placement and must not quietly get carbon-blind placement.
- Both guards raise rather than warn. A warning on a correctness-critical path is read past — the
  same conclusion [judge calibration](judge-calibration.md) reached about uncalibrated judges.

## Commands

```bash
# What signal do we have, and what may it be used for?
exa finops carbon signal

# Every reported figure states its method and type
exa finops carbon estimate --gpu-hours 12
exa finops carbon record JPCP --gpu-hours 12     # stored with signal_type + signal_method
```

## Configuration

```bash
EXAMLOPS_GRID_INTENSITY_URL=https://api.example.org/intensity?zone={zone}
EXAMLOPS_GRID_INTENSITY_ZONE=DE
EXAMLOPS_GRID_INTENSITY_METHOD=average_grid_mix   # default; the safe one
```

Set `EXAMLOPS_GRID_INTENSITY_METHOD=locational_marginal` **only if the feed really is marginal**.
The default is the conservative one because defaulting to `decision` would make the harmful path
the easy one.

Degradation is honest in the same direction: when the endpoint is unset, unreachable or
unparseable, the returned signal says `method=static_default`, `source=fallback`. A fallback is
never dressed up as a live reading, and a declared-marginal endpoint that fails does **not** hand
placement a decision-typed signal nobody actually read.

## Why carbon-aware placement is usually unavailable

Most public carbon data sources publish average intensity; marginal signals generally need a
premium subscription. So on a typical deployment `exa finops carbon signal` reports
`Usable for placement: NO`. **That is the honest state of the world, now visible rather than
papered over** — not a bug, which is why the placement record names the missing objective instead
of quietly weighting it zero.

The cost of that is smaller than it looks. Sukprasert et al. (EuroSys '24, 123 regions) measured
that *"simple scheduling policies often yield most of these reductions, with more sophisticated
techniques yielding little additional benefit"* — so losing the sophisticated path in signal-poor
regions forfeits little.

## Not yet implemented

Two requirements from ADR 0112's V16 amendment are **not** met, and the ADR records that:

- **R-ec — simple-baseline dominance.** A multi-objective placement policy must be measured
  against a simple baseline on the same trace, and ship the simple one if it does not win by a
  declared margin. No such benchmark exists. This is why no built-in carbon-aware *scoring policy*
  ships here: the typing and the guards do, so nothing can use the wrong signal, but a policy that
  makes carbon claims would owe the measurement first.
- **R-ed — declining-benefit re-test and a retirement criterion.** The benefit of carbon-aware
  scheduling falls as the grid decarbonises, fastest on the European grids this platform targets.
  No cadence and no retirement threshold are defined yet.

## Design

- ADR 0112 — *Carbon signals are typed: accounting vs decision* (requirements G9.3 · G9.4 · G9.5)
- `examlops.finops.carbon_signal` — the type and the guards
- Related: [FinOps providers](finops-providers.md) for swapping the carbon *formula*, which is a
  different axis from what the intensity figure measures
