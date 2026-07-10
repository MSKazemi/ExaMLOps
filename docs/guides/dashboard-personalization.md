# Personalization, Workspaces & Onboarding

The dashboard remembers how you like to work: a preference center, a watchlist of the entities you care
about, a first-run guided tour, and a contextual help drawer with a glossary — all self-hosted and
dependency-free.

- **Feature:** F21 · **Design:** [ADR 0072](../../design/adr/0072-dashboard-personalization-workspaces.md) ·
  **Spec:** `design/vision/specs/F21-personalization-workspaces-onboarding.md`
- **Frontend:** `lib/prefs.ts`, `lib/glossary.ts`, `pages/Preferences.tsx`,
  `components/{OnboardingTour,HelpDrawer,PinButton}.tsx`

## Preferences (`/preferences`)

Set your **default landing page** (where `/` takes you), **density**, and **language** (the F19 switcher).
Preferences are saved to this browser. Theme and status live in the sidebar.

```tsx
import { usePrefs } from '@/lib/prefs'
const { prefs, setPref } = usePrefs()
setPref('defaultLanding', '/finops')   // land on FinOps next time you open the app
```

## Watchlist

Pin the models/jobs you follow with the star button; they collect on the Preferences page.

```tsx
import { PinButton } from '@/components/PinButton'
<PinButton entity={{ type: 'models', id: 'jpcp' }} label="JPCP" />
```

```tsx
import { useWatchlist } from '@/lib/prefs'
const { pinned, isPinned, toggle } = useWatchlist()
```

## Onboarding tour

On first visit a short guided tour introduces the command palette, copilot, and personalization. It runs
**once** — completing or skipping it remembers your choice. Replay it any time from **Preferences →
Onboarding**.

## Help drawer & glossary

Press **`?`** anywhere (or the Help launcher) to open a contextual drawer with a searchable glossary of
platform terms (drift, promotion, alias, GPU-hours, canary, approval gate, …). It is focus-trapped and
fully self-hosted — no third-party help widget.

## Notes & limits

This slice persists **locally** (localStorage). Deferred (tracked in the plan): a BFF UI-state store for
**cross-device** sync, a **drag-drop widget grid** home + widget library with **persona default layouts**
(exportable to the F20 NOC kiosk), watchlist **change notifications** over F8/F12, and per-persona
flag-tied tours.

See [`docs/dashboard/architecture.md`](../dashboard/architecture.md#personalization-workspaces--onboarding-f21)
for the design diagram.
