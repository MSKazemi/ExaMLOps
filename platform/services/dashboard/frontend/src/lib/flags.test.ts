import { describe, it, expect } from 'vitest'
import { resolveFlag, FLAGS } from './flags'

function storageWith(map: Record<string, string>): Pick<Storage, 'getItem'> {
  return { getItem: (k: string) => (k in map ? map[k] : null) }
}

describe('resolveFlag (F23 R7)', () => {
  it('returns the registry default with no overrides', () => {
    expect(resolveFlag('mlopsConsole')).toBe(FLAGS.mlopsConsole.default)
  })
  it('localStorage override wins over everything', () => {
    expect(resolveFlag('mlopsConsole', { VITE_FLAG_MLOPSCONSOLE: 'false' }, storageWith({ 'flag:mlopsConsole': 'true' }))).toBe(true)
    expect(resolveFlag('mlopsConsole', {}, storageWith({ 'flag:mlopsConsole': 'false' }))).toBe(false)
  })
  it('env override applies when no storage override', () => {
    expect(resolveFlag('facilityConsole', { VITE_FLAG_FACILITYCONSOLE: 'false' })).toBe(false)
    expect(resolveFlag('facilityConsole', { VITE_FLAG_FACILITYCONSOLE: 'true' })).toBe(true)
  })
  it('ignores malformed override values and uses the default', () => {
    expect(resolveFlag('commandPalette', { VITE_FLAG_COMMANDPALETTE: 'yes' })).toBe(
      FLAGS.commandPalette.default,
    )
  })
})
