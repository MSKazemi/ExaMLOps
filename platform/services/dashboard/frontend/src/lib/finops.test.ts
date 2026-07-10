import { describe, it, expect } from 'vitest'
import { usd, budgetPct, carbonLabel } from './finops'

describe('usd', () => {
  it('formats amounts and handles null', () => {
    expect(usd(1234.5)).toBe('$1,234.50')
    expect(usd(null)).toBe('—')
  })
})

describe('budgetPct (F13 R2)', () => {
  it('formats a ratio as a percent', () => {
    expect(budgetPct(0.42)).toBe('42%')
    expect(budgetPct(1.33)).toBe('133%')
  })
  it('handles no-budget and infinite', () => {
    expect(budgetPct(null)).toBe('no budget')
    expect(budgetPct(Infinity)).toBe('∞')
  })
})

describe('carbonLabel (F13 R3 — honest uncertainty)', () => {
  it('shows the value with its ± uncertainty band', () => {
    expect(carbonLabel(4.5, 0.3)).toBe('4.5 kg ±30%')
  })
})
