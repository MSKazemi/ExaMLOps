# Visualization System

The **visualization system** is the dashboard's in-product chart toolkit — a small set of reusable,
themed, accessible primitives that the domain consoles (MLOps, Facility, FinOps, Fairness) compose.
It is **dependency-free** (self-contained SVG/CSS) and colour-blind-safe: every chart's colour comes
from the F3 status tokens and is always paired with a label or value.

- **Feature:** F4 · **Design:** [ADR 0055](../../design/adr/0055-dashboard-visualization-system.md) ·
  **Spec:** `design/vision/specs/F4-visualization-system.md`
- **Code:** `platform/services/dashboard/frontend/src/lib/viz.ts` +
  `platform/services/dashboard/frontend/src/components/viz/`

## When to use F4 vs F5 (Grafana)

| Use… | For… |
|---|---|
| **F4** (this toolkit) | KPI tiles, in-context distributions, A/B confidence intervals, small inline charts tied to an entity — data the app already has in hand. |
| **F5** (embedded Grafana) | Heavy, high-cardinality **time-series** (metrics over hours/days), owned by Grafana. See [dashboard-mlops-console](./dashboard-mlops-console.md) / the Overview "Live Metrics" panel. |

Rule of thumb: if Grafana already renders it, embed it (F5). If it's a small, entity-scoped chart of
data the page fetched, use F4.

## Primitives

### `<KpiTile value delta trend threshold/>`

A KPI card: big value, optional signed delta, optional inline sparkline (`trend`), and **threshold
colouring**. Pass `threshold={{ warn, crit }}` and a `direction` (`higher-worse` default, or
`lower-worse` for accuracy/throughput) to tint the value ok/warn/crit. Example (Facility console):

```tsx
<KpiTile label="Queue depth" value={ov.queueDepth} icon={Clock} threshold={{ warn: 5, crit: 20 }} />
```

### `<Distribution values bins/>`

A histogram of a numeric series (e.g. drift predictions). Renders themed SVG bars plus a data-table
fallback listing each bin's range and count.

### `<Uncertainty variants=[{label, mean, ci}]/>`

A dot-and-whisker chart: each variant's mean as a dot with its confidence-interval error bar — so A/B
and eval results are shown **with** their uncertainty, never as bare point estimates.

### `<ChartFrame ariaLabel title table/>`

The accessibility wrapper every chart uses (required, F4 R7 / F18). It gives the chart an
`aria-label` and always renders a keyboard-reachable **data-table fallback** in a `<details>`
disclosure — so the information exists as more than pixels. `<Distribution>` and `<Uncertainty>` wrap
themselves in it; build new charts the same way.

## Pure helpers (`lib/viz.ts`)

All chart math lives here so it is unit-testable and shared: `thresholdTone()`, `histogram()`,
`sparklinePoints()`, `ciLabel()`, `formatDelta()`, `toneStatus()` (maps a viz tone to an F3 status
token). Prefer adding new chart math here (tested) over inlining it in a component.

## Accessibility

Every chart **must** be wrapped in `<ChartFrame>` (or provide an equivalent aria-label + data-table).
Colour is never the only signal — thresholds also change the numeric value's prominence, and the data
table is always present. This satisfies the F18 accessibility baseline.

## Notes & limits

This slice ships the core primitives (KPI, distribution, uncertainty, the a11y frame) and wires
`<KpiTile>` into the Facility console. The richer F4 surfaces from the spec — `@xyflow` lineage &
topology graphs, brush-zoom / cross-chart hover-sync / click-to-drill, PNG/CSV export, and
worker-offloaded layout for large graphs — build on these and are tracked in the dashboard-nextgen
plan.
