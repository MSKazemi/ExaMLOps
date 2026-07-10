import { type ReactNode } from 'react'
import { useCapabilities } from '@/lib/capabilities'

interface Props {
  capability: string
  children: ReactNode
  /**
   * When denied: `'disable'` (default) renders a dimmed, non-interactive affordance with the reason
   * as a tooltip (F15 R3 — no silent dead control); `'hide'` renders nothing.
   */
  mode?: 'disable' | 'hide'
}

/**
 * CapabilityGate — renders `children` only when the caller holds `capability` (F15 R3 / ADR 0057).
 *
 * Affordance only — the BFF remains the sole enforcement point; this never gates data, just controls.
 * On deny it *explains why* (tooltip) rather than silently dropping the control, unless `mode="hide"`.
 */
export function CapabilityGate({ capability, children, mode = 'disable' }: Props) {
  const caps = useCapabilities()
  if (caps.can(capability)) return <>{children}</>
  if (mode === 'hide') return null
  const why = caps.reason(capability)
  return (
    <span
      title={why}
      aria-disabled="true"
      className="inline-flex opacity-50 cursor-not-allowed pointer-events-none"
      data-denied-capability={capability}
    >
      {children}
    </span>
  )
}
