import { describe, it, expect } from 'vitest'
import { can, reason, requiresStepUp, CAP } from './capabilities'

describe('can (F15 R2 affordance)', () => {
  it('is true only when the capability is present', () => {
    expect(can([CAP.VIEW, CAP.SEARCH], CAP.SEARCH)).toBe(true)
    expect(can([CAP.VIEW], CAP.MODEL_PROMOTE)).toBe(false)
  })
  it('is false for an undefined capability set (default-deny)', () => {
    expect(can(undefined, CAP.VIEW)).toBe(false)
  })
})

describe('reason (F15 R3 — explain denials)', () => {
  it('is empty when allowed', () => {
    expect(reason([CAP.MODEL_PROMOTE], CAP.MODEL_PROMOTE)).toBe('')
  })
  it('explains when denied', () => {
    expect(reason([CAP.VIEW], CAP.MODEL_PROMOTE)).toMatch(/permission/i)
  })
})

describe('requiresStepUp (F15 R6)', () => {
  it('flags governed actions', () => {
    expect(requiresStepUp(CAP.MODEL_PROMOTE)).toBe(true)
    expect(requiresStepUp(CAP.SECRET_REVEAL)).toBe(true)
    expect(requiresStepUp(CAP.SEARCH)).toBe(false)
  })
})
