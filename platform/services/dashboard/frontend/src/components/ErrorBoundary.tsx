import { Component, type ErrorInfo, type ReactNode } from 'react'
import { EmptyState } from '@/components/ui/empty-state'
import { TriangleAlert } from 'lucide-react'

interface Props {
  children: ReactNode
  /** Optional custom fallback; defaults to a designed EmptyState. */
  fallback?: ReactNode
}

interface State {
  error: Error | null
}

/**
 * ErrorBoundary — catches render/runtime errors in its subtree and shows a designed fallback instead
 * of a blank white screen (F23 resilience / ADR 0052). A reset button lets the user retry without a
 * full reload. Wrap route content so one page's crash never takes down the whole shell.
 *
 * (React error boundaries must be class components — there is no hook equivalent.)
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surface for local debugging; a real deployment would forward this to F24 telemetry.
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  reset = () => this.setState({ error: null })

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback
      return (
        <div className="p-6">
          <EmptyState
            icon={TriangleAlert}
            title="Something went wrong on this page"
            description={this.state.error.message || 'An unexpected error occurred while rendering.'}
            action={
              <button
                type="button"
                onClick={this.reset}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
              >
                Try again
              </button>
            }
          />
        </div>
      )
    }
    return this.props.children
  }
}
