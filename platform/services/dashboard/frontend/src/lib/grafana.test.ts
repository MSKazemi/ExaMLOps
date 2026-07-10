import { describe, it, expect } from 'vitest'
import { buildPanelUrl, GRAFANA_PANELS } from './grafana'

describe('buildPanelUrl', () => {
  const panel = { uid: 'examlops_drift', panelId: 3 }

  it('returns null when no base URL is configured', () => {
    expect(buildPanelUrl(null, panel)).toBeNull()
    expect(buildPanelUrl('', panel)).toBeNull()
  })

  it('builds a d-solo URL with panel id, kiosk and default dark theme', () => {
    const url = buildPanelUrl('http://grafana:3000', panel)!
    expect(url).toContain('http://grafana:3000/d-solo/examlops_drift?')
    expect(url).toContain('panelId=3')
    expect(url).toContain('theme=dark')
    expect(url).toContain('kiosk=')
  })

  it('normalises trailing slashes on the base URL', () => {
    const url = buildPanelUrl('http://grafana:3000///', panel)!
    expect(url).toContain('http://grafana:3000/d-solo/')
    expect(url).not.toContain(':3000///')
  })

  it('encodes theme, time range and template vars', () => {
    const url = buildPanelUrl('http://g', panel, {
      theme: 'light',
      timeRange: { from: 'now-6h', to: 'now' },
      vars: { model: 'JPCP' },
    })!
    expect(url).toContain('theme=light')
    expect(url).toContain('from=now-6h')
    expect(url).toContain('to=now')
    expect(url).toContain('var-model=JPCP')
  })

  it('every registered panel has a uid and numeric panelId', () => {
    for (const ref of Object.values(GRAFANA_PANELS)) {
      expect(ref.uid).toBeTruthy()
      expect(typeof ref.panelId).toBe('number')
    }
  })
})
