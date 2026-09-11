import { useEffect, useId, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useFocusTrap } from '@/hooks/useFocusTrap'

interface DialogProps {
  title: ReactNode
  /** Short line under the title (e.g. the command a form runs). */
  subtitle?: ReactNode
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  /** Width preset; content scrolls inside the viewport either way. */
  size?: 'md' | 'lg' | 'xl'
}

const WIDTH = { md: 'max-w-lg', lg: 'max-w-2xl', xl: 'max-w-4xl' }

/**
 * Accessible modal dialog (F18): `role="dialog"` + `aria-modal`, labelled by its title, focus
 * trapped while open and restored on close, Escape and a backdrop click close it. The one dialog
 * primitive for create/edit/delete flows, so every console's modals behave the same.
 */
export function Dialog({ title, subtitle, onClose, children, footer, size = 'lg' }: DialogProps) {
  const ref = useRef<HTMLDivElement>(null)
  const titleId = useId()
  useFocusTrap(ref, true)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/55 p-4 pt-[8vh]" onClick={onClose} role="presentation">
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className={cn('w-full overflow-hidden rounded-xl border border-border bg-background shadow-2xl', WIDTH[size])}
      >
        <header className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 id={titleId} className="text-sm font-semibold">
              {title}
            </h2>
            {subtitle && <div className="mt-0.5 text-[11px] text-muted-foreground">{subtitle}</div>}
          </div>
          <button type="button" onClick={onClose} aria-label="Close dialog" className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
            <X className="size-4" aria-hidden="true" />
          </button>
        </header>
        <div className="max-h-[75vh] overflow-y-auto p-4">{children}</div>
        {footer && <footer className="flex justify-end gap-2 border-t border-border px-4 py-3">{footer}</footer>}
      </div>
    </div>
  )
}
