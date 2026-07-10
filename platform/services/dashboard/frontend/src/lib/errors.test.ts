import { describe, it, expect } from 'vitest'
import { ApiError, parseProblem, shouldRetry } from './errors'

describe('parseProblem (F23 R2)', () => {
  it('parses RFC 7807 problem+json', () => {
    const p = parseProblem(422, {
      type: 'about:blank',
      title: 'Unprocessable',
      status: 422,
      detail: 'bad field',
      instance: '/api/x',
    })
    expect(p).toEqual({
      type: 'about:blank',
      title: 'Unprocessable',
      status: 422,
      detail: 'bad field',
      instance: '/api/x',
    })
  })
  it('parses FastAPI {detail: string}', () => {
    expect(parseProblem(404, { detail: 'not found' }).detail).toBe('not found')
  })
  it('joins FastAPI validation {detail: [{msg}]}', () => {
    expect(parseProblem(422, { detail: [{ msg: 'a' }, { msg: 'b' }] }).detail).toBe('a; b')
  })
  it('falls back for a non-object body', () => {
    expect(parseProblem(500, null).detail).toBe('API error 500')
  })
})

describe('ApiError', () => {
  it('exposes status and a message from the problem', () => {
    const e = new ApiError(403, { title: 'Forbidden' })
    expect(e).toBeInstanceOf(Error)
    expect(e.status).toBe(403)
    expect(e.message).toBe('Forbidden')
    expect(e.isClientError).toBe(true)
  })
  it('5xx is not a client error', () => {
    expect(new ApiError(503, {}).isClientError).toBe(false)
  })
})

describe('shouldRetry (F23)', () => {
  it('never retries a 4xx ApiError', () => {
    expect(shouldRetry(0, new ApiError(404, {}), 3)).toBe(false)
  })
  it('retries transient/5xx errors up to max', () => {
    expect(shouldRetry(0, new ApiError(500, {}), 1)).toBe(true)
    expect(shouldRetry(1, new ApiError(500, {}), 1)).toBe(false)
    expect(shouldRetry(0, new Error('network'), 1)).toBe(true)
  })
})
