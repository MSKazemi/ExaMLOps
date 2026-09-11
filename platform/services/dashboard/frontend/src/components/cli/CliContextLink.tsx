import { Link, useLocation } from 'react-router-dom'
import { SquareTerminal } from 'lucide-react'
import { CLI_CONSOLE_PATH, cliFilterHref, cliPrefixesFor } from '@/lib/cli'

/**
 * "exa drift ›" — from any console, one click to every `exa` command in its area (ADR 0119).
 * A console shows the curated workflow; this link is the way to the rest of what the CLI can do
 * there, so no console is a dead end. Hidden where no CLI group maps to the page.
 */
export function CliContextLink() {
  const { pathname } = useLocation()
  const prefixes = cliPrefixesFor(pathname)
  if (!prefixes.length || pathname.startsWith(CLI_CONSOLE_PATH)) return null
  const more = prefixes.length > 1 ? ` +${prefixes.length - 1}` : ''
  return (
    <Link
      to={cliFilterHref(prefixes)}
      // The accessible name says where the link goes; the page title is already announced, so
      // repeating "Drift" here would only make two links answer to the same name.
      aria-label="Open the CLI Console for this page's commands"
      title={`All ${prefixes.map((p) => `exa ${p}`).join(', ')} commands`}
      className="no-print fixed bottom-4 right-[14.5rem] z-40 hidden items-center gap-1.5 rounded-full border border-border bg-background px-3 py-2 text-sm shadow-lg hover:bg-muted sm:flex"
    >
      <SquareTerminal className="size-4 text-primary" aria-hidden="true" />
      <code className="text-xs">
        exa {prefixes[0]}
        {more}
      </code>
    </Link>
  )
}
