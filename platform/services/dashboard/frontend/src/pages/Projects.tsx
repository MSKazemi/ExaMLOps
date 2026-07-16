import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  FolderKanban, ChevronRight, Box, Layers, Users, Cpu, PlusCircle, X,
} from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { isAdmin } from '@/lib/auth'
import {
  useProjects, useCreateProject, statusToken, quotaSummary,
  type ProjectSummary, type CreateProjectBody,
} from '@/lib/projects'

const STATUS_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  ok:       { bg: 'oklch(0.72 0.18 155 / 12%)', border: 'oklch(0.72 0.18 155 / 30%)', text: 'var(--success-text)' },
  warn:     { bg: 'oklch(0.78 0.18 55 / 12%)',  border: 'oklch(0.78 0.18 55 / 30%)',  text: 'var(--warning-text)' },
  critical: { bg: 'oklch(0.66 0.22 25 / 12%)',  border: 'oklch(0.66 0.22 25 / 25%)',  text: 'var(--error-text)'   },
  unknown:  { bg: 'var(--surface-2)',           border: 'var(--border-md)',           text: 'var(--faint-text)'   },
}

function StatusBadge({ status }: { status: string }) {
  const c = STATUS_COLORS[statusToken(status)] ?? STATUS_COLORS.unknown
  return (
    <span
      className="text-[10px] font-medium px-2 py-0.5 rounded-full uppercase tracking-wide"
      style={{ background: c.bg, border: `1px solid ${c.border}`, color: c.text }}
    >
      {status}
    </span>
  )
}

function CountChip({ icon: Icon, value, label }: { icon: typeof Box; value: number; label: string }) {
  return (
    <div className="flex items-center gap-1.5 text-xs text-muted-foreground" title={label}>
      <Icon className="w-3.5 h-3.5" />
      <span className="font-mono text-foreground/80">{value}</span>
    </div>
  )
}

function ProjectCard({ project }: { project: ProjectSummary }) {
  const navigate = useNavigate()
  return (
    <div
      onClick={() => navigate(`/projects/${project.name}`)}
      className="rounded-xl overflow-hidden cursor-pointer card-hover"
      style={{ background: 'var(--surface-0)', border: '1px solid oklch(0.64 0.20 265 / 25%)' }}
    >
      <div className="h-1" style={{ background: 'linear-gradient(90deg, oklch(0.64 0.20 265), oklch(0.70 0.18 300))' }} />
      <div className="p-4 space-y-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="w-8 h-8 rounded-md flex items-center justify-center shrink-0"
              style={{ background: 'oklch(0.64 0.20 265 / 14%)', border: '1px solid oklch(0.64 0.20 265 / 25%)' }}>
              <FolderKanban className="w-3.5 h-3.5" style={{ color: 'var(--accent-text)' }} />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-sm truncate">{project.name}</span>
                <StatusBadge status={project.status} />
              </div>
              {project.description && (
                <p className="text-xs text-muted-foreground mt-0.5 truncate">{project.description}</p>
              )}
            </div>
          </div>
          <ChevronRight className="w-4 h-4 shrink-0" style={{ color: 'var(--faint-text)' }} />
        </div>

        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground font-mono">
          <Cpu className="w-3 h-3" />
          <span>{quotaSummary(project.quota)}</span>
        </div>

        <div className="flex items-center gap-4 pt-1">
          <CountChip icon={Box} value={project.modelCount} label="Models" />
          <CountChip icon={Layers} value={project.resourceCount} label="Resources" />
          <CountChip icon={Users} value={project.memberCount} label="Members" />
        </div>
      </div>
    </div>
  )
}

const EMPTY_FORM: CreateProjectBody = {
  name: '', description: '', cpuLimit: 8, memoryLimitGb: 32, storageGb: 100, gpuLimit: 1,
}

function CreateProjectModal({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState<CreateProjectBody>(EMPTY_FORM)
  const create = useCreateProject()
  const [error, setError] = useState<string | null>(null)

  const num = (v: string) => (v === '' ? 0 : Number(v))

  const handleSubmit = async () => {
    setError(null)
    try {
      await create.mutateAsync(form)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create project')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'oklch(0 0 0 / 55%)' }}>
      <div className="w-full max-w-md rounded-xl overflow-hidden" style={{ background: 'var(--surface-0)', border: '1px solid var(--border)' }}>
        <div className="flex items-center justify-between px-4 py-3" style={{ background: 'var(--surface-1)', borderBottom: '1px solid var(--border)' }}>
          <h2 className="text-sm font-semibold">New Project</h2>
          <button onClick={onClose} aria-label="Close" className="text-muted-foreground hover:text-foreground">
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-4 space-y-3">
          {/* GUI↔CLI parity */}
          <p className="text-[11px] font-mono px-2 py-1 rounded"
            style={{ background: 'var(--surface-deep)', border: '1px solid var(--border-sm)', color: 'var(--accent-text)' }}>
            exa project create {form.name || '<name>'}
          </p>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">Name</label>
            <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
              className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none"
              style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }} />
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">Description</label>
            <input value={form.description} onChange={e => setForm({ ...form, description: e.target.value })}
              className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none"
              style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            {([
              ['cpuLimit', 'CPU limit'],
              ['memoryLimitGb', 'Memory (GB)'],
              ['storageGb', 'Storage (GB)'],
              ['gpuLimit', 'GPU limit'],
            ] as const).map(([key, label]) => (
              <div key={key} className="space-y-1">
                <label className="text-xs text-muted-foreground">{label}</label>
                <input type="number" min={0} value={form[key]}
                  onChange={e => setForm({ ...form, [key]: num(e.target.value) })}
                  className="w-full rounded-lg px-3 py-1.5 text-sm focus:outline-none font-mono"
                  style={{ background: 'var(--input-bg)', border: '1px solid var(--border-md)', color: 'var(--foreground)' }} />
              </div>
            ))}
          </div>
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
          <button onClick={handleSubmit} disabled={create.isPending || !form.name.trim()}
            className="px-3 py-1.5 rounded-lg text-xs font-medium disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ background: 'oklch(0.64 0.20 265)', border: '1px solid oklch(0.64 0.20 265 / 60%)', color: 'oklch(0.99 0 0)' }}>
            {create.isPending ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  )
}

export function Projects() {
  const { data: projects, isLoading, error } = useProjects()
  const [showCreate, setShowCreate] = useState(false)
  const admin = isAdmin()

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Projects</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Multi-tenant projects — quota, assigned resources, members, and budget vs. consumption.
          </p>
        </div>
        {admin && (
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium"
            style={{ background: 'oklch(0.72 0.18 155 / 15%)', color: 'oklch(0.72 0.18 155)', border: '1px solid oklch(0.72 0.18 155 / 30%)' }}
          >
            <PlusCircle size={14} />
            New Project
          </button>
        )}
      </div>

      {showCreate && <CreateProjectModal onClose={() => setShowCreate(false)} />}

      {error && (
        <p className="text-sm rounded-lg px-4 py-3"
          style={{ background: 'oklch(0.66 0.22 25 / 12%)', border: '1px solid oklch(0.66 0.22 25 / 25%)', color: 'var(--error-text)' }}>
          Failed to load projects.
        </p>
      )}

      {isLoading && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" aria-label="Loading projects">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-32 w-full" />
          ))}
        </div>
      )}

      {!isLoading && projects && projects.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {projects.map(p => <ProjectCard key={p.name} project={p} />)}
        </div>
      )}

      {!isLoading && projects?.length === 0 && (
        <EmptyState
          icon={FolderKanban}
          title="No projects yet"
          description={admin ? 'Create a project to allocate quota and assign resources.' : 'No projects have been created yet.'}
        />
      )}
    </div>
  )
}
