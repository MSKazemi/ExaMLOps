import { Star } from 'lucide-react'
import { useWatchlist, type EntityRef } from '@/lib/prefs'

// Pin/unpin an entity to the watchlist (F21 / ADR 0072, R4).
export function PinButton({ entity, label }: { entity: EntityRef; label?: string }) {
  const { isPinned, toggle } = useWatchlist()
  const pinned = isPinned(entity)
  return (
    <button
      type="button"
      onClick={() => toggle(entity)}
      aria-pressed={pinned}
      aria-label={pinned ? `Unpin ${label ?? entity.id}` : `Pin ${label ?? entity.id}`}
      title={pinned ? 'Unpin from watchlist' : 'Pin to watchlist'}
      className="text-muted-foreground hover:text-foreground"
    >
      <Star className={`size-4 ${pinned ? 'fill-current text-amber-500' : ''}`} aria-hidden="true" />
    </button>
  )
}
