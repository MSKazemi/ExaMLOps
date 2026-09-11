import { useEffect, useRef, useState } from 'react'
import { Download, RotateCcw, Square } from 'lucide-react'
import { StatusPill } from '@/components/ui/status-pill'
import {
  cell,
  columnsOf,
  downloadWorkspaceFile,
  isTerminal,
  outputShape,
  useCancelCliRun,
  useCliRun,
  type CliRunDetail,
  type RunStatus,
} from '@/lib/cli'

const HEALTH: Record<RunStatus, string> = {
  queued: 'pending',
  running: 'pending',
  succeeded: 'healthy',
  failed: 'failed',
  timeout: 'failed',
  error: 'failed',
  cancelled: 'unknown',
}

const LABEL: Record<RunStatus, string> = {
  queued: 'Queued',
  running: 'Running…',
  succeeded: 'Succeeded',
  failed: 'Failed',
  timeout: 'Timed out',
  error: 'Could not start',
  cancelled: 'Cancelled',
}

/** A run's live status and its output, rendered as a table/record when the CLI returned JSON. */
export function RunOutput({
  runId,
  canDownload,
  onRerun,
}: {
  runId: string
  canDownload: boolean
  /** Load this run's arguments back into the form ("Run again"). */
  onRerun?: (run: CliRunDetail) => void
}) {
  const { data: run, error } = useCliRun(runId)
  const cancel = useCancelCliRun()
  const [raw, setRaw] = useState(false)

  if (error) return <p className="text-xs text-muted-foreground">Could not load this run.</p>
  if (!run) return <p className="text-xs text-muted-foreground">Starting…</p>

  const shape = outputShape(run.parsed, run.stdout)
  const done = isTerminal(run.status)

  return (
    <section aria-label="Run output" className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <StatusPill status={HEALTH[run.status]} label={LABEL[run.status]} />
        {run.exit_code !== null && <span className="text-muted-foreground">exit {run.exit_code}</span>}
        {run.duration_ms !== null && <span className="text-muted-foreground">{(run.duration_ms / 1000).toFixed(1)}s</span>}
        {run.error && <span className="text-muted-foreground">{run.error}</span>}
        {!done && (
          <button
            type="button"
            onClick={() => cancel.mutate(run.id)}
            className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 hover:bg-muted"
          >
            <Square className="size-3" aria-hidden="true" /> Cancel
          </button>
        )}
        {done && (
          <span className="ml-auto inline-flex items-center gap-3">
            {shape !== 'text' && shape !== 'empty' && (
              <label className="inline-flex items-center gap-1 text-muted-foreground">
                <input type="checkbox" checked={raw} onChange={(e) => setRaw(e.target.checked)} /> raw
              </label>
            )}
            {onRerun && (
              <button
                type="button"
                onClick={() => onRerun(run)}
                title="Load this run's arguments into the form, to review and run again"
                className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 hover:bg-muted"
              >
                <RotateCcw className="size-3" aria-hidden="true" /> Run again
              </button>
            )}
          </span>
        )}
      </div>

      <code className="block overflow-x-auto rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground">
        $ {run.display}
      </code>

      {!done && <LiveOutput run={run} />}
      {done && (raw ? <Pre text={run.stdout} /> : <Rendered shape={shape} parsed={run.parsed} stdout={run.stdout} />)}
      {run.truncated && (
        <p className="text-[11px] text-muted-foreground">Output was longer than the dashboard keeps and was cut off.</p>
      )}
      {done && run.stderr.trim() && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">stderr</summary>
          <Pre text={run.stderr} />
        </details>
      )}
      {done && run.files.length > 0 && (
        <div className="space-y-1 text-xs">
          <p className="text-muted-foreground">Files written to the CLI workspace:</p>
          <ul className="flex flex-wrap gap-2">
            {run.files.map((f) => (
              <li key={f}>
                <button
                  type="button"
                  disabled={!canDownload}
                  title={canDownload ? `Download ${f}` : 'Requires the admin role.'}
                  onClick={() => downloadWorkspaceFile(f).catch(() => {})}
                  className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-0.5 font-mono hover:bg-muted disabled:opacity-50"
                >
                  <Download className="size-3" aria-hidden="true" /> {f}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

/**
 * What a running command has printed so far. A JSON run's result is one document the CLI writes
 * when it finishes, so only its messages (stderr) can show before then. The view follows new
 * output unless the reader has scrolled up to read something.
 */
function LiveOutput({ run }: { run: CliRunDetail }) {
  const json = run.format === 'json'
  const text = json ? run.stderr : run.stdout
  const extra = json ? '' : run.stderr
  const ref = useRef<HTMLPreElement>(null)
  const follow = useRef(true)

  useEffect(() => {
    const el = ref.current
    if (el && follow.current) el.scrollTop = el.scrollHeight
  }, [text])

  return (
    <div className="space-y-1" aria-busy="true">
      {json && (
        <p className="text-[11px] text-muted-foreground">
          The result appears when the command finishes. Messages it prints meanwhile show below.
        </p>
      )}
      {text ? (
        <pre
          ref={ref}
          aria-label="Output so far"
          onScroll={(e) => {
            const el = e.currentTarget
            follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
          }}
          className="max-h-[28rem] overflow-auto rounded-md border border-border p-2 text-[11px] leading-relaxed whitespace-pre-wrap break-words"
        >
          {text}
        </pre>
      ) : (
        <p className="text-xs text-muted-foreground">Waiting for output…</p>
      )}
      {extra.trim() && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">stderr so far</summary>
          <Pre text={extra} />
        </details>
      )}
    </div>
  )
}

function Pre({ text }: { text: string }) {
  return (
    <pre className="max-h-[28rem] overflow-auto rounded-md border border-border p-2 text-[11px] leading-relaxed whitespace-pre-wrap break-words">
      {text || '(no output)'}
    </pre>
  )
}

function Rendered({ shape, parsed, stdout }: { shape: string; parsed: unknown; stdout: string }) {
  if (shape === 'empty') return <p className="text-xs text-muted-foreground">The command returned no rows.</p>
  if (shape === 'text') return <Pre text={stdout} />
  if (shape === 'table') {
    const rows = parsed as Record<string, unknown>[]
    const cols = columnsOf(rows)
    return (
      <div className="max-h-[28rem] overflow-auto rounded-md border border-border">
        <table className="w-full text-[11px]">
          <thead className="sticky top-0 bg-background">
            <tr>
              {cols.map((c) => (
                <th key={c} scope="col" className="border-b border-border px-2 py-1 text-left font-semibold">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="odd:bg-muted/30">
                {cols.map((c) => (
                  <td key={c} className="max-w-[24rem] truncate px-2 py-1 align-top" title={cell(r[c])}>
                    {cell(r[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        <p className="px-2 py-1 text-[10px] text-muted-foreground">{rows.length} row(s)</p>
      </div>
    )
  }
  if (shape === 'record') {
    const rec = parsed as Record<string, unknown>
    return (
      <dl className="grid max-h-[28rem] grid-cols-[minmax(8rem,auto)_1fr] gap-x-3 gap-y-1 overflow-auto rounded-md border border-border p-2 text-[11px]">
        {Object.entries(rec).map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="font-semibold">{k}</dt>
            <dd className="min-w-0">
              {v !== null && typeof v === 'object' ? (
                <pre className="whitespace-pre-wrap break-words">{JSON.stringify(v, null, 2)}</pre>
              ) : (
                cell(v)
              )}
            </dd>
          </div>
        ))}
      </dl>
    )
  }
  return <Pre text={JSON.stringify(parsed, null, 2)} />
}
