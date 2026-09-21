import { useRef, useState, type FormEvent } from 'react'
import { useLocation } from 'react-router-dom'
import { Sparkles, X, Copy, Send, TriangleAlert } from 'lucide-react'
import { sanitizeMarkdown } from '@/lib/sanitize'
import { useFocusTrap } from '@/hooks/useFocusTrap'
import {
  buildContext,
  describeCopilotError,
  isDegraded,
  proposalGateLabel,
  useCopilotAsk,
  type CopilotResponse,
  type CopilotTurn,
} from '@/lib/copilot'

/**
 * CopilotAnswer — presentational render of one assistant response (F11 / ADR 0065).
 *
 * Answer text is sanitized (F16). Proposals are **advisory only**: each shows the `exa` command with a
 * copy button and an approval-gate badge — there is deliberately no "run" button; execution goes through
 * the normal authorized/approval/audited flow (R5). The agent trace is collapsible (R6).
 */
export function CopilotAnswer({ response }: { response: CopilotResponse }) {
  const copy = (text: string) => navigator.clipboard?.writeText(text).catch(() => {})
  return (
    <div className="space-y-2">
      {isDegraded(response) && (
        <div className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
          <TriangleAlert className="size-3.5" aria-hidden="true" />
          Agent unavailable — showing a fallback message.
        </div>
      )}
      <p className="whitespace-pre-wrap text-sm">{sanitizeMarkdown(response.answer)}</p>

      {response.proposals.length > 0 && (
        <div className="space-y-1.5">
          <p className="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground">
            Suggested commands (run them yourself)
          </p>
          {response.proposals.map((p) => (
            <div
              key={p.command}
              className="flex items-center gap-2 rounded-md border border-border bg-muted/40 px-2 py-1.5"
            >
              <code className="flex-1 truncate text-xs">{p.command}</code>
              <span
                className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] ${
                  p.requiresApproval
                    ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300'
                    : 'bg-muted text-muted-foreground'
                }`}
              >
                {proposalGateLabel(p)}
              </span>
              <button
                type="button"
                onClick={() => copy(p.command)}
                aria-label={`Copy ${p.command}`}
                className="shrink-0 text-muted-foreground hover:text-foreground"
              >
                <Copy className="size-3.5" aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      )}

      {response.trace.length > 0 && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer select-none">Agent trace ({response.trace.length})</summary>
          <ol className="mt-1 space-y-1 border-l border-border pl-2">
            {response.trace.map((step, i) => (
              <li key={i}>
                <span className="uppercase tracking-wide text-[10px] text-muted-foreground">{step.kind}</span>
                {step.name ? ` · ${step.name}` : ''} — {step.detail}
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  )
}

/** Copilot side-drawer, mounted in the shell and available on every page (F11 R1). */
export function CopilotPanel() {
  const [open, setOpen] = useState(false)
  const [input, setInput] = useState('')
  const [turns, setTurns] = useState<CopilotTurn[]>([])
  const drawerRef = useRef<HTMLDivElement>(null)
  const { pathname } = useLocation()
  const ask = useCopilotAsk()
  useFocusTrap(drawerRef, open)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const question = input.trim()
    if (!question || ask.isPending) return
    setInput('')
    setTurns((t) => [...t, { role: 'user', text: question }])
    ask.mutate(
      { question, context: buildContext(pathname) },
      {
        onSuccess: (res) => setTurns((t) => [...t, { role: 'assistant', text: res.answer, response: res }]),
        onError: (err) =>
          setTurns((t) => [...t, { role: 'assistant', text: describeCopilotError(err) }]),
      },
    )
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Open copilot"
        className="no-print fixed bottom-4 right-4 z-40 flex items-center gap-1.5 rounded-full border border-border bg-background px-3 py-2 text-sm shadow-lg hover:bg-muted"
      >
        <Sparkles className="size-4 text-primary" aria-hidden="true" />
        Copilot
      </button>
    )
  }

  return (
    <div
      ref={drawerRef}
      role="dialog"
      aria-label="Copilot"
      aria-modal="false"
      className="fixed right-0 top-0 z-40 flex h-full w-full max-w-md flex-col border-l border-border bg-background shadow-2xl"
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="flex items-center gap-2 font-semibold">
          <Sparkles className="size-4 text-primary" aria-hidden="true" />
          Copilot
        </span>
        <button type="button" onClick={() => setOpen(false)} aria-label="Close copilot" className="text-muted-foreground hover:text-foreground">
          <X className="size-4" aria-hidden="true" />
        </button>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {turns.length === 0 && (
          <p className="text-sm text-muted-foreground">
            Ask about drift, cost, lineage, or how to do something. I can suggest <code>exa</code> commands —
            you run them yourself.
          </p>
        )}
        {turns.map((turn, i) =>
          turn.role === 'user' ? (
            <p key={i} className="ml-auto max-w-[85%] rounded-lg bg-primary/10 px-3 py-1.5 text-sm">
              {turn.text}
            </p>
          ) : turn.response ? (
            <CopilotAnswer key={i} response={turn.response} />
          ) : (
            <p key={i} className="text-sm text-muted-foreground">
              {turn.text}
            </p>
          ),
        )}
        {ask.isPending && <p className="text-sm text-muted-foreground">Thinking…</p>}
      </div>

      <form onSubmit={submit} className="flex items-center gap-2 border-t border-border p-3">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask the copilot…"
          aria-label="Ask the copilot"
          className="flex-1 rounded-md border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary"
        />
        <button
          type="submit"
          disabled={ask.isPending || input.trim() === ''}
          aria-label="Send"
          className="rounded-md border border-border p-2 text-muted-foreground hover:bg-muted disabled:opacity-40"
        >
          <Send className="size-4" aria-hidden="true" />
        </button>
      </form>
    </div>
  )
}
