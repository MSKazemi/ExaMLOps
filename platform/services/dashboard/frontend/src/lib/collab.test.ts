import { describe, it, expect } from 'vitest'
import { extractMentions, snapshotShareUrl } from './collab'

describe('extractMentions', () => {
  it('returns unique @-mentions in order', () => {
    expect(extractMentions('hi @alice @bob @alice')).toEqual(['alice', 'bob'])
  })
  it('is empty when there are no mentions', () => {
    expect(extractMentions('no mentions here')).toEqual([])
  })
})

describe('snapshotShareUrl', () => {
  it('builds an absolute shareable link with the token', () => {
    expect(snapshotShareUrl('abc123', 'https://dash.example')).toBe('https://dash.example/?snapshot=abc123')
  })
  it('url-encodes the token', () => {
    expect(snapshotShareUrl('a/b', 'https://x')).toBe('https://x/?snapshot=a%2Fb')
  })
})
