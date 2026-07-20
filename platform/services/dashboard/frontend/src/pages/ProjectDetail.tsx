import { useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import {
  ArrowLeft, FolderKanban, Layers, Users, Cpu, DollarSign, Gauge,
  PlusCircle, X, Clock, UserPlus, Plug, FlaskConical, Lock, Check,
  Play, Square, HardDrive, Workflow, Trash2, PlugZap, Pencil, Boxes,
} from 'lucide-react'
import { EmptyState } from '@/components/ui/empty-state'
import { isAdmin } from '@/lib/auth'
import {
  useProject, useAssignResource, useAddMember, useRemoveMember, useDeleteProject,
  useBindStorage, useUpdateProject, statusToken, budgetUsage,
  storageUsagePct, bytesToGb,
  RESOURCE_KINDS, MEMBER_ROLES,
  type AssignResourceBody, type AddMemberBody, type UpdateProjectBody, type ProjectDetail as ProjectDetailT,
} from '@/lib/projects'
import {
  useConnections, useCreateConnection, useDeleteConnection, useTestConnection,
  CONNECTION_KINDS, type CreateConnectionBody, type ConnectionKind,
} from '@/lib/connections'
import {
  useWorkbenches, useSetWorkbenchStatus, useCreateWorkbench, useDeleteWorkbench,
  nextStatus, type CreateWorkbenchBody,
} from '@/lib/workbenches'

const STATUS_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  ok:       { bg: 'oklch(0.72 0.18 155 / 12%)', border: 'oklch(0.72 0.18 155 / 30%)', text: 'var(--success-text)' },
  warn:     { bg: 'oklch(0.78 0.18 55 / 12%)',  border: 'oklch(0.78 0.18 55 / 30%)',  text: 'var(--warning-text)' },
  critical: { bg: 'oklch(0.66 0.22 25 / 12%)',  border: 'oklch(0.66 0.22 25 / 25%)',  text: 'var(--error-text)'   },
  unknown:  { bg: 'var(--surface-2)',           border: 'var(--border-md)',           text: 'var(--faint-text)'   },
}

function SectionCard({ icon: Icon, title, action, children }: {
  icon: typeof Layers; title: string; action?: React.ReactNode; children: React.ReactNode
}) {
  return (
    <div className="rounded-xl overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      <div className="px-4 py-3 flex items-center gap-2"
        style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border-sm)' }}>
        <Icon className="w-4 h-4" style={{ color: 'var(--accent-text)' }} />
        <span className="text-sm font-semibold">{title}</span>
        {action && <div className="ml-auto">{action}</div>}
      </div>
      <div className="p-4" style={{ background: 'var(--surface-0)' }}>{children}</div>
    </div>
  )
}

function QuotaMetric({ label, value, unit }: { label: string; value: number; unit: string }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground mb-1">{label}</p>
      <p className="text-sm font-mono">
        <strong>{value}</strong> <span className="text-muted-foreground">{unit}</span>
      </p>
    </div>
  )
}

function AssignResourceModal({ name, onClose }: { name: string; onClose: () => void }) {
  const [form, setForm] = useState<AssignResourceBody>({ kind: RESOURCE_KINDS[0], ref: '' })
  const assign = useAssignResource(name)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async () => {
    setError(null)
    try {
      await assign.mutateAsync(form)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to assign resource')
    }
  }

  return (
    <Modal title="Assign Resource" cli={`exa project assign ${name} --kind ${form.kind} --ref ${form.ref || '<ref>'}`} onClose={onClose}
      onSubmit={handleSubmit} submitting={assign.isPending} disabled={!form.ref.trim()} error={error}>
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Kind</label>
        <select value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value })}
          className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none"
          style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }}>
          {RESOURCE_KINDS.map(k => <option key={k} value={k}>{k}</option>)}
        </select>
      </div>
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Reference</label>
        <input value={form.ref} onChange={e => setForm({ ...form, ref: e.target.value })}
          placeholder="e.g. JPCP"
          className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none font-mono"
          style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }} />
      </div>
    </Modal>
  )
}

function AddMemberModal({ name, onClose }: { name: string; onClose: () => void }) {
  const [form, setForm] = useState<AddMemberBody>({ subject: '', role: MEMBER_ROLES[2] })
  const add = useAddMember(name)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async () => {
    setError(null)
    try {
      await add.mutateAsync(form)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to add member')
    }
  }

  return (
    <Modal title="Add Member" cli={`exa project add-member ${name} --subject ${form.subject || '<subject>'} --role ${form.role}`} onClose={onClose}
      onSubmit={handleSubmit} submitting={add.isPending} disabled={!form.subject.trim()} error={error}>
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Subject</label>
        <input value={form.subject} onChange={e => setForm({ ...form, subject: e.target.value })}
          placeholder="user@example.com"
          className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none"
          style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }} />
      </div>
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Role</label>
        <select value={form.role} onChange={e => setForm({ ...form, role: e.target.value })}
          className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none"
          style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }}>
          {MEMBER_ROLES.map(r => <option key={r} value={r}>{r}</option>)}
        </select>
      </div>
    </Modal>
  )
}

function CreateConnectionModal({ project, onClose }: { project: string; onClose: () => void }) {
  const [name, setName] = useState('')
  const [kind, setKind] = useState<ConnectionKind>(CONNECTION_KINDS[0])
  const [configText, setConfigText] = useState('{}')
  const [secret, setSecret] = useState('')
  const [error, setError] = useState<string | null>(null)
  const create = useCreateConnection(project)

  const handleSubmit = async () => {
    setError(null)
    let config: Record<string, unknown>
    try {
      config = configText.trim() ? JSON.parse(configText) : {}
    } catch {
      setError('Config must be valid JSON.')
      return
    }
    const body: CreateConnectionBody = { name: name.trim(), kind, project, config }
    if (secret) body.secret = secret
    try {
      await create.mutateAsync(body)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create connection')
    }
  }

  const cli =
    `exa connection create ${name || '<name>'} --kind ${kind} --project ${project}` +
    (secret ? ' --secret-value ***' : '')

  return (
    <Modal title="New Connection" cli={cli} onClose={onClose} onSubmit={handleSubmit}
      submitting={create.isPending} disabled={!name.trim()} error={error}>
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Name</label>
        <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. minio-data"
          className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none font-mono"
          style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }} />
      </div>
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Kind</label>
        <select value={kind} onChange={e => setKind(e.target.value as ConnectionKind)}
          className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none"
          style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }}>
          {CONNECTION_KINDS.map(k => <option key={k} value={k}>{k}</option>)}
        </select>
      </div>
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Config (JSON, non-secret)</label>
        <textarea value={configText} onChange={e => setConfigText(e.target.value)} rows={3}
          placeholder='{"endpoint":"http://localhost:19000","bucket":"data","access_key":"minioadmin"}'
          className="w-full rounded-lg px-3 py-1.5 text-xs focus:outline-none font-mono"
          style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }} />
      </div>
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Secret (optional — stored encrypted, never shown again)</label>
        <input type="password" value={secret} onChange={e => setSecret(e.target.value)} autoComplete="new-password"
          placeholder="credential / access secret"
          className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none font-mono"
          style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }} />
      </div>
    </Modal>
  )
}

function Modal({ title, cli, onClose, onSubmit, submitting, disabled, error, children }: {
  title: string; cli: string; onClose: () => void; onSubmit: () => void
  submitting: boolean; disabled: boolean; error: string | null; children: React.ReactNode
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'oklch(0 0 0 / 55%)' }}>
      <div className="w-full max-w-md rounded-xl overflow-hidden" style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}>
        <div className="flex items-center justify-between px-4 py-3" style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border)' }}>
          <h2 className="text-sm font-semibold">{title}</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-4 space-y-3">
          <p className="text-[11px] font-mono px-2 py-1 rounded"
            style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-sm)', color: 'var(--accent-text)' }}>
            {cli}
          </p>
          {children}
          {error && (
            <p className="text-xs rounded-lg px-3 py-2"
              style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
              {error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 px-4 py-3" style={{ background: 'var(--surface-1)', borderTop: '1px solid var(--border)' }}>
          <button onClick={onClose} className="px-3 py-1.5 rounded-lg text-xs font-medium"
            style={{ background: 'var(--surface-2)', border: '1px solid var(--border-md)', color: 'var(--subtle-text)' }}>
            Cancel
          </button>
          <button onClick={onSubmit} disabled={submitting || disabled}
            className="px-3 py-1.5 rounded-lg text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ background: 'oklch(0.64 0.20 265)', border: '1px solid oklch(0.64 0.20 265 / 60%)', color: 'oklch(0.99 0 0)' }}>
            {submitting ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

function ConnectionsCard({ project, admin }: { project: string; admin: boolean }) {
  const { data, isLoading } = useConnections(project)
  const [showNew, setShowNew] = useState(false)
  const del = useDeleteConnection(project)
  const test = useTestConnection(project)
  const [probe, setProbe] = useState<{ name: string; ok: boolean; detail: string } | null>(null)

  const runTest = async (name: string) => {
    setProbe(null)
    try {
      const r = await test.mutateAsync(name)
      setProbe({ name, ok: r.ok, detail: r.detail })
    } catch (e) {
      setProbe({ name, ok: false, detail: e instanceof Error ? e.message : 'test failed' })
    }
  }

  const newBtn = admin ? (
    <button onClick={() => setShowNew(true)}
      className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium"
      style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
      <PlusCircle className="w-3 h-3" /> New connection
    </button>
  ) : undefined

  return (
    <SectionCard icon={Plug} title="Connections" action={newBtn}>
      {isLoading ? (
        <p className="text-sm text-muted-foreground italic">Loading connections…</p>
      ) : data && data.length > 0 ? (
        <div className="space-y-3">
          <div className="rounded-lg overflow-hidden" style={{ border: '1px solid var(--border-sm)' }}>
            <table className="w-full text-sm">
              <thead>
                <tr style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border)' }}>
                  {['Name', 'Kind', 'Secret', 'Created by', ...(admin ? [''] : [])].map((h, i) => (
                    <th key={h || `act-${i}`} className="text-left px-4 py-2 text-xs font-semibold text-muted-foreground">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.map((c, i) => {
                  const busy = del.isPending && del.variables === c.name
                  return (
                  <tr key={c.name} style={{
                    background: i % 2 === 0 ? 'var(--surface-0)' : 'var(--surface-1)',
                    borderBottom: '1px solid var(--border-sm)',
                  }}>
                    <td className="px-4 py-2.5 font-mono text-xs">{c.name}</td>
                    <td className="px-4 py-2.5 text-xs">
                      <span className="text-[11px] px-2 py-0.5 rounded-md font-mono"
                        style={{ background: 'var(--surface-2)', border: '1px solid var(--border-sm)', color: 'var(--subtle-text)' }}>
                        {c.kind}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-xs">
                      {c.hasSecret ? (
                        <span className="inline-flex items-center gap-1" style={{ color: 'var(--warning-text)' }}>
                          <Lock className="w-3 h-3" /> secret set
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-muted-foreground">
                          <Check className="w-3 h-3" /> none
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-xs text-muted-foreground font-mono">{c.createdBy}</td>
                    {admin && (
                      <td className="px-4 py-2.5 text-xs">
                        <div className="flex items-center gap-1.5 justify-end">
                          <button onClick={() => runTest(c.name)} title="Test reachability"
                            className="inline-flex items-center gap-1 rounded-md px-2 py-0.5"
                            style={{ background: 'var(--surface-2)', border: '1px solid var(--border-sm)', color: 'var(--subtle-text)' }}>
                            <PlugZap className="w-3 h-3" /> Test
                          </button>
                          <button onClick={() => del.mutate(c.name)} disabled={busy} title="Delete connection"
                            className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 disabled:opacity-50"
                            style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
                            <Trash2 className="w-3 h-3" /> {busy ? '…' : 'Delete'}
                          </button>
                        </div>
                      </td>
                    )}
                  </tr>
                )})}
              </tbody>
            </table>
          </div>
          {probe && (
            <p className="text-[11px] rounded-lg px-3 py-2 font-mono"
              style={probe.ok
                ? { background: 'oklch(0.72 0.18 155 / 12%)', border: '1px solid oklch(0.72 0.18 155 / 30%)', color: 'var(--success-text)' }
                : { background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
              {probe.name}: {probe.ok ? 'reachable' : 'unreachable'} — {probe.detail}
            </p>
          )}
          <p className="text-[11px] text-muted-foreground">
            Secret values are stored encrypted and never returned to the browser — the dashboard
            writes them through the same path as{' '}
            <code className="font-mono" style={{ color: 'var(--accent-text)' }}>exa connection create</code>.
          </p>
        </div>
      ) : (
        <EmptyState icon={Plug} title="No connections yet"
          description={admin
            ? 'Add a project-scoped S3 / URI / dataplane connection with “New connection”.'
            : 'No connections defined. An admin can add one, or use `exa connection create`.'} />
      )}
      {showNew && <CreateConnectionModal project={project} onClose={() => setShowNew(false)} />}
    </SectionCard>
  )
}

function CreateWorkbenchModal({ project, onClose }: { project: string; onClose: () => void }) {
  const [name, setName] = useState('')
  const [image, setImage] = useState('')
  const [error, setError] = useState<string | null>(null)
  const create = useCreateWorkbench(project)

  const handleSubmit = async () => {
    setError(null)
    const body: CreateWorkbenchBody = { name: name.trim() }
    if (image.trim()) body.image = image.trim()
    try {
      await create.mutateAsync(body)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create workbench')
    }
  }

  const cli = `exa workbench create ${name || '<name>'} --project ${project}` +
    (image.trim() ? ` --image ${image.trim()}` : '')
  return (
    <Modal title="New Workbench" cli={cli} onClose={onClose} onSubmit={handleSubmit}
      submitting={create.isPending} disabled={!name.trim()} error={error}>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-muted-foreground">Name</span>
        <input value={name} onChange={e => setName(e.target.value)} placeholder="notebook"
          className="w-full rounded-lg px-3 py-2 text-sm font-mono"
          style={{ background: 'var(--surface-1)', border: '1px solid var(--border-md)' }} />
      </label>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-muted-foreground">Image (optional)</span>
        <input value={image} onChange={e => setImage(e.target.value)}
          placeholder="jupyter/scipy-notebook:latest"
          className="w-full rounded-lg px-3 py-2 text-sm font-mono"
          style={{ background: 'var(--surface-1)', border: '1px solid var(--border-md)' }} />
      </label>
      <p className="text-[11px] text-muted-foreground">
        Creates a project-bound notebook with its own persistent volume
        (<span className="font-mono">{project}-{name || '<name>'}-data</span>). Start it to inject
        the project's connections as environment variables.
      </p>
    </Modal>
  )
}

function WorkbenchesCard({ project, admin }: { project: string; admin: boolean }) {
  const { data, isLoading } = useWorkbenches(project)
  const setStatus = useSetWorkbenchStatus(project)
  const del = useDeleteWorkbench(project)
  const [showNew, setShowNew] = useState(false)

  const newBtn = admin ? (
    <button onClick={() => setShowNew(true)}
      className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium"
      style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
      <PlusCircle className="w-3 h-3" /> New workbench
    </button>
  ) : undefined

  return (
    <SectionCard icon={FlaskConical} title="Workbenches" action={newBtn}>
      {isLoading ? (
        <p className="text-sm text-muted-foreground italic">Loading workbenches…</p>
      ) : data && data.length > 0 ? (
        <div className="space-y-2">
          {data.map(wb => {
            const running = wb.status === 'RUNNING'
            const next = nextStatus(wb.status)
            const busy = setStatus.isPending && setStatus.variables?.name === wb.name
            const delBusy = del.isPending && del.variables === wb.name
            return (
              <div key={wb.name}
                className="flex items-center gap-3 rounded-lg px-3 py-2.5"
                style={{ background: 'var(--surface-1)', border: '1px solid var(--border-sm)' }}>
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-mono text-sm">{wb.name}</span>
                    <span className="text-[11px] font-medium px-2 py-0.5 rounded-full uppercase tracking-wide"
                      style={running
                        ? { background: 'oklch(0.72 0.18 155 / 12%)', border: '1px solid oklch(0.72 0.18 155 / 30%)', color: 'var(--success-text)' }
                        : { background: 'var(--surface-2)', border: '1px solid var(--border-md)', color: 'var(--faint-text)' }}>
                      {wb.status}
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground font-mono truncate">{wb.image}</p>
                  {wb.volume && (
                    <p className="text-[11px] text-muted-foreground font-mono truncate flex items-center gap-1">
                      <HardDrive className="w-3 h-3 shrink-0" /> {wb.volume}
                    </p>
                  )}
                </div>
                {admin && (
                  <div className="flex items-center gap-1.5 shrink-0">
                    <button
                      onClick={() => setStatus.mutate({ name: wb.name, status: next })}
                      disabled={busy}
                      className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                      style={running
                        ? { background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }
                        : { background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
                      {running ? <Square className="w-3 h-3" /> : <Play className="w-3 h-3" />}
                      {busy ? '…' : running ? 'Stop' : 'Start'}
                    </button>
                    <button
                      onClick={() => {
                        if (!window.confirm(`Delete workbench "${wb.name}"? Its volume is not deleted.`)) return
                        del.mutate(wb.name)
                      }}
                      disabled={delBusy} title="Delete workbench"
                      className="inline-flex items-center rounded-lg px-2 py-1 text-xs font-medium disabled:opacity-50"
                      style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ) : (
        <EmptyState icon={FlaskConical} title="No workbenches yet"
          description={admin
            ? 'Click "New workbench" to spin up an interactive notebook scoped to this project.'
            : 'Spin one up with `exa workbench create` to get an interactive environment scoped to this project.'} />
      )}
      {showNew && <CreateWorkbenchModal project={project} onClose={() => setShowNew(false)} />}
    </SectionCard>
  )
}

function BindStorageModal({ project, onClose }: { project: string; onClose: () => void }) {
  const { data: conns } = useConnections(project)
  const [connectionRef, setConnectionRef] = useState('')
  const [error, setError] = useState<string | null>(null)
  const bind = useBindStorage(project)

  const handleSubmit = async () => {
    setError(null)
    try {
      await bind.mutateAsync(connectionRef ? { connectionRef } : {})
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to bind storage')
    }
  }

  const cli = `exa project storage ${project}` + (connectionRef ? ` --connection ${connectionRef}` : '')

  return (
    <Modal title="Provision / Bind Storage" cli={cli} onClose={onClose} onSubmit={handleSubmit}
      submitting={bind.isPending} disabled={false} error={error}>
      <p className="text-xs text-muted-foreground">
        Ensures the per-project MinIO storage layout exists and (optionally) binds one of this
        project's connections to it.
      </p>
      <div className="space-y-1">
        <label className="text-xs text-muted-foreground">Connection (optional)</label>
        <select value={connectionRef} onChange={e => setConnectionRef(e.target.value)}
          className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none"
          style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }}>
          <option value="">— none (provision only) —</option>
          {(conns ?? []).map(c => <option key={c.name} value={c.name}>{c.name} ({c.kind})</option>)}
        </select>
      </div>
    </Modal>
  )
}

function StorageCard({ project, storage, admin, onBind }: {
  project: string
  storage: { bucket: string; prefix: string; quotaGb: number | null; usedBytes: number; connectionRef: string | null } | null | undefined
  admin: boolean
  onBind: () => void
}) {
  const bindBtn = admin ? (
    <button onClick={onBind}
      className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium"
      style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
      <PlugZap className="w-3 h-3" /> {storage ? 'Bind connection' : 'Provision storage'}
    </button>
  ) : undefined
  void project
  return (
    <SectionCard icon={HardDrive} title="Storage" action={bindBtn}>
      {storage ? (
        <div className="space-y-3">
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4">
            <QuotaMetric label="Used" value={Number((storage.usedBytes / 1e9).toFixed(2))} unit="GB" />
            <QuotaMetric label="Quota" value={storage.quotaGb ?? 0} unit="GB" />
            <div>
              <p className="text-xs uppercase tracking-wider text-muted-foreground mb-1">Connection</p>
              <p className="text-sm font-mono">{storage.connectionRef ?? '—'}</p>
            </div>
          </div>
          <p className="text-xs font-mono text-muted-foreground break-all">
            s3://{storage.bucket}/{storage.prefix} · {bytesToGb(storage.usedBytes)} used ({storageUsagePct(storage)}% of quota)
          </p>
          <div className="h-2 rounded-full overflow-hidden" style={{ background: 'var(--surface-2)' }}>
            <div className="h-full rounded-full"
              style={{ width: `${storageUsagePct(storage)}%`, background: 'oklch(0.64 0.20 265)' }} />
          </div>
        </div>
      ) : (
        <EmptyState icon={HardDrive} title="No storage provisioned"
          description={admin
            ? 'Provision the per-project MinIO layout with “Provision storage”.'
            : 'No project storage yet. An admin can provision it, or use `exa project storage`.'} />
      )}
    </SectionCard>
  )
}

function EditProjectModal({ project, onClose }: { project: ProjectDetailT; onClose: () => void }) {
  const [description, setDescription] = useState(project.description ?? '')
  const [cpuLimit, setCpuLimit] = useState(String(project.quota.cpuLimit))
  const [memoryLimitGb, setMemoryLimitGb] = useState(String(project.quota.memoryLimitGb))
  const [storageGb, setStorageGb] = useState(String(project.quota.storageGb))
  const [gpuLimit, setGpuLimit] = useState(String(project.quota.gpuLimit))
  const [networkName, setNetworkName] = useState(project.namespace ?? '')
  const [gpuHoursBudget, setGpuHoursBudget] = useState(
    project.budget ? String(project.budget.gpuHours) : '',
  )
  const [costBudget, setCostBudget] = useState(
    project.budget ? String(project.budget.costUsd) : '',
  )
  const [error, setError] = useState<string | null>(null)
  const update = useUpdateProject(project.name)

  const handleSubmit = async () => {
    setError(null)
    const body: UpdateProjectBody = { description, networkName }
    const num = (s: string) => (s.trim() === '' ? undefined : Number(s))
    body.cpuLimit = num(cpuLimit)
    body.memoryLimitGb = num(memoryLimitGb)
    body.storageGb = num(storageGb)
    body.gpuLimit = num(gpuLimit)
    if (gpuHoursBudget.trim() !== '' || costBudget.trim() !== '') {
      body.gpuHoursBudget = num(gpuHoursBudget)
      body.costBudget = num(costBudget)
    }
    try {
      await update.mutateAsync(body)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to update project')
    }
  }

  const numField = (
    label: string, value: string, set: (v: string) => void, unit: string,
  ) => (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-muted-foreground">{label} <span className="text-[10px]">({unit})</span></span>
      <input type="number" min="0" value={value} onChange={e => set(e.target.value)}
        className="w-full rounded-lg px-3 py-2 text-sm font-mono"
        style={{ background: 'var(--surface-1)', border: '1px solid var(--border-md)' }} />
    </label>
  )

  const cli = `exa project quota ${project.name} --cpu ${cpuLimit} --memory ${memoryLimitGb} --storage ${storageGb} --gpu ${gpuLimit}`
  return (
    <Modal title="Edit Project" cli={cli} onClose={onClose} onSubmit={handleSubmit}
      submitting={update.isPending} disabled={false} error={error}>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-muted-foreground">Description</span>
        <input value={description} onChange={e => setDescription(e.target.value)}
          className="w-full rounded-lg px-3 py-2 text-sm"
          style={{ background: 'var(--surface-1)', border: '1px solid var(--border-md)' }} />
      </label>
      <div className="grid grid-cols-2 gap-3">
        {numField('CPU', cpuLimit, setCpuLimit, 'cores')}
        {numField('Memory', memoryLimitGb, setMemoryLimitGb, 'GB')}
        {numField('Storage', storageGb, setStorageGb, 'GB')}
        {numField('GPU', gpuLimit, setGpuLimit, 'units')}
      </div>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-muted-foreground">Namespace / network</span>
        <input value={networkName} onChange={e => setNetworkName(e.target.value)}
          placeholder={`examlops-${project.name}`}
          className="w-full rounded-lg px-3 py-2 text-sm font-mono"
          style={{ background: 'var(--surface-1)', border: '1px solid var(--border-md)' }} />
        <span className="text-[10px] text-muted-foreground">Isolated namespace the project's resources bind into.</span>
      </label>
      <div className="grid grid-cols-2 gap-3">
        {numField('GPU-hour budget', gpuHoursBudget, setGpuHoursBudget, 'h')}
        {numField('Cost budget', costBudget, setCostBudget, 'USD')}
      </div>
    </Modal>
  )
}

export function ProjectDetail() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const { data, isLoading, error } = useProject(name ?? '')
  const [modal, setModal] = useState<'resource' | 'member' | 'storage' | 'edit' | null>(null)
  const admin = isAdmin()
  const removeMember = useRemoveMember(name ?? '')
  const delProject = useDeleteProject()

  if (isLoading) return (
    <div className="p-6 space-y-4 max-w-4xl mx-auto">
      {[...Array(4)].map((_, i) => (
        <div key={i} className="h-20 rounded-xl animate-pulse"
          style={{ background: 'var(--surface-1)', border: '1px solid var(--border-sm)' }} />
      ))}
    </div>
  )

  if (error || !data) return (
    <div className="p-6 max-w-4xl mx-auto">
      <p className="text-sm rounded-lg px-4 py-3"
        style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
        Failed to load project "{name}".
      </p>
    </div>
  )

  const sc = STATUS_COLORS[statusToken(data.status)] ?? STATUS_COLORS.unknown
  const resourceKinds = Object.entries(data.resources).filter(([, refs]) => refs.length > 0)
  const usage = budgetUsage(data.budget, data.consumption)

  const addResourceBtn = admin ? (
    <button onClick={() => setModal('resource')}
      className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium"
      style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
      <PlusCircle className="w-3 h-3" /> Assign resource
    </button>
  ) : undefined

  const addMemberBtn = admin ? (
    <button onClick={() => setModal('member')}
      className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium"
      style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
      <UserPlus className="w-3 h-3" /> Add member
    </button>
  ) : undefined

  const editBtn = admin ? (
    <button onClick={() => setModal('edit')}
      className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-medium"
      style={{ background: 'oklch(0.64 0.20 265 / 12%)', border: '1px solid oklch(0.64 0.20 265 / 25%)', color: 'var(--accent-text)' }}>
      <Pencil className="w-3 h-3" /> Edit
    </button>
  ) : undefined

  return (
    <div className="p-6 space-y-5 max-w-4xl mx-auto">
      <Link to="/projects" className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors">
        <ArrowLeft className="w-3.5 h-3.5" /> Projects
      </Link>

      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-2">
          <div className="flex items-center gap-2.5 flex-wrap">
            <FolderKanban className="w-5 h-5" style={{ color: 'var(--accent-text)' }} />
            <h1 className="text-2xl font-bold">{data.name}</h1>
            <span className="text-[11px] font-medium px-2 py-0.5 rounded-full uppercase tracking-wide"
              style={{ background: sc.bg, border: `1px solid ${sc.border}`, color: sc.text }}>
              {data.status}
            </span>
          </div>
          {data.description && <p className="text-sm text-muted-foreground max-w-2xl">{data.description}</p>}
          {data.namespace && (
            <p className="inline-flex items-center gap-1.5 text-[11px] font-mono px-2 py-0.5 rounded-md"
              style={{ background: 'var(--surface-2)', border: '1px solid var(--border-sm)', color: 'var(--subtle-text)' }}>
              <Boxes className="w-3 h-3" /> {data.namespace}
            </p>
          )}
        </div>
        <div className="text-right text-xs text-muted-foreground shrink-0 space-y-0.5">
          <p className="flex items-center gap-1 justify-end"><Clock className="w-3 h-3" /> {String(data.createdAt).slice(0, 19).replace('T', ' ')}</p>
          <p>by {data.createdBy}</p>
        </div>
      </div>

      {/* Quota */}
      <SectionCard icon={Cpu} title="Quota" action={editBtn}>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <QuotaMetric label="CPU" value={data.quota.cpuLimit} unit="cores" />
          <QuotaMetric label="Memory" value={data.quota.memoryLimitGb} unit="GB" />
          <QuotaMetric label="Storage" value={data.quota.storageGb} unit="GB" />
          <QuotaMetric label="GPU" value={data.quota.gpuLimit} unit="units" />
        </div>
      </SectionCard>

      {/* Budget vs Consumption */}
      <SectionCard icon={DollarSign} title="Budget & Consumption" action={editBtn}>
        {data.budget ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              <QuotaMetric label="GPU-hour budget" value={data.budget.gpuHours} unit="h" />
              <QuotaMetric label="Cost budget" value={data.budget.costUsd} unit="USD" />
              <QuotaMetric label="GPU-hours used" value={data.consumption.gpu_hours} unit="h" />
              <QuotaMetric label="Cost to date" value={data.consumption.cost_usd} unit="USD" />
            </div>
            {usage !== null && (
              <div className="space-y-1">
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span className="flex items-center gap-1"><Gauge className="w-3 h-3" /> GPU-hour budget usage</span>
                  <span className="font-mono">{Math.round(usage * 100)}%</span>
                </div>
                <div className="h-2 rounded-full overflow-hidden" style={{ background: 'var(--surface-2)' }}>
                  <div className="h-full rounded-full"
                    style={{
                      width: `${Math.min(usage * 100, 100)}%`,
                      background: usage >= 1 ? 'var(--error-text)' : usage >= 0.8 ? 'var(--warning-text)' : 'oklch(0.64 0.20 265)',
                    }} />
                </div>
              </div>
            )}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No budget configured. Consumption to date: {data.consumption.gpu_hours} GPU-hours · ${data.consumption.cost_usd}.</p>
        )}
      </SectionCard>

      {/* Storage (P6) — admins can provision + bind a connection */}
      <StorageCard project={data.name} storage={data.storage} admin={admin} onBind={() => setModal('storage')} />

      {/* Pipelines (P7): the project's Prefect training + Ray Serve serving surfaces */}
      {data.pipelines && (data.pipelines.prefect || data.pipelines.rayserve) && (
        <SectionCard icon={Workflow} title="Pipelines">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {data.pipelines.prefect && (
              <div className="rounded-lg p-3" style={{ border: '1px solid var(--border-sm)' }}>
                <p className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Prefect · training</p>
                <p className="text-sm">{data.pipelines.prefect.deployments.length} deployment(s)</p>
                <p className="text-xs text-muted-foreground font-mono">
                  {data.pipelines.prefect.schedule ?? 'no schedule'} · {data.pipelines.prefect.status}
                </p>
              </div>
            )}
            {data.pipelines.rayserve && (
              <div className="rounded-lg p-3" style={{ border: '1px solid var(--border-sm)' }}>
                <p className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Ray Serve · serving</p>
                <p className="text-sm">{data.pipelines.rayserve.models.length} served model(s)</p>
                <p className="text-xs text-muted-foreground font-mono">
                  {Object.keys(data.pipelines.rayserve.traffic).length} traffic rule(s) · {data.pipelines.rayserve.status}
                </p>
              </div>
            )}
          </div>
        </SectionCard>
      )}

      {/* Resources grouped by kind */}
      <SectionCard icon={Layers} title="Resources" action={addResourceBtn}>
        {resourceKinds.length > 0 ? (
          <div className="space-y-3">
            {resourceKinds.map(([kind, refs]) => (
              <div key={kind}>
                <p className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">{kind.replace(/_/g, ' ')}</p>
                <div className="flex flex-wrap gap-1.5">
                  {refs.map(ref => (
                    <span key={ref} className="text-[11px] px-2 py-0.5 rounded-md font-mono"
                      style={{ background: 'var(--surface-2)', border: '1px solid var(--border-sm)', color: 'var(--subtle-text)' }}>
                      {ref}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground italic">No resources assigned to this project.</p>
        )}
      </SectionCard>

      {/* Members */}
      <SectionCard icon={Users} title="Members" action={addMemberBtn}>
        {data.members.length > 0 ? (
          <div className="rounded-lg overflow-hidden" style={{ border: '1px solid var(--border-sm)' }}>
            <table className="w-full text-sm">
              <thead>
                <tr style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border)' }}>
                  {['Subject', 'Role', 'Granted by', 'When', ...(admin ? [''] : [])].map((h, i) => (
                    <th key={h || `act-${i}`} className="text-left px-4 py-2 text-xs font-semibold text-muted-foreground">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.members.map((m, i) => {
                  const busy = removeMember.isPending && removeMember.variables === m.subject
                  return (
                  <tr key={`${m.subject}-${i}`} style={{
                    background: i % 2 === 0 ? 'var(--surface-0)' : 'var(--surface-1)',
                    borderBottom: '1px solid var(--border-sm)',
                  }}>
                    <td className="px-4 py-2.5 font-mono text-xs">{m.subject}</td>
                    <td className="px-4 py-2.5 text-xs">{m.role}</td>
                    <td className="px-4 py-2.5 text-xs text-muted-foreground font-mono">{m.grantedBy}</td>
                    <td className="px-4 py-2.5 text-xs text-muted-foreground">{String(m.when).slice(0, 19).replace('T', ' ')}</td>
                    {admin && (
                      <td className="px-4 py-2.5 text-xs text-right">
                        <button onClick={() => removeMember.mutate(m.subject)} disabled={busy} title="Remove member"
                          className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 disabled:opacity-50"
                          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
                          <X className="w-3 h-3" /> {busy ? '…' : 'Remove'}
                        </button>
                      </td>
                    )}
                  </tr>
                )})}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon={Users} title="No members yet" description="Add members to grant access to this project." />
        )}
      </SectionCard>

      {/* Connections (viewers read; admins create/delete/test) */}
      <ConnectionsCard project={data.name} admin={admin} />

      {/* Workbenches (viewers see status; admins can start/stop) */}
      <WorkbenchesCard project={data.name} admin={admin} />

      {/* Danger zone — delete the project grouping (admin) */}
      {admin && (
        <SectionCard icon={Trash2} title="Danger zone">
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <p className="text-xs text-muted-foreground max-w-md">
              Delete this project and its membership/resource groupings. The underlying models,
              connections, and storage are <strong>not</strong> deleted — only the project.
            </p>
            <button
              onClick={async () => {
                if (!window.confirm(`Delete project "${data.name}"? This cannot be undone.`)) return
                try {
                  await delProject.mutateAsync(data.name)
                  navigate('/projects')
                } catch {
                  /* surfaced by the mutation error state */
                }
              }}
              disabled={delProject.isPending}
              className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium shrink-0 disabled:opacity-50"
              style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 30%)', color: 'var(--error-text)' }}>
              <Trash2 className="w-3.5 h-3.5" /> {delProject.isPending ? 'Deleting…' : 'Delete project'}
            </button>
          </div>
        </SectionCard>
      )}

      {modal === 'resource' && admin && <AssignResourceModal name={data.name} onClose={() => setModal(null)} />}
      {modal === 'member' && admin && <AddMemberModal name={data.name} onClose={() => setModal(null)} />}
      {modal === 'storage' && admin && <BindStorageModal project={data.name} onClose={() => setModal(null)} />}
      {modal === 'edit' && admin && <EditProjectModal project={data} onClose={() => setModal(null)} />}
    </div>
  )
}
