import { cn } from '@/lib/utils'
import { TIER_HELP, TIER_LABEL, type Tier } from '@/lib/cli'

// Text + tint, never colour alone (F18): the label always says what the tier is.
const TINT: Record<Tier, string> = {
  read: '--success-text',
  admin: '--warning-text',
  destructive: '--error-text',
  cli_only: '--text-2',
}

export function TierBadge({ tier, className }: { tier: Tier; className?: string }) {
  const color = `var(${TINT[tier]})`
  return (
    <span
      title={TIER_HELP[tier]}
      data-tier={tier}
      style={{ color, backgroundColor: `color-mix(in oklch, ${color} 14%, transparent)` }}
      className={cn('inline-flex shrink-0 items-center rounded-4xl px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide', className)}
    >
      {TIER_LABEL[tier]}
    </span>
  )
}
