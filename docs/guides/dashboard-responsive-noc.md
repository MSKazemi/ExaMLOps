# Responsive, Multi-Device & NOC Wall

The dashboard adapts across form factors — laptop to ultrawide — and ships a **NOC/wall kiosk** mode, a
pop-out for live panels, and a print stylesheet for clean reports. All dependency-free.

- **Feature:** F20 · **Design:** [ADR 0069](../../design/adr/0069-dashboard-responsive-multidevice-noc.md) ·
  **Spec:** `design/vision/specs/F20-responsive-multidevice-noc.md`
- **Frontend:** `lib/responsive.ts`, `lib/noc.ts`, `pages/NocWall.tsx`, `components/PopOutButton.tsx`,
  `index.css` (print)

## Responsive breakpoints

```ts
import { useBreakpoint, useMediaQuery } from '@/lib/responsive'

const bp = useBreakpoint()          // 'laptop' | 'desktop' | 'wide' | 'ultrawide'
const wide = useMediaQuery('(min-width: 1920px)')
```

`matchBreakpoint(width)` is the pure classifier behind `useBreakpoint`; grids (F17) and charts (F4) reflow
across these breakpoints.

## NOC / wall kiosk mode

Open **`/noc`** (e.g. on a wall display or `?kiosk=1`). It is a full-screen, dark, big-font view that
**auto-rotates** curated slides — GPU spend, active alerts, estimated carbon — every 12 seconds, with a
live clock and no navigation chrome.

- It renders **inside the authenticated app**, so an unattended wall **never drops to a login** (use a
  long-lived viewer/service token — F15). An **Exit** link always returns to the app.
- Slides are composed by the pure `buildNocSlides(finops, alerts, locale)`; if a data source is
  unavailable the slide shows "—" rather than an error — a wall must never show an error page.

## Pop-out live panels

```tsx
import { PopOutButton } from '@/components/PopOutButton'

<PopOutButton url={grafanaPanelUrl} label="Pop out logs" />
```

`popOut(url)` opens the panel in its own window so an operator can watch a Grafana embed or log tail on a
second monitor while working elsewhere.

## Print reports

A `@media print` stylesheet hides everything marked `.no-print` (the nav rail, copilot launcher, pop-out
buttons) and drops backgrounds, so governance (F14) and finance (F13) pages print as clean reports. Add
`no-print` to any control that shouldn't appear on paper.

## Notes & limits

Shipped: responsive breakpoints, the NOC kiosk, pop-out, and the print stylesheet. Deferred (tracked in
the plan): a full **PWA** (installable, offline app shell + cached last-known data with a stale badge —
needs `vite-plugin-pwa`), touch/tablet density tuning, and richer pop-out panel wiring. Kiosk layouts
will be curated via F21.

See [`docs/dashboard/architecture.md`](../dashboard/architecture.md#responsive-multi-device--noc-wall-f20)
for the design diagram.
