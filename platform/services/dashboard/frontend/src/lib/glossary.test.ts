import { describe, it, expect } from 'vitest'
import { GLOSSARY, searchGlossary } from './glossary'

describe('searchGlossary', () => {
  it('returns all terms for an empty query', () => {
    expect(searchGlossary('')).toHaveLength(GLOSSARY.length)
  })
  it('matches term and definition text, case-insensitively', () => {
    const hits = searchGlossary('DRIFT')
    expect(hits.some((t) => t.term === 'Drift')).toBe(true)
  })
  it('returns nothing for an unknown term', () => {
    expect(searchGlossary('zzzznotaterm')).toHaveLength(0)
  })
})
