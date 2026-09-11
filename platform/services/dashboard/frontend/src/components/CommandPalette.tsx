import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search, CornerDownLeft } from 'lucide-react'
import { getRole } from '@/lib/auth'
import { rankCommands } from '@/lib/search'
import { useSearch, type SearchResult } from '@/lib/search'
import type { Command } from '@/lib/commands'
import { cliCommandHref, searchCommands, useCliCatalog } from '@/lib/cli'
import { fuzzyScore } from '@/lib/search'
import { resourceHref } from '@/lib/resources'
import { useFlag } from '@/lib/serverflags'
import { pageAllowed, useModules } from '@/lib/modules'
import { useFocusTrap } from '@/hooks/useFocusTrap'

// A flat, selectable palette row — either a registered command or a federated search hit.
type Row =
  | { type: 'command'; key: string; label: string; group: string; cmd: Command }
  | { type: 'result'; key: string; label: string; group: string; result: SearchResult }
  | { type: 'cli'; key: string; label: string; group: string; path: string }
  | { type: 'resource'; key: string; label: string; group: string; id: string }

// How many `exa` commands the palette offers per query — enough to find one, not a wall.
const MAX_CLI_ROWS = 8

/**
 * CommandPalette — ⌘K / Ctrl-K global palette (F2 / ADR 0056).
 *
 * Fuzzy-navigates to any page/entity and copies action `exa` equivalents (GUI↔CLI parity).
 * Registered commands are role-scoped (F15); federated `/search` results stream in below them.
 * Keyboard: ⌘K toggle · ↑/↓ move · Enter select · Esc close.
 */
export function CommandPalette() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()
  // Trap focus inside the dialog while open and restore it to the trigger on close (F18 R2).
  useFocusTrap(dialogRef, open)
  const role = getRole()
  const { results } = useSearch(query)
  // Every `exa` command is reachable from ⌘K (ADR 0119): fetched once, only after the user types —
  // and neither fetched nor shown (even from cache) while the `cliConsole` kill switch is off.
  const cliOn = useFlag('cliConsole')
  const { data: cachedCatalog } = useCliCatalog(cliOn && open && query.trim().length > 0)
  const catalog = cliOn ? cachedCatalog : undefined
  // A page of a module this site switched off (ADR 0128) is not offered either.
  const disabledPages = useModules().data?.disabled_pages

  // ⌘K / Ctrl-K toggles the palette from anywhere. Resetting query/selection here (an event
  // handler, not an effect) keeps state updates out of the render/effect path.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setQuery('')
        setActive(0)
        setOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Focus the input when the palette opens (no state update — effect stays side-effect-only).
  useEffect(() => {
    if (open) requestAnimationFrame(() => inputRef.current?.focus())
  }, [open])

  const rows: Row[] = useMemo(() => {
    const cmdRows: Row[] = rankCommands(query, role)
      .filter((cmd) => !cmd.to || pageAllowed(cmd.to, disabledPages))
      .map((cmd) => ({
      type: 'command',
      key: `cmd:${cmd.id}`,
      label: cmd.label,
      group: cmd.group,
      cmd,
    }))
    const resultRows: Row[] = results.map((r) => ({
      type: 'result',
      key: `res:${r.source}:${r.id}`,
      label: r.label,
      group: r.source,
      result: r,
    }))
    const cliRows: Row[] =
      catalog && query.trim()
        ? searchCommands(catalog.commands, query)
            .slice(0, MAX_CLI_ROWS)
            .map((c) => ({ type: 'cli', key: `cli:${c.path}`, label: `exa ${c.path}`, group: 'CLI', path: c.path }))
        : []
    // "Manage Projects", "Manage Gateway keys"… — every resource table, by name.
    const resourceRows: Row[] =
      catalog?.resources && query.trim()
        ? catalog.resources
            .map((r) => ({ r, s: Math.max(fuzzyScore(query, r.title), fuzzyScore(query, r.id)) }))
            .filter((x) => x.s > 0)
            .sort((a, b) => b.s - a.s)
            .slice(0, 5)
            .map(({ r }) => ({ type: 'resource', key: `res:${r.id}`, label: `Manage ${r.title}`, group: 'Resources', id: r.id }))
        : []
    return [...cmdRows, ...resourceRows, ...cliRows, ...resultRows]
  }, [query, role, results, catalog, disabledPages])

  // Derive the effective selection (clamped to the current list) instead of syncing it in an
  // effect — the raw `active` may exceed `rows.length` after the list shrinks.
  const activeIdx = Math.min(active, Math.max(0, rows.length - 1))

  const runRow = (row: Row | undefined) => {
    if (!row) return
    if (row.type === 'result') {
      navigate(row.result.url)
      setOpen(false)
      return
    }
    if (row.type === 'resource') {
      navigate(resourceHref(row.id))
      setOpen(false)
      return
    }
    if (row.type === 'cli') {
      navigate(cliCommandHref(row.path))
      setOpen(false)
      return
    }
    const { cmd } = row
    if (cmd.to) {
      navigate(cmd.to)
      setOpen(false)
    } else if (cmd.cliEquivalent) {
      navigator.clipboard?.writeText(cmd.cliEquivalent).catch(() => {})
      setOpen(false)
    }
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') setOpen(false)
    else if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((a) => Math.min(a + 1, rows.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((a) => Math.max(a - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      runRow(rows[activeIdx])
    }
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh] bg-black/40"
      onClick={() => setOpen(false)}
      role="presentation"
    >
      <div
        ref={dialogRef}
        className="w-full max-w-lg rounded-xl border border-border bg-background shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Command palette"
        aria-modal="true"
      >
        <div className="flex items-center gap-2 border-b border-border px-3">
          <Search className="size-4 text-muted-foreground shrink-0" aria-hidden="true" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Search models, jobs, pages… or run a command"
            aria-label="Search or run a command"
            className="w-full bg-transparent py-3 text-sm outline-none placeholder:text-muted-foreground"
          />
        </div>
        <ul className="max-h-80 overflow-y-auto py-1" role="listbox">
          {rows.length === 0 && (
            <li className="px-4 py-6 text-center text-sm text-muted-foreground">No matches</li>
          )}
          {rows.map((row, i) => (
            <li key={row.key} role="option" aria-selected={i === activeIdx}>
              <button
                type="button"
                onMouseEnter={() => setActive(i)}
                onClick={() => runRow(row)}
                className={`flex w-full items-center justify-between gap-2 px-4 py-2 text-left text-sm ${
                  i === activeIdx ? 'bg-muted' : ''
                }`}
              >
                <span className="truncate">{row.label}</span>
                <span className="flex items-center gap-2 shrink-0">
                  <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    {row.group}
                  </span>
                  {i === activeIdx && <CornerDownLeft className="size-3 text-muted-foreground" />}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
