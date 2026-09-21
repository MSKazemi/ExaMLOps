import { describe, it, expect } from 'vitest'
import {
  buildContext,
  describeCopilotError,
  isDegraded,
  proposalGateLabel,
  type CopilotResponse,
} from './copilot'
import { ApiError } from './errors'

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

describe('describeCopilotError', () => {
  it.each([
    [401, /session has expired/i],
    [403, /not permitted/i],
    [429, /too many/i],
    [500, /HTTP 500/],
    [503, /HTTP 503/],
  ])('says something specific for HTTP %i', (status, pattern) => {
    expect(describeCopilotError(new ApiError(status, { title: 'x' }))).toMatch(pattern)
  })

  it('names a rejected request and its reason', () => {
    expect(describeCopilotError(new ApiError(422, { title: 'Invalid', detail: 'question is empty' }))).toMatch(
      /HTTP 422.*question is empty/,
    )
  })

  it('tells a network failure apart from an API failure', () => {
    expect(describeCopilotError(new TypeError('Failed to fetch'))).toMatch(/cannot reach the dashboard API/i)
  })

  it('keeps a generic sentence only for the truly unknown', () => {
    expect(describeCopilotError('boom')).toMatch(/failed unexpectedly/)
  })
})
