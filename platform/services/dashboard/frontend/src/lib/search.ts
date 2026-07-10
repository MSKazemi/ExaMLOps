import { useEffect, useState } from 'react'
import { apiFetch } from './api'
import { visibleCommands, type Command } from './commands'
import type { Role } from './auth'

// ── search result types (mirror backend search.py / F2 R3) ───────────────────

export interface SearchResult {
  kind: 'model' | 'job' | 'audit' | 'page'
  id: string
  label: string
  url: string
  score: number
  source: 'mlflow' | 'scheduler' | 'dataset' | 'docs' | 'audit'
}

export interface SearchResponse {
  search: { query: string; count: number; results: SearchResult[]; groups: Record<string, SearchResult[]> }
  _partial?: string[]
}

// ── pure client-side fuzzy scoring for the command list (F2 R1) ───────────────

/** Mirror of backend `score()` so palette command ranking matches server search ranking. */
export function fuzzyScore(query: string, text: string): number {
  const q = query.trim().toLowerCase()
  const t = text.toLowerCase()
  if (!q) return 0
  if (t === q) return 100
  if (t.startsWith(q)) return 80
  if (
    t
      .replace(/[/-]/g, ' ')
      .split(' ')
      .some((w) => w.startsWith(q))
  )
    return 60
  if (t.includes(q)) return 40
  return isSubsequence(q, t) ? 20 : 0
}

function isSubsequence(q: string, t: string): boolean {
  let i = 0
  for (const ch of t) if (ch === q[i]) i++
  return i === q.length
}

/** Rank a role's visible commands against a query (empty query ⇒ all, in registry order). */
export function rankCommands(query: string, role: Role | null): Command[] {
  const cmds = visibleCommands(role)
  if (!query.trim()) return cmds
  return cmds
    .map((c) => ({ c, s: fuzzyScore(query, c.label) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s || a.c.label.localeCompare(b.c.label))
    .map((x) => x.c)
}

// ── debounced federated-search hook (F2 R4) ───────────────────────────────────

/**
 * Debounced, cancelable federated search against `/api/v1/search`. Returns results (empty for a
 * blank query) and a loading flag. Each keystroke supersedes the last (AbortController).
 */
export function useSearch(query: string, delayMs = 200) {
  const [results, setResults] = useState<SearchResult[]>([])
  const [loading, setLoading] = useState(false)

  const q = query.trim()
  useEffect(() => {
    const ctl = new AbortController()
    // All state updates happen inside the (asynchronous) timer callback, never synchronously in
    // the effect body — a blank query clears results with a zero-delay tick.
    const timer = setTimeout(
      () => {
        if (!q) {
          setResults([])
          setLoading(false)
          return
        }
        setLoading(true)
        apiFetch<SearchResponse>(`/api/v1/search?q=${encodeURIComponent(q)}`, { signal: ctl.signal })
          .then((r) => setResults(r.search.results))
          .catch(() => {
            /* aborted or failed — keep last results */
          })
          .finally(() => setLoading(false))
      },
      q ? delayMs : 0,
    )
    return () => {
      clearTimeout(timer)
      ctl.abort()
    }
  }, [q, delayMs])

  return { results, loading }
}
