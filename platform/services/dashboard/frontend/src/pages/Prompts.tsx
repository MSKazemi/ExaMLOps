import { useState } from 'react'
import { ScrollText, Tag } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { StatusPill } from '@/components/ui/status-pill'
import { isAdmin } from '@/lib/auth'
import { usePrompts, useCreatePromptVersion, useSetPromptLabel, type Prompt } from '@/lib/prompts'

function PromptCard({ p, admin }: { p: Prompt; admin: boolean }) {
  const setLabel = useSetPromptLabel()
  const [label, setLabel_] = useState('prod')
  const [version, setVersion] = useState(p.versions[0]?.version ?? 1)
  const [msg, setMsg] = useState<string | null>(null)

  const applyLabel = async () => {
    setMsg(null)
    try {
      await setLabel.mutateAsync({ name: p.name, label: label.trim(), version })
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to set label')
    }
  }

  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold font-mono text-sm">{p.name}</h3>
        <span className="text-xs text-muted-foreground">{p.versions.length} version{p.versions.length === 1 ? '' : 's'}</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {p.labels.length === 0 ? (
          <span className="text-xs text-muted-foreground">no labels</span>
        ) : (
          p.labels.map((l) => (
            <span key={l.label} className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs">
              <Tag className="size-3" aria-hidden="true" /> {l.label} → v{l.version}
            </span>
          ))
        )}
      </div>
      <div className="text-xs text-muted-foreground">
        Latest: v{p.versions[0]?.version} · vars: {p.versions[0]?.variables.join(', ') || 'none'}
      </div>
      {admin && (
        <div className="flex flex-wrap items-center gap-2 border-t border-border/60 pt-2">
          <span className="text-xs text-muted-foreground">Point label</span>
          <input aria-label={`Label name for ${p.name}`} value={label} onChange={(e) => setLabel_(e.target.value)}
            className="w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
          <span className="text-xs text-muted-foreground">→ v</span>
          <select aria-label={`Label version for ${p.name}`} value={version} onChange={(e) => setVersion(Number(e.target.value))}
            className="rounded-md border border-border bg-transparent px-2 py-1 text-xs">
            {p.versions.map((v) => <option key={v.version} value={v.version}>{v.version}</option>)}
          </select>
          <button onClick={applyLabel} disabled={setLabel.isPending || !label.trim()}
            className="rounded-md border border-primary bg-primary px-2 py-1 text-xs text-primary-foreground disabled:opacity-50">
            Apply
          </button>
          {msg && <span className="text-xs" style={{ color: 'var(--error-text)' }}>{msg}</span>}
        </div>
      )}
    </div>
  )
}

/**
 * Prompts console (B1, dashboard-rebuild M2) — the versioned prompt registry. Admins create
 * immutable versions (variables auto-declared from `{tokens}`) and move/rollback labels. Writes
 * reuse the shared `examlops.data.prompts` path (audited).
 */
export function Prompts() {
  const admin = isAdmin()
  const { data: prompts = [], isLoading, error } = usePrompts()
  const create = useCreatePromptVersion()
  const [name, setName] = useState('')
  const [template, setTemplate] = useState('')
  const [label, setLabel] = useState('')
  const [msg, setMsg] = useState<string | null>(null)

  const doCreate = async () => {
    setMsg(null)
    if (!name.trim() || !template.trim()) {
      setMsg('Name and template are required.')
      return
    }
    try {
      const r = await create.mutateAsync({ name: name.trim(), body: { template, label: label.trim() || undefined } })
      setMsg(`Created ${r.name} v${r.version} (vars: ${r.variables.join(', ') || 'none'}).`)
      setTemplate('')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Failed to create version')
    }
  }

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
          <ScrollText className="size-6 text-muted-foreground" aria-hidden="true" />
          Prompt Registry
        </h1>
        <p className="text-sm text-muted-foreground">
          Immutable prompt versions with moving labels (dev/staging/prod). Variables are auto-declared
          from <code>{'{tokens}'}</code> in the template.
        </p>
      </div>

      {msg && (
        <p className="text-xs rounded-lg px-3 py-2" style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-2)' }}>
          {msg}
        </p>
      )}

      {admin && (
        <section className="space-y-2">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">New version</h2>
          <div className="flex flex-wrap items-end gap-2">
            <label className="text-xs text-muted-foreground">
              Name
              <input aria-label="Prompt name" value={name} onChange={(e) => setName(e.target.value)}
                className="mt-1 block w-40 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
            <label className="text-xs text-muted-foreground">
              Label (optional)
              <input aria-label="Prompt label" value={label} onChange={(e) => setLabel(e.target.value)}
                placeholder="dev" className="mt-1 block w-24 rounded-md border border-border bg-transparent px-2 py-1 text-xs" />
            </label>
          </div>
          <textarea aria-label="Prompt template" value={template} onChange={(e) => setTemplate(e.target.value)}
            placeholder="Summarize {ticket} for {team}…" rows={3}
            className="block w-full rounded-md border border-border bg-transparent px-2 py-1 text-xs font-mono" />
          <button onClick={doCreate} disabled={create.isPending}
            className="rounded-md border border-primary bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50">
            Create version
          </button>
        </section>
      )}

      {error && <EmptyState title="Couldn't load prompts" description="The prompt registry endpoint is unreachable." />}
      {isLoading && <Skeleton className="h-24 w-full" />}
      {!isLoading && prompts.length === 0 && !error && (
        <EmptyState title="No prompts yet" description={admin ? 'Create the first version above.' : 'An admin can register prompts here.'} />
      )}

      {prompts.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {prompts.map((p) => <PromptCard key={p.name} p={p} admin={admin} />)}
        </div>
      )}

      {!admin && prompts.length > 0 && (
        <StatusPill status="warn" label="Read-only — prompt edits require the admin role" />
      )}
    </div>
  )
}
