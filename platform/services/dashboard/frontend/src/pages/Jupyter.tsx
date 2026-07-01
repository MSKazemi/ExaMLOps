import { useConfig, useContainers } from '@/lib/api'
import { ExternalLink } from 'lucide-react'

export function Jupyter() {
  const { data: configData } = useConfig()
  const { data: containersData } = useContainers()

  const config = (configData ?? {}) as Record<string, string>
  const hubUrl = config.jupyterhub_url || 'http://localhost:18888'
  const containers = containersData?.containers ?? []

  const jupyterContainer = containers.find((c) => c.name === 'jupyterhub')
  const isRunning = jupyterContainer?.status === 'running'

  const statusLabel = isRunning
    ? 'Running'
    : jupyterContainer
      ? 'Stopped'
      : 'Not started'

  const statusColor = isRunning
    ? 'oklch(0.72 0.18 155)'
    : jupyterContainer
      ? 'oklch(0.66 0.22 25)'
      : 'oklch(0.75 0.18 80)'

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold">JupyterHub</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Multi-user notebook server for interactive data exploration and model development.
        </p>
      </div>

      <div className="border rounded-lg p-6 space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span
              className="w-2 h-2 rounded-full"
              style={{ background: statusColor }}
            />
            <span className="text-sm font-medium">{statusLabel}</span>
            {!jupyterContainer && (
              <span className="text-xs text-muted-foreground">
                — separate Docker profile
              </span>
            )}
          </div>
          <a
            href={hubUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 px-4 py-2 rounded-md text-sm font-medium transition-colors"
            style={{
              background: 'oklch(0.55 0.22 265)',
              color: 'white',
            }}
          >
            <ExternalLink size={14} />
            Launch JupyterHub
          </a>
        </div>

        <div className="text-sm text-muted-foreground">
          URL:{' '}
          <code className="font-mono text-xs">{hubUrl}</code>
        </div>
      </div>

      {!isRunning && (
        <div className="border rounded-lg p-4 space-y-3">
          <h2 className="text-sm font-semibold">Start JupyterHub</h2>
          <p className="text-sm text-muted-foreground">
            JupyterHub runs under a separate Docker Compose profile. Start it with:
          </p>
          <pre className="rounded p-3 text-xs font-mono" style={{ background: 'var(--surface-2)' }}>
            make jupyter-up
          </pre>
          <p className="text-xs text-muted-foreground">
            Add users:{' '}
            <code className="font-mono">
              make jupyter-add-user USER=alice HUB_TOKEN=&lt;admin-api-token&gt;
            </code>
          </p>
        </div>
      )}
    </div>
  )
}
