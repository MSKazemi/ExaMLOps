import { ExternalLink } from 'lucide-react'
import { popOut } from '@/lib/responsive'

// Detach a live panel (Grafana embed / log tail) into its own window (F20 / ADR 0069, R5).
export function PopOutButton({ url, label = 'Pop out', name }: { url: string; label?: string; name?: string }) {
  return (
    <button
      type="button"
      onClick={() => popOut(url, name)}
      aria-label={label}
      className="no-print flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-muted"
    >
      <ExternalLink className="size-3.5" aria-hidden="true" />
      {label}
    </button>
  )
}
