import { useState } from 'react'
import { Code2, PlusCircle, Trash2, CheckCircle2, X, ShieldCheck } from 'lucide-react'
import {
  useProviders, useSaveProvider, useActivateProvider, useDeleteProvider,
  readProvider, validateProvider, providerTemplate,
  PROVIDER_DOMAINS, type ProviderRow, type ValidateResult,
} from '@/lib/providers'

// The dashboard editor for notebook/CLI-authored calculation providers (ADR 0074). Admins can
// view/edit/upload the Python behind a project's cost/carbon/… calculations and pick the active
// one; the backend AST-sandboxes every upload. Viewers see the list read-only.

const ACCENT = { background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }
const DANGER = { background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }
const SUCCESS = { background: 'oklch(0.72 0.18 155 / 12%)', border: '1px solid oklch(0.72 0.18 155 / 30%)', color: 'var(--success-text)' }

function ProviderEditor({
  project, initial, onClose,
}: {
  project: string
  initial?: { domain: string; name: string; code: string }
  onClose: () => void
}) {
  const editing = !!initial
  const [domain, setDomain] = useState(initial?.domain ?? PROVIDER_DOMAINS[0])
  const [name, setName] = useState(initial?.name ?? '')
  const [code, setCode] = useState(initial?.code ?? providerTemplate(initial?.domain ?? PROVIDER_DOMAINS[0]))
  const [activate, setActivate] = useState(false)
  const [check, setCheck] = useState<ValidateResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const save = useSaveProvider(project)

  const runValidate = async () => {
    setError(null)
    try {
      setCheck(await validateProvider(code))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'validation failed')
    }
  }
  const runSave = async () => {
    setError(null)
    try {
      await save.mutateAsync({ project, domain, name: name.trim(), code, activate })
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'save failed')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'oklch(0 0 0 / 55%)' }}>
      <div className="w-full max-w-2xl rounded-xl overflow-hidden" style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}>
        <div className="flex items-center justify-between px-4 py-3" style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border)' }}>
          <h2 className="text-sm font-semibold">{editing ? `Edit provider — ${initial!.name}` : 'New provider'}</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground"><X className="w-4 h-4" /></button>
        </div>
        <div className="p-4 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <label className="block space-y-1">
              <span className="text-xs font-medium text-muted-foreground">Domain</span>
              <select value={domain} disabled={editing} onChange={e => { setDomain(e.target.value); if (!editing && !code.trim()) setCode(providerTemplate(e.target.value)) }}
                className="w-full rounded-lg px-3 py-2 text-sm font-mono disabled:opacity-60"
                style={{ background: 'var(--surface-1)', border: '1px solid var(--border-md)' }}>
                {PROVIDER_DOMAINS.map(d => <option key={d} value={d}>{d}</option>)}
              </select>
            </label>
            <label className="block space-y-1">
              <span className="text-xs font-medium text-muted-foreground">Name</span>
              <input value={name} disabled={editing} onChange={e => setName(e.target.value)} placeholder="my-cost"
                className="w-full rounded-lg px-3 py-2 text-sm font-mono disabled:opacity-60"
                style={{ background: 'var(--surface-1)', border: '1px solid var(--border-md)' }} />
            </label>
          </div>
          <label className="block space-y-1">
            <span className="text-xs font-medium text-muted-foreground">Python (defines one <code>class X(Provider)</code>; no imports — Provider/ProviderMeta/math pre-injected)</span>
            <textarea value={code} onChange={e => { setCode(e.target.value); setCheck(null) }} spellCheck={false} rows={14}
              className="w-full rounded-lg px-3 py-2 text-xs font-mono"
              style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-md)' }} />
          </label>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input type="checkbox" checked={activate} onChange={e => setActivate(e.target.checked)} />
            Make this the active {domain} provider for the project
          </label>
          {check && (
            <p className="text-xs rounded-lg px-3 py-2" style={check.ok ? SUCCESS : DANGER}>
              {check.ok ? `✓ Valid — defines ${check.class}` : `✗ ${check.error}`}
            </p>
          )}
          {error && <p className="text-xs rounded-lg px-3 py-2" style={DANGER}>{error}</p>}
        </div>
        <div className="flex justify-between gap-2 px-4 py-3" style={{ background: 'var(--surface-1)', borderTop: '1px solid var(--border)' }}>
          <button onClick={runValidate} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium" style={ACCENT}>
            <ShieldCheck className="w-3.5 h-3.5" /> Validate
          </button>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-3 py-1.5 rounded-lg text-xs font-medium"
              style={{ background: 'var(--surface-2)', border: '1px solid var(--border-md)', color: 'var(--subtle-text)' }}>Cancel</button>
            <button onClick={runSave} disabled={save.isPending || !name.trim()}
              className="px-3 py-1.5 rounded-lg text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ background: 'oklch(0.64 0.20 265)', border: '1px solid oklch(0.64 0.20 265 / 60%)', color: 'oklch(0.99 0 0)' }}>
              {save.isPending ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

export function ProvidersCard({ project, admin }: { project: string; admin: boolean }) {
  const { data = [], isLoading } = useProviders(project)
  const activate = useActivateProvider(project)
  const del = useDeleteProvider(project)
  const [editing, setEditing] = useState<{ domain: string; name: string; code: string } | undefined>(undefined)
  const [showNew, setShowNew] = useState(false)

  const openEdit = async (row: ProviderRow) => {
    try {
      const src = await readProvider(project, row.domain, row.name)
      setEditing({ domain: row.domain, name: row.name, code: src.code })
    } catch { /* surfaced by the editor */ }
  }

  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border-sm)', background: 'var(--surface-0)' }}>
      <div className="flex items-center justify-between px-4 py-3" style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border-sm)' }}>
        <h2 className="text-sm font-semibold flex items-center gap-2"><Code2 className="w-4 h-4" style={{ color: 'var(--accent-text)' }} /> Providers</h2>
        {admin && (
          <button onClick={() => setShowNew(true)} className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium" style={ACCENT}>
            <PlusCircle className="w-3 h-3" /> New provider
          </button>
        )}
      </div>
      <div className="p-4 space-y-2">
        <p className="text-[11px] text-muted-foreground">
          Editable Python behind this project's calculations (FinOps cost/carbon, drift, …). Uploads are
          AST-sandboxed. Author the same from a notebook: <span className="font-mono">examlops.providers.save_provider</span>.
        </p>
        {isLoading ? (
          <p className="text-sm text-muted-foreground italic">Loading providers…</p>
        ) : data.length === 0 ? (
          <p className="text-sm text-muted-foreground italic">No authored providers yet. Click "New provider" to write one.</p>
        ) : (
          <div className="space-y-1.5">
            {data.map(row => {
              const busy = (activate.isPending && activate.variables?.name === row.name) || (del.isPending && del.variables?.name === row.name)
              return (
                <div key={`${row.domain}/${row.name}`} className="flex items-center gap-3 rounded-lg px-3 py-2" style={{ background: 'var(--surface-1)', border: '1px solid var(--border-sm)' }}>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[11px] px-1.5 py-0.5 rounded font-mono" style={{ background: 'var(--surface-2)', color: 'var(--subtle-text)' }}>{row.domain}</span>
                      <span className="font-mono text-sm">{row.name}</span>
                      {row.active && <span className="text-[10px] px-1.5 py-0.5 rounded-full uppercase tracking-wide" style={SUCCESS}>active</span>}
                      {!row.ok && <span className="text-[10px] px-1.5 py-0.5 rounded-full" style={DANGER} title={row.error ?? ''}>gate error</span>}
                    </div>
                  </div>
                  {admin && (
                    <div className="flex items-center gap-1.5 shrink-0">
                      <button onClick={() => openEdit(row)} className="rounded-md px-2 py-1 text-xs font-medium" style={ACCENT}>Edit</button>
                      {!row.active && (
                        <button onClick={() => activate.mutate({ domain: row.domain, name: row.name })} disabled={busy}
                          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium disabled:opacity-50" style={SUCCESS} title="Make active for this domain">
                          <CheckCircle2 className="w-3 h-3" /> Activate
                        </button>
                      )}
                      <button onClick={() => { if (window.confirm(`Delete provider "${row.name}" (${row.domain})?`)) del.mutate({ domain: row.domain, name: row.name }) }}
                        disabled={busy} title="Delete provider" className="rounded-md px-2 py-1 text-xs font-medium disabled:opacity-50" style={DANGER}>
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>
      {showNew && admin && <ProviderEditor project={project} onClose={() => setShowNew(false)} />}
      {editing && admin && <ProviderEditor project={project} initial={editing} onClose={() => setEditing(undefined)} />}
    </div>
  )
}
