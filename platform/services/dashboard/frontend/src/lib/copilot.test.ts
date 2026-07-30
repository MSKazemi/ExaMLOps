import { describe, it, expect } from 'vitest'
import { buildContext, isDegraded, proposalGateLabel, type CopilotResponse } from './copilot'

describe('buildContext', () => {
  it('derives an entity from a two-segment path', () => {
    expect(buildContext('/models/jpcp')).toEqual({
      page: '/models/jpcp',
      entity: { type: 'models', id: 'jpcp' },
      filters: undefined,
    })
  })
  it('ignores a leading lifecycle-group prefix when grounding the entity', () => {
    // ADR 0097 §1: /build/models/jpcp grounds to the same entity as the old /models/jpcp.
    expect(buildContext('/build/models/jpcp')).toEqual({
      page: '/build/models/jpcp',
      entity: { type: 'models', id: 'jpcp' },
    })
    // A group root alone (/operate/drift) has one meaningful segment → no entity.
    expect(buildContext('/operate/drift').entity).toBeUndefined()
  })

  it('omits the entity for a single-segment path and passes filters', () => {
    const ctx = buildContext('/drift', { env: 'prod' })
    expect(ctx.entity).toBeUndefined()
    expect(ctx.filters).toEqual({ env: 'prod' })
  })
  it('normalizes an empty path to root', () => {
    expect(buildContext('').page).toBe('/')
  })
})

describe('isDegraded', () => {
  const base: CopilotResponse = { answer: '', hitl_required: false, proposals: [], trace: [] }
  it('is true when _partial names the agent', () => {
    expect(isDegraded({ ...base, _partial: ['agent'] })).toBe(true)
  })
  it('is false with no _partial', () => {
    expect(isDegraded(base)).toBe(false)
  })
})

describe('proposalGateLabel', () => {
  it('labels approval-gated vs read-only proposals', () => {
    expect(proposalGateLabel({ command: 'exa retrain m', requiresApproval: true })).toBe('Needs approval')
    expect(proposalGateLabel({ command: 'exa status', requiresApproval: false })).toBe('Read-only')
  })
})
