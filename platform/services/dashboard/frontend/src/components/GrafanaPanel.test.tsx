import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { GrafanaPanel } from './GrafanaPanel'

describe('GrafanaPanel', () => {
  it('renders an accessible fallback when Grafana is not configured', () => {
    render(<GrafanaPanel name="drift.trend" baseUrl={null} />)
    expect(screen.getByText('Grafana not configured')).toBeInTheDocument()
  })

  it('renders a d-solo iframe with the registry panel when configured', () => {
    render(<GrafanaPanel name="drift.trend" baseUrl="http://grafana:3000" title="Drift" />)
    const frame = screen.getByTitle('Drift') as HTMLIFrameElement
    expect(frame.tagName).toBe('IFRAME')
    expect(frame.getAttribute('src')).toContain('/d-solo/examlops_drift')
    expect(frame.getAttribute('loading')).toBe('lazy')
  })

  it('passes theme and template vars through to the iframe URL', () => {
    render(
      <GrafanaPanel name="model.inferences" baseUrl="http://g" theme="light" vars={{ model: 'JPCP' }} />,
    )
    const src = (screen.getByTitle('Grafana panel: model.inferences') as HTMLIFrameElement).getAttribute('src')!
    expect(src).toContain('theme=light')
    expect(src).toContain('var-model=JPCP')
  })
})
