import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Download, Pencil, Plus, RotateCcw, ShieldCheck, SquareTerminal, Trash2 } from 'lucide-react'
import { Dialog } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { StatusPill } from '@/components/ui/status-pill'
import { CAP, useCapabilities } from '@/lib/capabilities'
import { cliCommandHref, downloadWorkspaceFile } from '@/lib/cli'
import { runToCompletion } from '@/lib/resources'

// The `exa` configuration the dashboard's own CLI runs use (CLI Console, Resources): endpoints,
// tokens and named contexts in the dashboard host's config.toml. Everything here is an `exa
// config …` / `exa env` run — the same code, validation and audit as the terminal.

interface Setting {
  key: string
  value: string
  source: string
}
interface EnvView {
  active_context: string | null
  config_file: string
  settings: Setting[]
}
interface ContextsView {
  contexts: string[]
  active: string | null
}
interface Finding {
  level: string
  key: string
  message: string
}

const SECRET = /token|secret|password/i
const BASE = ''

function sourceLabel(source: string): { text: string; tone: string } {
  if (source.startsWith('env:')) return { text: `env ${source.slice(4)}`, tone: '--warning-text' }
  if (source.startsWith('context:')) return { text: `context ${source.slice(8)}`, tone: '--accent-text' }
  if (source === 'file') return { text: 'base config', tone: '--success-text' }
  return { text: 'default', tone: '--text-2' }
}

export function CliConfigSection() {
  const caps = useCapabilities()
  const admin = caps.can(CAP.CLI_WRITE)
  const qc = useQueryClient()
  // Which configuration the table shows and edits: the base file, or one named context.
  const [target, setTarget] = useState<string>(BASE)
  const [editing, setEditing] = useState<Setting | null>(null)
  const [draft, setDraft] = useState('')
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const contexts = useQuery({
    queryKey: ['cli-config', 'contexts'],
    queryFn: async () => (await runToCompletion('config contexts', {})).parsed as ContextsView,
  })
  const env = useQuery({
    queryKey: ['cli-config', 'env', target],
    queryFn: async () => (await runToCompletion('env', {}, { context: target })).parsed as EnvView,
  })
  const [findings, setFindings] = useState<Finding[] | null>(null)

  const run = useMutation({
    mutationFn: ({ command, args, confirm }: { command: string; args: Record<string, unknown>; confirm?: string }) =>
      runToCompletion(command, args, { confirm }),
    onSuccess: (r) => {
      const p = r.parsed as { message?: string } | null
      setMsg({ ok: true, text: p?.message ?? `exa ${r.command} succeeded` })
      qc.invalidateQueries({ queryKey: ['cli-config'] })
    },
    onError: (e) => setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) }),
  })

  const ctxArgs = target ? { context: target } : {}
  const active = contexts.data?.active ?? null

  const save = () => {
    if (!editing) return
    run.mutate({ command: 'config set', args: { key: editing.key, value: draft, ...ctxArgs } })
    setEditing(null)
  }

  const validate = async () => {
    setFindings(null)
    try {
      setFindings((await runToCompletion('env', { validate: true })).parsed as Finding[])
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) })
    }
  }

  const exportConfig = async () => {
    try {
      const r = await runToCompletion('config export', { out: 'config/config-export.json' })
      await downloadWorkspaceFile(r.files.find((f) => f.endsWith('config-export.json')) ?? 'config/config-export.json')
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) })
    }
  }

  const btn = 'inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-muted disabled:opacity-50'

  return (
    <section aria-label="exa CLI configuration" className="space-y-4 rounded-xl p-5" style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}>
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-0.5">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <SquareTerminal className="size-4 text-muted-foreground" aria-hidden="true" /> exa CLI configuration
          </h2>
          <p className="max-w-xl text-xs text-muted-foreground">
            The endpoints, tokens and contexts the <code>exa</code> commands run by this dashboard use (CLI Console,
            Resources). Resolution order: environment variable → active context → base config → default.
          </p>
          {env.data && <p className="text-[11px] text-muted-foreground">File: <code>{env.data.config_file}</code></p>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className={btn} onClick={validate}>
            <ShieldCheck className="size-3" aria-hidden="true" /> Validate
          </button>
          <button type="button" className={btn} onClick={exportConfig} disabled={!admin} title={admin ? 'exa config export' : 'Requires the admin role.'}>
            <Download className="size-3" aria-hidden="true" /> Export
          </button>
        </div>
      </header>

      {/* Contexts */}
      <div className="space-y-2">
        <h3 className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">Contexts</h3>
        {contexts.isLoading && <Skeleton className="h-8 w-full" />}
        {contexts.error && <p className="text-xs text-muted-foreground">Couldn&rsquo;t list contexts: {String((contexts.error as Error).message)}</p>}
        {contexts.data && (
          <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Configuration to show">
            {[BASE, ...contexts.data.contexts].map((name) => {
              const isActive = name === BASE ? active === null : name === active
              return (
                <div key={name || 'base'} className={`flex items-center gap-1 rounded-lg border px-2 py-1 text-xs ${target === name ? 'border-primary' : 'border-border'}`}>
                  <button type="button" aria-pressed={target === name} onClick={() => setTarget(name)} className="font-medium">
                    {name || 'base'}
                  </button>
                  {isActive && <span className="text-[10px] text-[color:var(--success-text)]">active</span>}
                  {!isActive && (
                    <button
                      type="button"
                      className="text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-40"
                      disabled={!admin || run.isPending}
                      title={admin ? `Make ${name || 'the base config'} active for every exa run` : 'Requires the admin role.'}
                      onClick={() => run.mutate({ command: 'config use', args: name ? { name } : { clear: true } })}
                    >
                      use
                    </button>
                  )}
                  {name && (
                    <button type="button" aria-label={`Delete context ${name}`} className="rounded p-0.5 hover:bg-muted disabled:opacity-40" disabled={!admin} onClick={() => setDeleting(name)}>
                      <Trash2 className="size-3" aria-hidden="true" />
                    </button>
                  )}
                </div>
              )
            })}
            <button type="button" className={btn} disabled={!admin} onClick={() => setCreating(true)} title={admin ? undefined : 'Requires the admin role.'}>
              <Plus className="size-3" aria-hidden="true" /> New context
            </button>
          </div>
        )}
      </div>

      {/* Effective settings */}
      <div className="space-y-2">
        <h3 className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
          Effective settings — {target ? `with context ${target}` : 'base configuration'}
        </h3>
        <p className="text-[11px] text-muted-foreground">
          {target
            ? `Edits are written into context ${target} and override the base value there; Reset removes the override.`
            : active
              ? `Context ${active} is active, so values it overrides show its source. Edits here change the base file.`
              : 'Edits change the base file; Reset returns a value to its default.'}
        </p>
        {env.isLoading && <Skeleton className="h-40 w-full" />}
        {env.error && <p className="text-xs text-muted-foreground">Couldn&rsquo;t read settings: {String((env.error as Error).message)}</p>}
        {env.data && (
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-xs">
              <thead className="bg-muted/40">
                <tr>
                  <th scope="col" className="px-2 py-1.5 text-left">Key</th>
                  <th scope="col" className="px-2 py-1.5 text-left">Value</th>
                  <th scope="col" className="px-2 py-1.5 text-left">Source</th>
                  <th scope="col" className="px-2 py-1.5 text-right"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {env.data.settings.map((s) => {
                  const src = sourceLabel(s.source)
                  const fromEnv = s.source.startsWith('env:')
                  const own = target ? s.source === `context:${target}` : s.source === 'file'
                  const isEditing = editing?.key === s.key
                  return (
                    <tr key={s.key} className="border-t border-border">
                      <td className="px-2 py-1.5 font-mono">{s.key}</td>
                      <td className="px-2 py-1.5">
                        {isEditing ? (
                          <form className="flex items-center gap-1" onSubmit={(e) => { e.preventDefault(); save() }}>
                            <input
                              autoFocus
                              aria-label={`New value for ${s.key}`}
                              type={SECRET.test(s.key) ? 'password' : 'text'}
                              value={draft}
                              onChange={(e) => setDraft(e.target.value)}
                              className="w-72 rounded-md border border-border bg-transparent px-2 py-0.5 font-mono"
                            />
                            <button type="submit" className={btn} disabled={!draft}>Save</button>
                            <button type="button" className={btn} onClick={() => setEditing(null)}>Cancel</button>
                          </form>
                        ) : (
                          <span className="font-mono">{s.value}</span>
                        )}
                      </td>
                      <td className="px-2 py-1.5">
                        <span className="rounded-4xl px-1.5 py-0.5 text-[10px]" style={{ color: `var(${src.tone})`, background: `color-mix(in oklch, var(${src.tone}) 12%, transparent)` }}>
                          {src.text}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-2 py-1 text-right">
                        {!isEditing && (
                          <>
                            <button
                              type="button"
                              aria-label={`Edit ${s.key}`}
                              className="rounded p-1 hover:bg-muted disabled:opacity-40"
                              disabled={!admin || fromEnv}
                              title={fromEnv ? `Set by ${s.source.slice(4)} in the dashboard's environment — it wins over any file value; change it in the deployment.` : admin ? 'Edit' : 'Requires the admin role.'}
                              onClick={() => { setEditing(s); setDraft(SECRET.test(s.key) || s.value === '(unset)' ? '' : s.value) }}
                            >
                              <Pencil className="size-3.5" aria-hidden="true" />
                            </button>
                            <button
                              type="button"
                              aria-label={`Reset ${s.key}`}
                              className="rounded p-1 hover:bg-muted disabled:opacity-40"
                              disabled={!admin || !own}
                              title={own ? `Remove this value (exa config unset${target ? ` --context ${target}` : ''})` : 'Nothing set here to remove'}
                              onClick={() => run.mutate({ command: 'config unset', args: { key: s.key, ...ctxArgs } })}
                            >
                              <RotateCcw className="size-3.5" aria-hidden="true" />
                            </button>
                          </>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {findings && (
        <ul aria-label="Validation findings" className="space-y-1 text-xs">
          {findings.map((f, i) => (
            <li key={i} className="flex items-start gap-2">
              <StatusPill status={f.level === 'ok' ? 'healthy' : f.level === 'warn' ? 'degraded' : 'failed'} label={f.level} />
              {f.key !== '-' && <code>{f.key}</code>}
              <span className="text-muted-foreground">{f.message}</span>
            </li>
          ))}
        </ul>
      )}

      {msg && (
        <p role={msg.ok ? 'status' : 'alert'} className="flex items-center gap-1 text-xs" style={{ color: msg.ok ? 'var(--success-text)' : 'var(--error-text)' }}>
          {msg.ok && <CheckCircle2 className="size-3.5" aria-hidden="true" />} {msg.text}
        </p>
      )}
      <p className="text-[11px] text-muted-foreground">
        Every change is an <code>exa config</code> run, audited like any other —{' '}
        <Link to={cliCommandHref('config set')} className="text-primary hover:underline">open the config commands in the CLI Console</Link>.
      </p>

      {creating && (
        <NewContextDialog
          onClose={() => setCreating(false)}
          onCreate={(name, key, value) => {
            run.mutate({ command: 'config set', args: { key, value, context: name } })
            setTarget(name)
            setCreating(false)
          }}
          keys={env.data?.settings.map((s) => s.key) ?? []}
        />
      )}
      {deleting && (
        <DeleteContextDialog
          name={deleting}
          onClose={() => setDeleting(null)}
          onDelete={() => {
            run.mutate({ command: 'config delete-context', args: { name: deleting }, confirm: 'config delete-context' })
            if (target === deleting) setTarget(BASE)
            setDeleting(null)
          }}
        />
      )}
    </section>
  )
}

function NewContextDialog({ keys, onClose, onCreate }: { keys: string[]; onClose: () => void; onCreate: (name: string, key: string, value: string) => void }) {
  const [name, setName] = useState('')
  const [key, setKey] = useState(keys[0] ?? 'control_plane_url')
  const [value, setValue] = useState('')
  const valid = /^[A-Za-z0-9_.-]{1,64}$/.test(name) && value.trim() !== ''
  const field = 'mt-1 block w-full rounded-md border border-border bg-transparent px-2 py-1 text-xs'
  return (
    <Dialog
      title="New context"
      subtitle={<code>exa config set KEY VALUE --context NAME</code>}
      onClose={onClose}
      size="md"
      footer={
        <>
          <button type="button" onClick={onClose} className="rounded-md border border-border px-3 py-1 text-xs hover:bg-muted">Cancel</button>
          <button type="button" disabled={!valid} onClick={() => onCreate(name, key, value)} className="rounded-md border border-primary bg-primary px-3 py-1 text-xs text-primary-foreground disabled:opacity-50">
            Create
          </button>
        </>
      }
    >
      <div className="space-y-3 text-xs">
        <p className="text-muted-foreground">A context is a named environment that overrides some of the base settings. Start it with its first override; it is not made active.</p>
        <label className="block">Name<input aria-label="Context name" value={name} onChange={(e) => setName(e.target.value)} placeholder="staging" className={field} /></label>
        <label className="block">
          First setting
          <select aria-label="Setting" value={key} onChange={(e) => setKey(e.target.value)} className={field}>
            {keys.map((k) => <option key={k}>{k}</option>)}
          </select>
        </label>
        <label className="block">Value<input aria-label="Value" type={SECRET.test(key) ? 'password' : 'text'} value={value} onChange={(e) => setValue(e.target.value)} className={`${field} font-mono`} /></label>
      </div>
    </Dialog>
  )
}

function DeleteContextDialog({ name, onClose, onDelete }: { name: string; onClose: () => void; onDelete: () => void }) {
  const [typed, setTyped] = useState('')
  return (
    <Dialog
      title={`Delete context ${name}`}
      subtitle={<code>exa config delete-context {name}</code>}
      onClose={onClose}
      size="md"
      footer={
        <>
          <button type="button" onClick={onClose} className="rounded-md border border-border px-3 py-1 text-xs hover:bg-muted">Cancel</button>
          <button type="button" disabled={typed !== name} onClick={onDelete} className="rounded-md border border-border px-3 py-1 text-xs text-[color:var(--error-text)] disabled:opacity-50">
            Delete
          </button>
        </>
      }
    >
      <label className="block text-xs">
        This removes every value in the context. Type <code>{name}</code> to confirm.
        <input aria-label="Type the context name to confirm" value={typed} onChange={(e) => setTyped(e.target.value)} className="mt-1 block w-full rounded-md border border-border bg-transparent px-2 py-1 font-mono" />
      </label>
    </Dialog>
  )
}
