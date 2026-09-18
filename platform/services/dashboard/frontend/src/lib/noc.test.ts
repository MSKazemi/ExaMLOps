import { describe, expect, it } from 'vitest'
import { buildNocSlides } from './noc'

/**
 * A NOC wall is read from across a room, and silence on it means "quiet". So the two ways a number
 * can be missing must not look the same: a source that has **not reported yet** resolves itself,
 * and a source the BFF could **not reach** is someone's job. Both used to render "Awaiting data".
 */
describe('buildNocSlides', () => {
  const finops = { cost: { total_cost_usd: 120, total_gpu_hours: 30 }, carbon: { co2e_kg: 4.2 } }
  const inbox = { count: 3, counts: { critical: 1, warning: 2 } }

  it('shows the live figures when every source answered', () => {
    const slides = buildNocSlides(finops, inbox, 'en', [])
    expect(slides.find((s) => s.id === 'alerts')?.value).toBe('3')
    expect(slides.find((s) => s.id === 'spend')?.value).not.toBe('—')
  })

  it('degrades to a dash rather than a zero when a source is absent', () => {
    const slides = buildNocSlides(undefined, undefined, 'en', [])
    expect(slides.map((s) => s.value)).toContain('—')
    // A zero would be a claim: "no alerts". A dash is the absence of one.
    expect(slides.find((s) => s.id === 'alerts')?.value).toBe('—')
  })

  it('says "awaiting" when nothing has reported yet', () => {
    const slides = buildNocSlides(undefined, undefined, 'en', [])
    expect(slides.find((s) => s.id === 'alerts')?.sub).toBe('Awaiting data')
  })

  it('says the source is unavailable when the BFF could not reach it', () => {
    const slides = buildNocSlides(undefined, undefined, 'en', ['inbox', 'cost'])
    expect(slides.find((s) => s.id === 'alerts')?.sub).toBe('inbox source unavailable')
    expect(slides.find((s) => s.id === 'spend')?.sub).toBe('cost source unavailable')
  })

  it('keeps the carbon caveat even while that source is down', () => {
    const slides = buildNocSlides(undefined, undefined, 'en', ['carbon'])
    const sub = slides.find((s) => s.id === 'carbon')?.sub ?? ''
    expect(sub).toContain('carbon source unavailable')
    // ADR 0112 R-ee: the operational-only caveat must survive every state of this slide.
    expect(sub).toContain('embodied carbon not measured')
  })
})
