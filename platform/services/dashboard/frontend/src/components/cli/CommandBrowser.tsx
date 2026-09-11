import { useMemo, useState } from 'react'
import { ChevronDown, Search, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { filterByPrefixes, groupByPanel, searchCommands, type CliCatalog, type CliCommand } from '@/lib/cli'
import { TierBadge } from './TierBadge'

interface Props {
  catalog: CliCatalog
  selected: string | null
  onSelect: (path: string) => void
  /** Path prefixes from a console's "exa commands" link (e.g. ['drift']). */
  prefixes: string[]
  onClearPrefixes: () => void
}

/** Searchable list of every `exa` command, grouped by the CLI's own lifecycle panels. */
export function CommandBrowser({ catalog, selected, onSelect, prefixes, onClearPrefixes }: Props) {
  const [query, setQuery] = useState('')
  const [closed, setClosed] = useState<Set<string>>(new Set())

  const visible = useMemo(
    () => searchCommands(filterByPrefixes(catalog.commands, prefixes), query),
    [catalog.commands, prefixes, query],
  )
  // While searching, show ranked hits flat; otherwise group by panel.
  const grouped = useMemo(() => (query.trim() ? null : groupByPanel(catalog, visible)), [catalog, visible, query])

  const toggle = (panel: string) =>
    setClosed((prev) => {
      const next = new Set(prev)
      if (next.has(panel)) next.delete(panel)
      else next.add(panel)
      return next
    })

  const row = (c: CliCommand) => (
    <li key={c.path}>
      <button
        type="button"
        onClick={() => onSelect(c.path)}
        aria-current={selected === c.path ? 'true' : undefined}
        title={c.short_help}
        className={cn(
          'flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs hover:bg-muted',
          selected === c.path && 'bg-muted font-medium',
        )}
      >
        <code className="flex-1 truncate">{c.path}</code>
        <TierBadge tier={c.tier} />
      </button>
    </li>
  )

  return (
    <div className="flex h-full flex-col gap-2">
      <div className="relative">
        <Search className="pointer-events-none absolute left-2 top-2 size-3.5 text-muted-foreground" aria-hidden="true" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={`Search ${catalog.total} commands…`}
          aria-label="Search exa commands"
          className="w-full rounded-md border border-border bg-transparent py-1.5 pl-7 pr-2 text-xs outline-none focus:border-primary"
        />
      </div>
      {prefixes.length > 0 && (
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <span>
            Showing <code>{prefixes.map((p) => `exa ${p}`).join(', ')}</code>
          </span>
          <button type="button" onClick={onClearPrefixes} aria-label="Show all commands" className="inline-flex items-center gap-0.5 hover:text-foreground">
            <X className="size-3" aria-hidden="true" /> all
          </button>
        </div>
      )}
      <nav aria-label="exa commands" className="min-h-0 flex-1 overflow-y-auto pr-1">
        {visible.length === 0 && <p className="px-2 py-4 text-xs text-muted-foreground">No command matches.</p>}
        {grouped ? (
          grouped.map(([panel, cmds]) => {
            const open = !closed.has(panel)
            return (
              <div key={panel} className="mb-1">
                <button
                  type="button"
                  onClick={() => toggle(panel)}
                  aria-expanded={open}
                  className="flex w-full items-center gap-1 px-1 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground"
                >
                  <ChevronDown className={cn('size-3 transition-transform', !open && '-rotate-90')} aria-hidden="true" />
                  <span className="flex-1 text-left">{panel}</span>
                  <span className="font-normal">{cmds.length}</span>
                </button>
                {open && <ul className="space-y-px">{cmds.map(row)}</ul>}
              </div>
            )
          })
        ) : (
          <ul className="space-y-px">{visible.map(row)}</ul>
        )}
      </nav>
    </div>
  )
}
