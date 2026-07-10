import { useState } from 'react'
import { Sparkles } from 'lucide-react'
import { useOnboarding } from '@/lib/prefs'

// First-run guided tour (F21 / ADR 0072, R5). Self-hosted, runs once until completed/skipped
// (persisted via useOnboarding). Steps are static copy — no SaaS, no external tour library.
const STEPS: { title: string; body: string }[] = [
  {
    title: 'Welcome to ExaMLOps',
    body: 'This dashboard is your control plane for training, serving, drift, cost, and governance across the platform.',
  },
  {
    title: 'Find anything fast',
    body: 'Press ⌘K (or Ctrl-K) for the command palette — jump to any model, job, or page and copy the equivalent exa command.',
  },
  {
    title: 'Ask the copilot',
    body: 'Use the Copilot to ask grounded questions and get suggested exa commands. It proposes; you run.',
  },
  {
    title: 'Make it yours',
    body: 'Set your default landing page, theme, language, and pin the models you care about in Preferences. Press ? any time for help.',
  },
]

export function OnboardingTour() {
  const { done, complete } = useOnboarding()
  const [step, setStep] = useState(0)
  if (done) return null

  const isLast = step === STEPS.length - 1
  const current = STEPS[step]

  return (
    <div className="no-print fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4" role="presentation">
      <div role="dialog" aria-label="Getting started" aria-modal="true" className="w-full max-w-md rounded-xl border border-border bg-background p-6 shadow-2xl">
        <div className="mb-3 flex items-center gap-2">
          <Sparkles className="size-5 text-primary" aria-hidden="true" />
          <h2 className="text-lg font-bold">{current.title}</h2>
        </div>
        <p className="text-sm text-muted-foreground">{current.body}</p>

        <div className="mt-4 flex items-center gap-1.5">
          {STEPS.map((s, i) => (
            <span key={s.title} aria-hidden="true" className={`h-1.5 rounded-full ${i === step ? 'w-6 bg-primary' : 'w-1.5 bg-muted'}`} />
          ))}
        </div>

        <div className="mt-5 flex items-center justify-between">
          <button type="button" onClick={complete} className="text-xs text-muted-foreground hover:text-foreground">
            Skip
          </button>
          <button
            type="button"
            onClick={() => (isLast ? complete() : setStep((s) => s + 1))}
            className="rounded-md border border-primary bg-primary px-4 py-1.5 text-sm text-primary-foreground"
          >
            {isLast ? 'Get started' : 'Next'}
          </button>
        </div>
      </div>
    </div>
  )
}
