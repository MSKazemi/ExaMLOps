import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Minimize2 } from 'lucide-react'
import { useRotator } from '@/lib/responsive'
import { buildNocSlides } from '@/lib/noc'
import { useFinops } from '@/lib/finops'
import { useAlerts } from '@/lib/alerts'
import { useI18n } from '@/hooks/i18nContext'

function useClock(locale: string): string {
  const fmt = (l: string) => new Intl.DateTimeFormat(l, { timeStyle: 'medium' }).format(new Date())
  // Seed in the initializer; the effect only schedules updates (no set-state in the effect body).
  const [now, setNow] = useState(() => fmt(locale))
  useEffect(() => {
    const id = setInterval(() => setNow(fmt(locale)), 1000)
    return () => clearInterval(id)
  }, [locale])
  return now
}

/**
 * NOC / wall kiosk view (F20 / ADR 0069, R2). Big-font, dark, auto-rotating curated dashboards with no
 * interaction chrome. Rendered as a fixed full-screen overlay inside the authed app, so it never drops to
 * a login (long-lived viewer token — F15). Reach it at `/noc` (or any page with `?kiosk=1`).
 */
export function NocWall() {
  const { locale } = useI18n()
  const { data: finops } = useFinops()
  const { data: alerts } = useAlerts()
  const slides = buildNocSlides(finops, alerts?.inbox, locale)
  const active = useRotator(slides.length, 12_000)
  const clock = useClock(locale)
  const slide = slides[active] ?? slides[0]

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black text-white" data-testid="noc-wall">
      <header className="flex items-center justify-between px-8 py-5 text-sm text-white/60">
        <span className="font-semibold tracking-widest uppercase">ExaMLOps · NOC</span>
        <span className="tabular-nums" aria-label="Current time">{clock}</span>
        <Link to="/" aria-label="Exit kiosk" className="flex items-center gap-1 text-white/50 hover:text-white">
          <Minimize2 className="size-4" aria-hidden="true" /> Exit
        </Link>
      </header>

      <main className="flex flex-1 flex-col items-center justify-center gap-4 text-center">
        <p className="text-2xl uppercase tracking-widest text-white/50">{slide.title}</p>
        <p className="text-[12vw] font-bold leading-none tabular-nums">{slide.value}</p>
        <p className="text-3xl text-white/70">{slide.sub}</p>
      </main>

      <footer className="flex items-center justify-center gap-3 pb-8">
        {slides.map((s, i) => (
          <span
            key={s.id}
            aria-hidden="true"
            className={`size-3 rounded-full ${i === active ? 'bg-white' : 'bg-white/25'}`}
          />
        ))}
      </footer>
    </div>
  )
}
