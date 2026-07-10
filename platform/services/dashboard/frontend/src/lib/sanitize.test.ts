import { describe, it, expect } from 'vitest'
import { sanitizeMarkdown, safeUrl } from './sanitize'

describe('sanitizeMarkdown (F16 R2)', () => {
  it('strips <script> blocks', () => {
    expect(sanitizeMarkdown('hi <script>alert(1)</script> there')).toBe('hi  there')
  })
  it('strips inline event handlers', () => {
    expect(sanitizeMarkdown('<img src=x onerror="alert(1)">')).not.toContain('onerror')
  })
  it('neutralizes javascript: URIs', () => {
    expect(sanitizeMarkdown('[x](javascript:alert(1))')).not.toContain('javascript:')
  })
  it('strips iframe / object tags', () => {
    expect(sanitizeMarkdown('<iframe src="evil"></iframe>a')).toBe('a')
  })
  it('leaves ordinary markdown untouched', () => {
    const md = '# Title\n\nSome **bold** and a [link](https://example.com).'
    expect(sanitizeMarkdown(md)).toBe(md)
  })
  it('handles empty input', () => {
    expect(sanitizeMarkdown('')).toBe('')
  })
})

describe('safeUrl (F16 R2)', () => {
  it('blocks javascript: URLs', () => {
    expect(safeUrl('javascript:alert(1)')).toBe('')
  })
  it('allows http/https and relative URLs', () => {
    expect(safeUrl('https://example.com')).toBe('https://example.com')
    expect(safeUrl('/models/jpcp')).toBe('/models/jpcp')
  })
  it('is not tripped up by the stateful regex on repeated calls', () => {
    expect(safeUrl('javascript:x')).toBe('')
    expect(safeUrl('https://ok.com')).toBe('https://ok.com') // would fail if lastIndex leaked
  })
})
