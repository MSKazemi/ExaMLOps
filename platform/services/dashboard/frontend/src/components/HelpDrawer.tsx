import { useEffect, useRef, useState } from 'react'
import { HelpCircle, X } from 'lucide-react'
import { searchGlossary } from '@/lib/glossary'
import { useFocusTrap } from '@/hooks/useFocusTrap'

/**
 * Contextual help drawer + glossary (F21 / ADR 0072, R6). Opens on `?` (Shift-/) from anywhere or via the
 * launcher; self-hosted (no SaaS). Focus-trapped (F18).
 */
export function HelpDrawer() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const drawerRef = useRef<HTMLDivElement>(null)
  useFocusTrap(drawerRef, open)

  // `?` toggles the drawer (ignored while typing in an input/textarea).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      const typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
      if (e.key === '?' && !typing) {
        e.preventDefault()
        setOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const terms = searchGlossary(query)

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Open help"
        className="no-print fixed bottom-4 right-32 z-40 flex items-center gap-1.5 rounded-full border border-border bg-background px-3 py-2 text-sm shadow-lg hover:bg-muted"
      >
        <HelpCircle className="size-4 text-primary" aria-hidden="true" />
        Help
      </button>
    )
  }

  return (
    <div
      ref={drawerRef}
      role="dialog"
      aria-label="Help & glossary"
      className="no-print fixed right-0 top-0 z-40 flex h-full w-full max-w-sm flex-col border-l border-border bg-background shadow-2xl"
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="flex items-center gap-2 font-semibold">
          <HelpCircle className="size-4 text-primary" aria-hidden="true" />
          Help &amp; glossary
        </span>
        <button type="button" onClick={() => setOpen(false)} aria-label="Close help" className="text-muted-foreground hover:text-foreground">
          <X className="size-4" aria-hidden="true" />
        </button>
      </header>
      <div className="border-b border-border p-3">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search terms…"
          aria-label="Search glossary"
          className="w-full rounded-md border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary"
        />
      </div>
      <dl className="flex-1 space-y-3 overflow-y-auto p-4 text-sm">
        {terms.length === 0 && <p className="text-muted-foreground">No matching terms.</p>}
        {terms.map((t) => (
          <div key={t.term}>
            <dt className="font-semibold">{t.term}</dt>
            <dd className="text-muted-foreground">{t.definition}</dd>
          </div>
        ))}
      </dl>
      <p className="border-t border-border px-4 py-2 text-xs text-muted-foreground">
        Press <kbd className="rounded border border-border px-1">?</kbd> anywhere to toggle this drawer.
      </p>
    </div>
  )
}
