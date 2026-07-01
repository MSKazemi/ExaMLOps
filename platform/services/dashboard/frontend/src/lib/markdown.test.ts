// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { rewriteImageUrls } from './markdown'

describe('rewriteImageUrls', () => {
  it('replaces dashboard:// placeholders', () => {
    const md = '![Alt](dashboard://image/abc-123)'
    const images = [{ id: 'abc-123', placeholder: 'dashboard://image/abc-123', url: 'https://minio/bucket/key' }]
    expect(rewriteImageUrls(md, images)).toBe('![Alt](https://minio/bucket/key)')
  })

  it('leaves filesystem image refs unchanged', () => {
    const md = '![Alt](images/diagram.png)'
    expect(rewriteImageUrls(md, [])).toBe('![Alt](images/diagram.png)')
  })

  it('skips images without id', () => {
    const md = '![Alt](dashboard://image/x)'
    const images = [{ id: null, placeholder: 'images/diagram.png', url: 'http://cp/img' }]
    expect(rewriteImageUrls(md, images)).toBe('![Alt](dashboard://image/x)')
  })
})
