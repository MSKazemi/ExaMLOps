# Accessibility (WCAG 2.2 AA)

The dashboard targets **WCAG 2.2 AA**. Accessibility is realized mostly through the F3 design system and
a set of reusable primitives, so new surfaces inherit it rather than retrofitting each page.

- **Feature:** F18 · **Design:** [ADR 0068](../../design/adr/0068-dashboard-accessibility.md) ·
  **Spec:** `design/vision/specs/F18-accessibility.md`
- **Frontend:** `lib/a11y.ts` (pure), `hooks/useFocusTrap.ts`, `hooks/announcer.ts` +
  `hooks/useAnnouncer.tsx`, `components/SkipLink.tsx`, `index.css` (reduced-motion)

## What ships

| Concern (WCAG) | Mechanism |
|---|---|
| Keyboard operability (R2) | `useFocusTrap` traps + restores focus in dialogs; `SkipLink` → `#main` landmark |
| Screen-reader live regions (R3) | `AnnouncerProvider` polite/assertive `aria-live`; `RouteAnnouncer` announces page changes |
| Contrast AA (R4) | `lib/a11y.ts` OKLCH→luminance contrast math; a CI test audits the real F3 tokens meet AA |
| Non-colour status (R4) | F3 status semantics — always icon **+** label, never colour alone |
| Reduced motion (R5) | global `@media (prefers-reduced-motion: reduce)` neutralizes animation/transition |
| Accessible charts (R6) | F4 `<ChartFrame>` renders a keyboard-reachable data-table fallback |

## Keyboard model

- **Skip link** — the first tabbable element; focusing it reveals a “Skip to main content” link that
  jumps to `<main id="main" tabIndex={-1}>`.
- **Dialogs** — the ⌘K command palette uses `useFocusTrap(ref, open)`: focus moves into the dialog on
  open, Tab/Shift-Tab wrap inside it, and focus **returns to the trigger** on close.
- Every actionable control is a real `<button>`/`<a>`/`<input>` — reachable and operable without a mouse.

## Announcing live content

```tsx
import { useAnnouncer } from '@/hooks/announcer'

function DriftWatcher() {
  const { announce } = useAnnouncer()
  // when an F8 push arrives:
  announce(`Drift critical on ${model}`)          // polite — heard without stealing focus
  announce('Service down', 'assertive')           // assertive — interrupts
}
```

The provider mounts one hidden `aria-live` region per priority. `announce()` clears then re-sets the
text so repeating the same message still triggers a screen-reader announcement.

## Contrast audit (the axe stand-in)

`lib/a11y.test.ts` converts each F3 **OKLCH** token to relative luminance and asserts foreground, muted,
and primary all meet AA (≥ 4.5:1) on their background across the day/night/high-contrast themes. Editing
a token that regresses contrast **fails CI** — a dependency-free guard until axe-core is added.

`oklchLuminance` implements OKLCH → OKLab → linear-sRGB → WCAG relative luminance (channels clamped to
gamut), so the check runs on the real palette, not a hex approximation.

## Notes & limits

This slice ships the substrate + first adoptions (palette focus trap, skip link, route announcer,
reduced-motion, token contrast audit). Deferred (tracked in the plan): **axe-core in CI** (R7 — adds a
dependency; the token audit is the interim guard), a full **manual audit** with tracked issues (R1),
and focus-trap adoption across every remaining dialog/drawer.

See [`docs/dashboard/architecture.md`](../dashboard/architecture.md#accessibility--wcag-22-aa-f18) for
the design diagram.
