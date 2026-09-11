import { describe, it, expect } from 'vitest'
import { promotionVerdict, freshnessLabel, evalGateView, type EvalGate, type PromotionCheck } from './mlops'

const base: PromotionCheck = {
  model: 'JPCP',
  mlflowName: 'jpcp',
  policy: { allow: true, reasons: [] },
  eval: { state: 'no_gate', pass: null, reason: 'no eval gate configured', metrics: [], lastReport: null },
  approval: { required: true, state: 'pending' },
  allowed: true,
}

describe('promotionVerdict (F9 R4)', () => {
  it('blocks with the first reason when not allowed', () => {
    const chk = { ...base, allowed: false, policy: { allow: false, reasons: ['no promotion policy configured'] } }
    const v = promotionVerdict(chk)
    expect(v.tone).toBe('warn')
    expect(v.text).toContain('no promotion policy configured')
  })

  it('surfaces the pending approval step even when eligible', () => {
    const v = promotionVerdict(base)
    expect(v.tone).toBe('warn')
    expect(v.text).toContain('awaiting approval')
  })

  it('is ready only when allowed and no approval required', () => {
    const v = promotionVerdict({ ...base, approval: { required: false } })
    expect(v.tone).toBe('ok')
    expect(v.text).toBe('Ready to promote')
  })
})

describe('freshnessLabel', () => {
  it('renders a dash when never recorded', () => {
    expect(freshnessLabel(null)).toBe('—')
  })
  it('formats an ISO timestamp to date + HH:MM', () => {
    expect(freshnessLabel('2026-07-02T11:00:00')).toBe('2026-07-02 11:00')
  })
})

describe('evalGateView (ADR 0008)', () => {
  const gate = (state: EvalGate['state']): EvalGate => ({
    state,
    pass: state === 'passed' ? true : state === 'failed' || state === 'warned' ? false : null,
    reason: '',
    metrics: [],
    lastReport: null,
  })
  it('never reads a failed or unrun gate as healthy', () => {
    expect(evalGateView(gate('failed')).status).toBe('failed')
    expect(evalGateView(gate('not_run')).status).toBe('pending')
    expect(evalGateView(gate('warned'))).toEqual({
      status: 'degraded',
      label: 'Eval gate warning — not blocking',
    })
  })
  it('passes only on a passing report, and says when no gate exists', () => {
    expect(evalGateView(gate('passed')).status).toBe('healthy')
    expect(evalGateView(gate('no_gate'))).toEqual({ status: 'unknown', label: 'No eval gate' })
  })
})
